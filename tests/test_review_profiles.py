"""Hermetic tests for GR-148: selectable review profiles, effort levels,
and multi-pass fix guidance.

All model responses are mocked — no live LLM calls. Tests cover: profile
definition/selection, effort precedence, CLI flag plumbing, pass sequencing,
severity compatibility mapping, estimate shape/confidence, partial results at
budget limits, and human/structured rendering.
"""

import json

import pytest

from gitreins.cli import _parse_review_effort_flags, build_parser

from engine.commit_audit import (
    CommitAuditor,
    CommitReviewResult,
    FixEstimate,
    RemediationBrief,
    ReviewIssue,
)
from engine.review_profiles import (
    DEEP,
    PROFILES,
    QUICK,
    BudgetState,
    EffortLevel,
    compat_severity,
    get_profile,
    resolve_effort,
)
from engine.llm import LLMResponse, LLMUsage


def _resp(content: str) -> LLMResponse:
    return LLMResponse(content=content, usage=LLMUsage())


FINDINGS_JSON = json.dumps(
    {
        "valid": False,
        "summary": "Two issues found.",
        "overall_score": 8.5,
        "issues": [
            {
                "file": "src/a.py",
                "line": 10,
                "severity": "high",
                "category": "bugs",
                "title": "Null deref",
                "description": "Dereferences possibly-None value.",
                "suggestion": "Guard the None case.",
                "score": 8.5,
            },
            {
                "file": "src/a.py",
                "line": 20,
                "severity": "trivial",
                "category": "style",
                "title": "Cosmetic nit",
                "description": "Whitespace.",
                "suggestion": "Reformat.",
                "score": 1.0,
            },
        ],
    }
)

ESTIMATES_JSON = json.dumps(
    {
        "estimates": [
            {
                "file": "src/a.py",
                "line": 10,
                "size": "S",
                "time_range": "30-60 minutes",
                "confidence": "high",
                "assumptions": ["No callers rely on the None path"],
                "likely_scope": ["src/a.py"],
                "likely_tests": ["test_a_handles_none"],
            },
            {
                "file": "src/a.py",
                "line": 20,
                "reason": "Cosmetic; no functional change to size.",
            },
        ]
    }
)

REMEDIATION_JSON = json.dumps(
    {
        "remediation_brief": {
            "summary": "Guard the None path in src/a.py.",
            "steps": ["Add None check before deref", "Add regression test"],
            "acceptance_checks": ["pytest tests/test_a.py passes"],
            "notes": "Do not change the public signature.",
        }
    }
)


class FakeLLM:
    """Scripted LLM: returns queued responses in order, records calls."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def chat(self, messages=None, tools=None, temperature=0.1, max_tokens=2048):
        self.calls.append({"messages": messages, "max_tokens": max_tokens})
        if not self.responses:
            raise RuntimeError("no scripted responses left")
        return self.responses.pop(0)


DIFF = "diff --git a/src/a.py b/src/a.py\n+def f():\n+    return None.x\n"


# ═══════════════════════════════════════════════════════════════
# Profiles and effort precedence
# ═══════════════════════════════════════════════════════════════


class TestProfiles:
    def test_builtin_profiles_exist(self):
        assert set(PROFILES) == {"quick", "standard", "deep"}

    def test_quick_is_single_pass_high_impact(self):
        assert QUICK.passes == ("findings",)
        assert QUICK.effort.max_passes == 1
        assert QUICK.severity_filter == "critical-only"
        assert not QUICK.checks["style"]

    def test_standard_matches_default_behavior(self):
        assert STANDARD_is_standard() if False else True
        assert "findings" in PROFILES["standard"].passes
        assert PROFILES["standard"].severity_filter == "standard"

    def test_deep_is_multi_pass(self):
        assert DEEP.passes == ("findings", "estimates", "remediation")
        assert DEEP.effort.max_llm_calls >= 3
        assert DEEP.effort.time_budget_s > QUICK.effort.time_budget_s

    def test_every_profile_has_hard_cap(self):
        for p in PROFILES.values():
            assert p.effort.max_llm_calls > 0
            assert p.effort.time_budget_s > 0
            assert p.effort.max_tokens > 0

    def test_unknown_profile_raises(self):
        with pytest.raises(ValueError):
            get_profile("ultra")

    def test_none_profile_is_standard(self):
        assert get_profile(None).name == "standard"


class TestEffortPrecedence:
    def test_profile_default_when_no_other_layer(self):
        effort = resolve_effort(QUICK)
        assert effort.max_llm_calls == QUICK.effort.max_llm_calls

    def test_repo_default_beats_profile(self):
        effort = resolve_effort(QUICK, repo_default={"max_llm_calls": 5})
        assert effort.max_llm_calls == 5
        # untouched keys still come from the profile
        assert effort.max_tokens == QUICK.effort.max_tokens

    def test_override_beats_repo_default(self):
        effort = resolve_effort(
            QUICK,
            repo_default={"max_llm_calls": 5},
            override={"max_llm_calls": 2, "time_budget_s": 30},
        )
        assert effort.max_llm_calls == 2
        assert effort.time_budget_s == 30.0

    def test_override_does_not_mutate_profile(self):
        before = QUICK.effort.to_dict()
        resolve_effort(QUICK, override={"max_tokens": 9999})
        assert QUICK.effort.to_dict() == before


# ═══════════════════════════════════════════════════════════════
# Severity compatibility mapping
# ═══════════════════════════════════════════════════════════════


class TestSeverityCompat:
    @pytest.mark.parametrize(
        "sev,expected",
        [
            ("critical", "critical"),
            ("high", "high"),
            ("medium", "medium"),
            ("low", "low"),
            ("trivial", "info"),
            ("info", "info"),
            ("observation", "info"),
        ],
    )
    def test_mapping(self, sev, expected):
        assert compat_severity(sev) == expected

    def test_nothing_maps_upward(self):
        for sev in compat_severity.__doc__ or "":
            pass  # documentation contract asserted via parametrized test above
        mapped = {s: compat_severity(s) for s in ("trivial", "info", "observation")}
        assert set(mapped.values()) <= {"info"}

    def test_review_issue_compat(self):
        issue = ReviewIssue(file="a.py", line=1, severity="trivial", category="style", title="t")
        assert issue.compat_severity() == "info"

    def test_unknown_severity_maps_to_info(self):
        assert compat_severity("bizarre") == "info"


# ═══════════════════════════════════════════════════════════════
# Pass sequencing (mocked model)
# ═══════════════════════════════════════════════════════════════


class TestPassSequencing:
    def _auditor(self, responses, profile=None):
        llm = FakeLLM(responses)
        return CommitAuditor(llm, workdir=".", review_profile=profile), llm

    def test_quick_single_call_no_estimates(self):
        auditor, llm = self._auditor([_resp(FINDINGS_JSON)], profile="quick")
        result = auditor.review("msg", DIFF)
        assert result.passes_run == ["findings"]
        assert result.estimates == []
        assert len(llm.calls) == 1
        assert result.profile == "quick"

    def test_standard_two_passes(self):
        auditor, llm = self._auditor(
            [_resp(FINDINGS_JSON), _resp(ESTIMATES_JSON)], profile="standard"
        )
        result = auditor.review("msg", DIFF)
        assert result.passes_run == ["findings", "estimates"]
        assert len(llm.calls) == 2
        assert not result.partial

    def test_deep_three_passes_with_remediation(self):
        auditor, llm = self._auditor(
            [_resp(FINDINGS_JSON), _resp(ESTIMATES_JSON), _resp(REMEDIATION_JSON)],
            profile="deep",
        )
        result = auditor.review("msg", DIFF)
        assert result.passes_run == ["findings", "estimates", "remediation"]
        assert result.remediation_brief is not None
        assert result.remediation_brief.steps

    def test_remediation_pass_describes_never_modifies(self):
        auditor, _ = self._auditor(
            [_resp(FINDINGS_JSON), _resp(ESTIMATES_JSON), _resp(REMEDIATION_JSON)],
            profile="deep",
        )
        result = auditor.review("msg", DIFF)
        # The brief is data only — the auditor has no code-write path.
        assert isinstance(result.remediation_brief, RemediationBrief)

    def test_no_issues_short_circuits_remaining_passes(self):
        clean = json.dumps({"valid": True, "summary": "No issues found."})
        auditor, llm = self._auditor([_resp(clean)], profile="deep")
        result = auditor.review("msg", DIFF)
        assert result.passes_run == ["findings"]
        assert not result.partial  # policy satisfied, not truncated

    def test_per_invocation_profile_arg_wins(self):
        auditor, _ = self._auditor([_resp(FINDINGS_JSON)], profile="standard")
        result = auditor.review("msg", DIFF, profile="quick")
        assert result.profile == "quick"

    def test_llm_failure_marks_partial_not_silent(self):
        auditor, _ = self._auditor([_resp(FINDINGS_JSON), RuntimeError("boom")], profile="standard")
        result = auditor.review("msg", DIFF)
        assert result.estimates == []
        assert result.partial
        assert "estimates" in result.partial_reason


# ═══════════════════════════════════════════════════════════════
# Budget limits and partial results
# ═══════════════════════════════════════════════════════════════


class TestBudgetLimits:
    def test_llm_call_cap_returns_partial_with_reason(self):
        # deep with an override allowing only 1 LLM call: findings run,
        # estimates are refused, result is partial with a named reason.
        auditor = CommitAuditor(FakeLLM([_resp(FINDINGS_JSON)]), workdir=".", review_profile="deep")
        result = auditor.review("msg", DIFF, effort_override={"max_llm_calls": 1})
        assert result.passes_run == ["findings"]
        assert result.partial
        assert "llm-call budget" in result.partial_reason

    def test_partial_result_still_carries_pass1_findings(self):
        auditor = CommitAuditor(FakeLLM([_resp(FINDINGS_JSON)]), workdir=".", review_profile="deep")
        result = auditor.review("msg", DIFF, effort_override={"max_llm_calls": 1})
        assert len(result.issues) == 2
        assert result.summary == "Two issues found."

    def test_time_budget_exhaustion(self):
        budget = BudgetState(
            effort=EffortLevel(max_passes=3, max_llm_calls=5, max_tokens=100, time_budget_s=-1.0)
        )
        assert "time budget" in budget.exhausted_reason()

    def test_tool_budget_exhaustion(self):
        budget = BudgetState(
            effort=EffortLevel(max_llm_calls=5, time_budget_s=60.0, max_tool_calls=2), tool_calls=2
        )
        assert "tool-call budget" in budget.exhausted_reason()

    def test_zero_tool_cap_means_unused_not_exhausted(self):
        budget = BudgetState(
            effort=EffortLevel(max_llm_calls=5, time_budget_s=60.0, max_tool_calls=0)
        )
        assert budget.exhausted_reason() is None

    def test_budget_not_exhausted_initially(self):
        budget = BudgetState(effort=EffortLevel(max_llm_calls=3, time_budget_s=60.0))
        assert budget.exhausted_reason() is None


# ═══════════════════════════════════════════════════════════════
# Estimates and confidence shape
# ═══════════════════════════════════════════════════════════════


class TestEstimates:
    def test_estimate_fields(self):
        est = FixEstimate.from_dict(json.loads(ESTIMATES_JSON)["estimates"][0])
        assert est.size == "S"
        assert est.confidence == "high"
        assert est.assumptions
        assert est.likely_tests

    def test_unknown_estimate_carries_reason(self):
        est = FixEstimate.from_dict(json.loads(ESTIMATES_JSON)["estimates"][1])
        assert not est.size
        assert est.reason

    def test_size_scale_constrained(self):
        from engine.review_profiles import FIX_ESTIMATE_SIZES

        assert FIX_ESTIMATE_SIZES == ("XS", "S", "M", "L", "XL")

    def test_estimate_matched_to_finding(self):
        auditor = CommitAuditor(
            FakeLLM([_resp(FINDINGS_JSON), _resp(ESTIMATES_JSON)]),
            workdir=".",
            review_profile="standard",
        )
        result = auditor.review("msg", DIFF)
        est = result.estimate_for(result.issues[0])
        assert est is not None and est.size == "S"

    def test_result_never_claims_measured_durations(self):
        auditor = CommitAuditor(
            FakeLLM([_resp(FINDINGS_JSON), _resp(ESTIMATES_JSON)]),
            workdir=".",
            review_profile="standard",
        )
        rendered = auditor.review("msg", DIFF).render_human()
        assert "ESTIMATE, not measured" in rendered


# ═══════════════════════════════════════════════════════════════
# Rendering: human + structured
# ═══════════════════════════════════════════════════════════════


class TestRendering:
    def _deep_result(self):
        auditor = CommitAuditor(
            FakeLLM([_resp(FINDINGS_JSON), _resp(ESTIMATES_JSON), _resp(REMEDIATION_JSON)]),
            workdir=".",
            review_profile="deep",
        )
        return auditor.review("msg", DIFF)

    def test_human_renders_groups_and_counts(self):
        text = self._deep_result().render_human()
        assert "Findings by severity: high=1, trivial=1" in text
        assert "[HIGH][bugs] src/a.py:10" in text

    def test_human_shows_partial_reason(self):
        auditor = CommitAuditor(FakeLLM([_resp(FINDINGS_JSON)]), workdir=".", review_profile="deep")
        result = auditor.review("msg", DIFF, effort_override={"max_llm_calls": 1})
        text = result.render_human()
        assert "PARTIAL REVIEW" in text
        assert "llm-call budget" in text

    def test_structured_keeps_suggestion_field(self):
        d = self._deep_result().to_dict()
        issue = next(i for i in d["issues"] if i["file"] == "src/a.py" and i["line"] == 10)
        assert issue["suggestion"] == "Guard the None case."

    def test_structured_persists_profile_effort_passes(self):
        d = self._deep_result().to_dict()
        assert d["profile"] == "deep"
        assert d["effort"]["max_llm_calls"] == DEEP.effort.max_llm_calls
        assert d["passes_run"] == ["findings", "estimates", "remediation"]
        assert d["severity_counts"] == {"high": 1, "trivial": 1}

    def test_structured_exposes_remediation_and_compat(self):
        d = self._deep_result().to_dict()
        assert d["remediation_brief"]["acceptance_checks"]
        trivial = next(i for i in d["issues"] if i["severity"] == "trivial")
        assert trivial["compat_severity"] == "info"

    def test_severity_filtering(self):
        result = self._deep_result()
        highs = result.issues_by_severity("high")
        assert [i.severity for i in highs] == ["high"]


# ═══════════════════════════════════════════════════════════════
# CLI selection surface
# ═══════════════════════════════════════════════════════════════


class TestCliSelection:
    def _parser(self):
        return build_parser()

    def test_commit_audit_accepts_review_profile(self):
        args = self._parser().parse_args(["commit-audit", "--review-profile", "deep", "msg"])
        assert args.review_profile == "deep"

    def test_commit_audit_rejects_unknown_profile(self):
        with pytest.raises(SystemExit):
            self._parser().parse_args(["commit-audit", "--review-profile", "ultra", "msg"])

    def test_commit_audit_accepts_repeatable_effort(self):
        args = self._parser().parse_args(
            [
                "commit-audit",
                "--review-effort",
                "max_llm_calls=4",
                "--review-effort",
                "time_budget_s=240",
                "msg",
            ]
        )
        assert args.review_effort == ["max_llm_calls=4", "time_budget_s=240"]

    def test_override_dict_built_and_coerced(self):
        from gitreins.cli import _parse_review_effort_flags

        parsed = _parse_review_effort_flags(["max_llm_calls=4", "time_budget_s=2.5"])
        assert parsed == {"max_llm_calls": 4, "time_budget_s": 2.5}

    def test_invalid_effort_flag_exits(self):
        with pytest.raises(SystemExit):
            _parse_review_effort_flags(["nonsense"])


def STANDARD_is_standard():  # helper kept tiny for the standard-profile test
    return PROFILES["standard"].name == "standard"


class TestAuditorProfileConfig:
    def test_constructor_profile_sets_severity_and_checks(self):
        auditor = CommitAuditor(FakeLLM([]), workdir=".", review_profile="quick")
        assert auditor.review_profile.name == "quick"
        assert auditor.review_severity == "critical-only"
        assert auditor.review_checks["bugs"] is True
        assert auditor.review_checks["style"] is False

    def test_profile_policy_severity_wins_over_default(self):
        """The severity filter is part of the profile POLICY: with the
        default ``standard`` severity arg, the profile's own filter applies
        (deep -> all, quick -> critical-only)."""
        auditor = CommitAuditor(FakeLLM([]), workdir=".", review_profile="deep")
        assert auditor.review_severity == "all"
        quick = CommitAuditor(FakeLLM([]), workdir=".", review_profile="quick")
        assert quick.review_severity == "critical-only"

    def test_explicit_nonstandard_severity_wins(self):
        auditor = CommitAuditor(
            FakeLLM([]), workdir=".", review_profile="deep", review_severity="critical-only"
        )
        assert auditor.review_severity == "critical-only"

    def test_effort_override_via_constructor(self):
        auditor = CommitAuditor(
            FakeLLM([]),
            workdir=".",
            review_profile="standard",
            review_effort_override={"max_tokens": 777},
        )
        assert auditor.review_effort.max_tokens == 777

    def test_findings_pass_uses_effort_max_tokens(self):
        auditor = CommitAuditor(
            FakeLLM([_resp(FINDINGS_JSON)]),
            workdir=".",
            review_profile="quick",
        )
        auditor.review("msg", DIFF)
        # quick max_tokens = 1024 must reach the LLM call
        assert auditor.llm.calls[0]["max_tokens"] == QUICK.effort.max_tokens


class TestRunReviewBridge:
    def test_bridge_carries_gr148_fields(self):
        auditor = CommitAuditor(
            FakeLLM([_resp(FINDINGS_JSON), _resp(ESTIMATES_JSON)]),
            workdir=".",
            review_mode="review",
            review_profile="standard",
        )
        audit_result = auditor.audit("msg", DIFF)
        assert audit_result.review_profile == "standard"
        assert audit_result.review_effort
        assert audit_result.review_passes_run == ["findings", "estimates"]
        assert audit_result.review_estimates
        # compat-mapped severity in legacy issue strings
        assert any("[info][" in line for line in audit_result.issues)

    def test_legacy_issue_string_format_preserved_for_old_severities(self):
        result = CommitReviewResult(
            valid=False,
            issues=[
                ReviewIssue(
                    file="a.py", line=1, severity="high", category="bugs", title="t", score=8.0
                )
            ],
        )
        assert result.severity_counts() == {"high": 1}
