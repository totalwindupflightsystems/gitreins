"""
Unit tests for engine/judge.py — Judge orchestrator and pipeline dispatch.
axiom:trace work_item=GR-001 spec=specs/07-Judge-Orchestrator.md plan=.memory-bank/work-items/GR-001/plan.yaml
"""

from unittest import mock
from unittest.mock import patch

import pytest
from pathlib import Path

from engine.judge import Judge, JudgeResult
from engine.guard_manager import GuardResult, Tier1Result
from engine.evaluator import Verdict, VerdictItem
from engine.llm import LLMClient


def _mock_verdict_response(content: str) -> mock.MagicMock:
    """A ``chat()`` response the evaluator can cost and parse.

    The evaluator reads numeric usage fields off every response; a bare
    MagicMock answers comparisons with another MagicMock and the run dies in
    the loop, which would make a gate test pass for the wrong reason.
    """
    usage = mock.MagicMock(
        prompt_tokens=0,
        completion_tokens=0,
        cache_read_tokens=0,
        cache_write_tokens=0,
        total_tokens=0,
    )
    return mock.MagicMock(content=content, tool_calls=None, usage=usage)


# ── Phase 1-5-1: JudgeResult, legacy path ────────────────────────────────────


class TestJudgeResult:
    """Test JudgeResult dataclass and summary — step-1-5-1-1."""

    def test_judge_result_with_pipeline_result(self) -> None:
        """JudgeResult with pipeline_result → summary shows stage results."""
        pipeline = {
            "passed": True,
            "stages": {
                "tier1": {
                    "id": "tier1",
                    "passed": True,
                    "summary": "  ✓ secrets: ok\n  ✓ lint: ok",
                },
            },
        }
        result = JudgeResult(task_id="t1", passed=True, pipeline_result=pipeline)
        assert result.task_id == "t1"
        assert result.passed is True
        summary = result.summary
        assert "Stage tier1" in summary
        assert "PASS" in summary

    def test_judge_result_with_verdict_legacy(self) -> None:
        """JudgeResult with verdict (legacy) → summary shows verdict items."""
        items = [VerdictItem(criterion="c1", status="PASS", detail="ok")]
        verdict = Verdict(verdict="COMPLETE", items=items, summary="all good")
        result = JudgeResult(task_id="t1", passed=True, verdict=verdict)
        summary = result.summary
        assert "Tier 2" in summary or "Agentic Evaluator" in summary
        assert "COMPLETE" in summary

    def test_judge_result_both_pipeline_and_verdict(self) -> None:
        """JudgeResult with both pipeline and verdict shows both."""
        pipeline = {"passed": True, "stages": {}}
        items = [VerdictItem(criterion="c1", status="PASS", detail="ok")]
        verdict = Verdict(verdict="INCOMPLETE", items=items, summary="partial")
        result = JudgeResult(task_id="t1", passed=False, pipeline_result=pipeline, verdict=verdict)
        summary = result.summary
        assert "Judge Result" in summary
        assert "Overall" in summary

    def test_passed_true_shows_pass_check(self) -> None:
        """passed=True → 'PASS ✓' in summary."""
        result = JudgeResult(task_id="t1", passed=True)
        assert "PASS" in result.summary
        assert "✓" in result.summary

    def test_passed_false_shows_fail_cross(self) -> None:
        """passed=False → 'FAIL ✗' in summary."""
        result = JudgeResult(task_id="t1", passed=False)
        assert "FAIL" in result.summary
        assert "✗" in result.summary


class TestJudgeLegacyPath:
    """Test Judge._run_legacy — Tier 1 → Tier 2 — step-1-5-1-2."""

    def test_legacy_guards_pass_tier2_runs(self, judge: Judge, llm_client: LLMClient) -> None:
        """When guards pass, Tier 2 evaluator is called."""
        from engine.task_manager import Task

        task = Task(id="t1", title="Test", criteria=["c1"])

        # Mock guards to pass
        tier1_pass = Tier1Result(
            passed=True,
            results=[
                GuardResult("secrets", True, "ok"),
                GuardResult("lint", True, "ok"),
                GuardResult("tests", True, "ok"),
            ],
        )
        with patch.object(judge.guard_manager, "run_all", return_value=tier1_pass):
            # Mock evaluator to return COMPLETE
            verdict_json = '{"verdict":"COMPLETE","items":[{"criterion":"c1","status":"PASS","detail":"verified"}],"summary":"good"}'
            mock_usage = mock.MagicMock(
                prompt_tokens=0,
                completion_tokens=0,
                cache_read_tokens=0,
                cache_write_tokens=0,
                total_tokens=0,
            )
            mock_resp = mock.MagicMock(content=verdict_json, tool_calls=None, usage=mock_usage)
            with patch.object(llm_client, "chat", return_value=mock_resp):
                result = judge._run_legacy(task)
        assert result.passed is True
        assert result.verdict is not None
        assert result.verdict.verdict == "COMPLETE"
        # BUGFIX: tier1 and tier2 are now populated
        assert result.tier1 is not None
        assert result.tier1.passed is True
        assert result.tier2 is not None
        assert result.tier2.verdict == "COMPLETE"

    def test_legacy_guards_fail_tier2_skipped(self, judge: Judge) -> None:
        """When guards fail, Tier 2 is skipped, result.passed=False."""
        from engine.task_manager import Task

        task = Task(id="t1", title="Test", criteria=["c1"])

        # Mock guards to fail
        tier1_fail = Tier1Result(
            passed=False,
            results=[
                GuardResult("secrets", False, "key detected"),
            ],
        )
        with patch.object(judge.guard_manager, "run_all", return_value=tier1_fail):
            result = judge._run_legacy(task)
        assert result.passed is False
        assert result.pipeline_result == {}
        assert result.tier1 is not None
        assert result.tier1.passed is False
        assert result.tier2 is None  # Skipped because guards failed


class TestJudgeEvaluateTask:
    """Test Judge.evaluate_task() pipeline vs legacy dispatch."""

    def test_evaluate_task_uses_pipeline_when_config_has_stages(
        self, judge: Judge, tmp_workdir: str
    ) -> None:
        """evaluate_task runs pipeline when pipeline config has stages."""
        from engine.task_manager import Task

        # Write a config with pipeline stages
        import os
        import yaml

        config_dir = os.path.join(tmp_workdir, ".gitreins")
        os.makedirs(config_dir, exist_ok=True)
        config = {
            "pipeline": {
                "stages": [
                    {
                        "id": "tier1",
                        "parallel": True,
                        "on": ["pre-eval"],
                        "steps": [
                            {
                                "id": "secrets",
                                "type": "script",
                                "run": "echo clean",
                                "on_fail": "continue",
                            },
                        ],
                    },
                ],
            }
        }
        with open(os.path.join(config_dir, "config.yaml"), "w") as f:
            yaml.dump(config, f)

        task = Task(id="t1", title="Test", criteria=["c1"])
        result = judge.evaluate_task(task)
        assert result.passed is True
        assert result.pipeline_result is not None
        assert "stages" in result.pipeline_result

    def test_evaluate_task_falls_back_to_legacy_without_config(
        self, judge: Judge, llm_client: LLMClient
    ) -> None:
        """evaluate_task falls back to legacy when no pipeline config exists."""
        from engine.task_manager import Task

        task = Task(id="t1", title="Test", criteria=["c1"])

        tier1_pass = Tier1Result(
            passed=True,
            results=[
                GuardResult("secrets", True, "ok"),
                GuardResult("lint", True, "ok"),
                GuardResult("tests", True, "ok"),
            ],
        )
        with patch.object(judge.guard_manager, "run_all", return_value=tier1_pass):
            verdict_json = '{"verdict":"COMPLETE","items":[{"criterion":"c1","status":"PASS","detail":"ok"}],"summary":"done"}'
            mock_usage = mock.MagicMock(
                prompt_tokens=0,
                completion_tokens=0,
                cache_read_tokens=0,
                cache_write_tokens=0,
                total_tokens=0,
            )
            mock_resp = mock.MagicMock(content=verdict_json, tool_calls=None, usage=mock_usage)
            with patch.object(llm_client, "chat", return_value=mock_resp):
                result = judge.evaluate_task(task)
        assert result.passed is True

    def test_empty_items_complete_is_not_a_pass_through_evaluate_task(
        self, judge: Judge, llm_client: LLMClient, tmp_workdir: str
    ) -> None:
        """GR-167 AC3: COMPLETE with no items does not pass evaluate_task.

        76 of 297 stored passes (25.6%) carried zero per-criterion items:
        `passed` was `verdict == "COMPLETE"` and nothing asserted that any
        criterion had been graded.
        """
        import os

        import yaml

        from engine.task_manager import Task

        config_dir = os.path.join(tmp_workdir, ".gitreins")
        os.makedirs(config_dir, exist_ok=True)
        with open(os.path.join(config_dir, "config.yaml"), "w") as f:
            yaml.dump(
                {
                    "pipeline": {
                        "stages": [
                            {
                                "id": "tier1",
                                "parallel": True,
                                "on": ["pre-eval"],
                                "steps": [
                                    {
                                        "id": "secrets",
                                        "type": "script",
                                        "run": "echo clean",
                                        "on_fail": "continue",
                                    }
                                ],
                            },
                            {
                                "id": "tier2",
                                "type": "ai_eval",
                                "on": ["pre-eval"],
                                "condition": "true",
                                "max_iterations": 2,
                            },
                        ]
                    }
                },
                f,
            )

        task = Task(id="t1", title="Test", criteria=["c1"])
        verdict_json = '{"verdict":"COMPLETE","items":[],"summary":"done"}'
        with patch.object(llm_client, "chat", return_value=_mock_verdict_response(verdict_json)):
            result = judge.evaluate_task(task)

        assert result.passed is False
        assert result.pipeline_result["stages"]["tier2"]["passed"] is False

    def test_legacy_path_refuses_complete_without_items(
        self, judge: Judge, llm_client: LLMClient
    ) -> None:
        """GR-167: the legacy call site (`_run_legacy`) applies the same rule."""
        from engine.task_manager import Task

        task = Task(id="t1", title="Test", criteria=["c1"])
        tier1_pass = Tier1Result(passed=True, results=[GuardResult("tests", True, "ok")])
        judge._pass_on_error = False
        with patch.object(judge.guard_manager, "run_all", return_value=tier1_pass):
            verdict_json = '{"verdict":"COMPLETE","items":[],"summary":"done"}'
            with patch.object(
                llm_client, "chat", return_value=_mock_verdict_response(verdict_json)
            ):
                result = judge._run_legacy(task)

        assert result.passed is False
        assert result.tier2 is not None and result.tier2.verdict == "COMPLETE"
        # The reason is visible where a reader looks: the verdict summary.
        assert "Coverage FAILED" in result.tier2.summary

    def test_legacy_path_refuses_a_complete_that_leaves_a_criterion_ungraded(
        self, judge: Judge, llm_client: LLMClient
    ) -> None:
        """GR-167 AC1: an item list that does not cover the task fails."""
        from engine.task_manager import Task

        task = Task(id="t1", title="Test", criteria=["c1 shipped", "c2 documented"])
        tier1_pass = Tier1Result(passed=True, results=[GuardResult("tests", True, "ok")])
        judge._pass_on_error = False
        with patch.object(judge.guard_manager, "run_all", return_value=tier1_pass):
            verdict_json = (
                '{"verdict":"COMPLETE","items":'
                '[{"criterion":"c1 shipped","status":"PASS","detail":"ok"}],'
                '"summary":"done"}'
            )
            with patch.object(
                llm_client, "chat", return_value=_mock_verdict_response(verdict_json)
            ):
                result = judge._run_legacy(task)

        assert result.passed is False
        # The ungraded criterion is named, not just counted.
        assert result.tier2 is not None
        assert "c2 documented" in result.tier2.summary

    def test_legacy_path_still_passes_a_fully_graded_complete(
        self, judge: Judge, llm_client: LLMClient
    ) -> None:
        """Positive control: full coverage still passes (nothing was weakened)."""
        from engine.task_manager import Task

        task = Task(id="t1", title="Test", criteria=["c1 shipped", "c2 documented"])
        tier1_pass = Tier1Result(passed=True, results=[GuardResult("tests", True, "ok")])
        judge._pass_on_error = False
        with patch.object(judge.guard_manager, "run_all", return_value=tier1_pass):
            verdict_json = (
                '{"verdict":"COMPLETE","items":['
                '{"criterion":"c1 shipped","status":"PASS","detail":"ok"},'
                '{"criterion":"c2 documented","status":"PASS","detail":"ok"}],'
                '"summary":"done"}'
            )
            with patch.object(
                llm_client, "chat", return_value=_mock_verdict_response(verdict_json)
            ):
                result = judge._run_legacy(task)

        assert result.passed is True

    def test_run_precommit_runs_pipeline(self, judge: Judge, tmp_workdir: str) -> None:
        """run_precommit runs pipeline with trigger='pre-commit'."""
        import os
        import yaml

        config_dir = os.path.join(tmp_workdir, ".gitreins")
        os.makedirs(config_dir, exist_ok=True)
        config = {
            "pipeline": {
                "stages": [
                    {
                        "id": "tier1",
                        "parallel": True,
                        "on": ["pre-commit"],
                        "steps": [
                            {
                                "id": "secrets",
                                "type": "script",
                                "run": "echo clean",
                                "on_fail": "continue",
                            },
                        ],
                    },
                ],
            }
        }
        with open(os.path.join(config_dir, "config.yaml"), "w") as f:
            yaml.dump(config, f)

        result = judge.run_precommit()
        assert result is True


class TestVerdictCoverage:
    """GR-167: ``engine.gate`` is the one authority on what a verdict covers."""

    def test_exact_text_beats_a_prefix(self) -> None:
        """'c1' must grade 'c1', not the item that merely starts with it."""
        from engine.gate import check_coverage

        verdict = Verdict(
            verdict="COMPLETE",
            items=[
                VerdictItem("c1 extended criterion text", "FAIL", "no"),
                VerdictItem("c1", "PASS", "ok"),
            ],
        )
        coverage = check_coverage(verdict, ["c1"])
        assert coverage.graded == ("c1",)
        assert coverage.failing == ()

    def test_interior_elision_in_a_nine_of_nine_pass_verdict_is_covered(self) -> None:
        """A long token echoed as ``...`` must not create a false red (AUG-164)."""
        from engine.gate import check_coverage, verdict_is_passing

        criteria = [
            (
                f"AC{i} - run the proof with PATH=/home/kara/auger/.venv/bin:"
                "/home/kara/.local/bin:/usr/local/bin:/usr/bin:/bin and assert the result"
            )
            for i in range(1, 7)
        ]
        echoed = [
            criterion.replace(
                "/home/kara/auger/.venv/bin:/home/kara/.local/bin:/usr/local/bin:/usr/bin:/bin",
                "...",
            )
            for criterion in criteria
        ]
        criteria.extend(
            ["derived criterion one", "derived criterion two", "derived criterion three"]
        )
        verdict = Verdict(
            verdict="COMPLETE",
            items=[
                *[VerdictItem(criterion, "PASS", "proof") for criterion in echoed],
                VerdictItem("derived criterion one", "PASS", "proof"),
                VerdictItem("derived criterion two", "PASS", "proof"),
                VerdictItem("derived criterion three", "PASS", "proof"),
            ],
        )

        coverage = check_coverage(verdict, criteria)
        assert len(verdict.items) == 9
        assert len(coverage.graded) == 9
        assert coverage.ungraded == ()
        assert verdict_is_passing(verdict, criteria) is True

    def test_persisted_pipeline_shape_uses_nested_complete_outcome(self) -> None:
        """The persisted 9/9 record has its COMPLETE word under tier2 (AUG-164)."""
        from engine.gate import check_coverage, verdict_is_passing

        criterion = (
            "a long proof token /home/kara/auger/.venv/bin:/home/kara/.local/bin/xyz "
            "and assert the result"
        )
        record = {
            "stages": {
                "tier2": {
                    "steps": [
                        {
                            "data": {
                                "verdict": "COMPLETE",
                                "items": [
                                    {
                                        "criterion": criterion.replace(
                                            "/home/kara/auger/.venv/bin:/home/kara/.local/bin/xyz",
                                            "...",
                                        ),
                                        "status": "PASS",
                                    }
                                ],
                            }
                        }
                    ]
                }
            },
        }
        coverage = check_coverage(record, [criterion])
        assert coverage.ungraded == ()
        assert verdict_is_passing(record, [criterion]) is True

    def test_interior_elision_does_not_make_a_fail_or_short_prefix_pass(self) -> None:
        """A FAIL and a genuinely truncated echo remain negative controls (AUG-164)."""
        from engine.gate import check_coverage, verdict_is_passing

        criterion = (
            "AC1 - run PATH=/home/kara/auger/.venv/bin:/home/kara/.local/bin:"
            "/usr/local/bin:/usr/bin:/bin and assert the result"
        )
        elided = criterion.replace(
            "/home/kara/auger/.venv/bin:/home/kara/.local/bin:/usr/local/bin:/usr/bin:/bin",
            "...",
        )
        failing = Verdict(
            verdict="COMPLETE",
            items=[VerdictItem(elided, "FAIL", "failed proof")],
        )
        coverage = check_coverage(failing, [criterion])
        assert coverage.graded == (criterion,)
        assert coverage.failing == (criterion,)
        assert verdict_is_passing(failing, [criterion]) is False

        truncated = Verdict(
            verdict="COMPLETE",
            items=[VerdictItem(criterion[:30], "PASS", "proof")],
        )
        short_coverage = check_coverage(truncated, [criterion])
        assert short_coverage.ungraded == (criterion,)
        assert verdict_is_passing(truncated, [criterion]) is False

    def test_one_item_cannot_grade_two_criteria(self) -> None:
        """Coverage is per criterion: a duplicated item covers one of them."""
        from engine.gate import verdict_is_passing

        verdict = Verdict(
            verdict="COMPLETE",
            items=[VerdictItem("shared criterion text here", "PASS", "ok")],
        )
        assert verdict_is_passing(verdict, ["shared criterion text here"]) is True
        assert (
            verdict_is_passing(
                verdict, ["shared criterion text here", "shared criterion text here"]
            )
            is False
        )

    def test_a_fail_item_does_not_complete(self) -> None:
        """COMPLETE beside a FAIL item is a contradiction, not a pass."""
        from engine.gate import verdict_is_passing

        verdict = Verdict(verdict="COMPLETE", items=[VerdictItem("c1 shipped", "FAIL", "missing")])
        assert verdict_is_passing(verdict, ["c1 shipped"]) is False

    def test_the_persisted_dict_shape_reads_the_same(self) -> None:
        """A verdict.json dict and a live Verdict answer identically."""
        from engine.gate import verdict_is_passing

        record = {
            "verdict": "COMPLETE",
            "items": [{"criterion": "c1 shipped", "status": "PASS", "detail": "ok"}],
        }
        assert verdict_is_passing(record, ["c1 shipped"]) is True
        assert verdict_is_passing(record, ["c1 shipped", "c2 documented"]) is False

    def test_incomplete_label_with_complete_pass_evidence_passes(self) -> None:
        """A stale INCOMPLETE label cannot defeat complete PASS evidence."""
        from engine.gate import verdict_is_passing

        verdict = Verdict(verdict="INCOMPLETE", items=[VerdictItem("c1 shipped", "PASS", "ok")])
        assert verdict_is_passing(verdict, ["c1 shipped"]) is True


class TestNotVerifiableRouting:
    """GR-178: what the judge could NOT verify is routed to a human.

    A criterion that code review alone cannot settle (a browser check, an
    external API, live data) must be a claim of its own — NOT_VERIFIABLE —
    never a PASS by absence of complaint and never a manufactured FAIL.
    """

    # The acceptance criterion's own example: a UI criterion that needs a
    # browser. Code review can read the nav component; it cannot see it render.
    BROWSER_CRITERION = "the settings page renders the new nav in a real browser"

    def _browser_verdict(self) -> Verdict:
        return Verdict(
            verdict="COMPLETE",
            items=[
                VerdictItem(
                    self.BROWSER_CRITERION,
                    "NOT_VERIFIABLE",
                    "Read settings/nav.tsx; rendering can only be confirmed in a browser",
                    not_verifiable_reason=(
                        "Human check: open the built site in a browser and confirm the nav renders"
                    ),
                ),
            ],
        )

    def test_a_browser_check_criterion_is_not_counted_as_passed(self) -> None:
        """A NOT_VERIFIABLE item is never a PASS in the coverage gate."""
        from engine.gate import check_coverage, verdict_is_passing

        verdict = self._browser_verdict()
        assert verdict_is_passing(verdict, [self.BROWSER_CRITERION]) is False
        coverage = check_coverage(verdict, [self.BROWSER_CRITERION])
        # It WAS graded — the judge looked — but it landed in its own bucket,
        # not in PASS and not in a manufactured FAIL.
        assert coverage.graded == (self.BROWSER_CRITERION,)
        assert coverage.not_verifiable == (self.BROWSER_CRITERION,)
        assert coverage.failing == ()
        assert coverage.complete is False

    def test_not_verifiable_listed_separately_from_failing(self) -> None:
        """'Cannot be verified from here' is not 'verified broken'."""
        from engine.gate import check_coverage

        verdict = Verdict(
            verdict="INCOMPLETE",
            items=[
                VerdictItem(self.BROWSER_CRITERION, "NOT_VERIFIABLE", "needs a browser"),
                VerdictItem("c2 broken", "FAIL", "returns 500"),
            ],
        )
        coverage = check_coverage(verdict, [self.BROWSER_CRITERION, "c2 broken"])
        assert coverage.not_verifiable == (self.BROWSER_CRITERION,)
        assert coverage.failing == ("c2 broken",)
        # The summary line names both, in separate buckets.
        assert "not verifiable" in coverage.reason
        assert self.BROWSER_CRITERION in coverage.reason
        assert "not passing" in coverage.reason

    def test_the_persisted_dict_shape_reads_the_same(self) -> None:
        """A verdict.json dict with NOT_VERIFIABLE answers like a live verdict."""
        from engine.gate import check_coverage, verdict_is_passing

        record = {
            "verdict": "COMPLETE",
            "items": [
                {
                    "criterion": self.BROWSER_CRITERION,
                    "status": "NOT_VERIFIABLE",
                    "detail": "needs a browser",
                    "not_verifiable_reason": "human check",
                }
            ],
        }
        assert verdict_is_passing(record, [self.BROWSER_CRITERION]) is False
        assert check_coverage(record, [self.BROWSER_CRITERION]).not_verifiable == (
            self.BROWSER_CRITERION,
        )

    def test_merge_recommendation_degrades_to_needs_review(self) -> None:
        """Any NOT_VERIFIABLE item degrades the merge recommendation."""
        from engine.gate import (
            MERGE_RECOMMENDATION_OK,
            MERGE_RECOMMENDATION_REVIEW,
            merge_recommendation,
        )

        criteria = [self.BROWSER_CRITERION]
        assert (
            merge_recommendation(self._browser_verdict(), criteria) == MERGE_RECOMMENDATION_REVIEW
        )
        # All-PASS verdict over the same criteria: no concerns.
        clean = Verdict(
            verdict="COMPLETE",
            items=[VerdictItem(self.BROWSER_CRITERION, "PASS", "verified by hand")],
        )
        assert merge_recommendation(clean, criteria) == MERGE_RECOMMENDATION_OK

    def test_merge_recommendation_three_values(self) -> None:
        """FAIL → changes_required outranks, NOT_VERIFIABLE → needs_review."""
        from engine.gate import (
            MERGE_RECOMMENDATION_CHANGES,
            MERGE_RECOMMENDATION_OK,
            MERGE_RECOMMENDATION_REVIEW,
            merge_recommendation,
        )

        assert merge_recommendation(self._browser_verdict()) == MERGE_RECOMMENDATION_REVIEW
        failing = Verdict(
            verdict="INCOMPLETE",
            items=[
                VerdictItem(self.BROWSER_CRITERION, "NOT_VERIFIABLE", "needs a browser"),
                VerdictItem("c2 broken", "FAIL", "returns 500"),
            ],
        )
        assert merge_recommendation(failing) == MERGE_RECOMMENDATION_CHANGES
        clean = Verdict(verdict="COMPLETE", items=[VerdictItem("c1 ok", "PASS", "evidence")])
        assert merge_recommendation(clean) == MERGE_RECOMMENDATION_OK

    def test_merge_gate_still_keys_on_pass_class(self) -> None:
        """The recommendation is advisory: the gate never reads it."""
        from engine.gate import verdict_is_passing

        # Even a needs_review verdict answers through the same coverage
        # authority — no new bypass exists.
        verdict = self._browser_verdict()
        assert verdict_is_passing(verdict, [self.BROWSER_CRITERION]) is False

    def test_not_verifiable_listed_separately_in_judge_summary(self) -> None:
        """The JudgeResult summary carries its own human-routing section."""
        result = JudgeResult(
            task_id="GR-178",
            verdict=self._browser_verdict(),
            passed=False,
        )
        summary = result.summary
        assert "Could NOT verify — needs human review" in summary
        assert self.BROWSER_CRITERION in summary
        assert "open the built site in a browser" in summary

    def test_parser_keeps_not_verifiable_instead_of_coercing(self) -> None:
        """A model's NOT_VERIFIABLE survives _parse_verdict with its reason."""
        from engine.evaluator import AgenticEvaluator

        content = (
            '{"verdict":"INCOMPLETE","items":[{"criterion":"'
            + self.BROWSER_CRITERION
            + '","status":"NOT_VERIFIABLE","detail":"needs a browser",'
            + '"not_verifiable_reason":"human browser check"}],"summary":"s"}'
        )
        evaluator = AgenticEvaluator.__new__(AgenticEvaluator)
        evaluator._sandbox = {}
        verdict = evaluator._parse_verdict(content)
        assert verdict.items[0].status == "NOT_VERIFIABLE"
        assert verdict.items[0].not_verifiable_reason == "human browser check"

    def test_complete_with_not_verifiable_is_forced_incomplete(self) -> None:
        """A COMPLETE carrying a NOT_VERIFIABLE item cannot stand."""
        from engine.evaluator import AgenticEvaluator

        content = (
            '{"verdict":"COMPLETE","items":[{"criterion":"'
            + self.BROWSER_CRITERION
            + '","status":"NOT_VERIFIABLE","detail":"needs a browser"}],"summary":"s"}'
        )
        evaluator = AgenticEvaluator.__new__(AgenticEvaluator)
        evaluator._sandbox = {}
        verdict = evaluator._parse_verdict(content)
        assert verdict.verdict == "INCOMPLETE"
        assert verdict.items[0].status == "NOT_VERIFIABLE"


class TestJudgeInit:
    """Test Judge initialization."""

    def test_judge_constructor_creates_guard_manager(self, judge: Judge) -> None:
        """Judge constructor creates a GuardManager."""
        assert judge.guard_manager is not None

    def test_judge_constructor_accepts_guard_config(
        self, tmp_workdir: str, llm_client: LLMClient
    ) -> None:
        """Judge constructor accepts guard_config dict."""
        judge = Judge(llm_client, tmp_workdir, guard_config={"guards": {"secrets": False}})
        assert judge.guard_manager._enabled["secrets"] is False


class TestExtendedJudge:
    """Extended coverage for Judge module."""

    def test_judge_result_empty_pipeline(self) -> None:
        """JudgeResult with empty pipeline_result shows Overall."""
        result = JudgeResult(task_id="t1")
        assert result.task_id == "t1"
        assert result.passed is False
        summary = result.summary
        assert "Overall" in summary

    def test_judge_result_no_pipeline_no_verdict(self) -> None:
        """JudgeResult without pipeline or verdict still produces summary."""
        result = JudgeResult(task_id="t1", passed=False)
        summary = result.summary
        assert "Judge Result: t1" in summary
        assert "FAIL" in summary

    def test_evaluate_task_pipeline_exception_returns_error(
        self, judge: Judge, tmp_workdir: str, llm_client: LLMClient
    ) -> None:
        """evaluate_task catches pipeline exception and returns error in result."""
        from engine.task_manager import Task
        import os
        import yaml

        config_dir = os.path.join(tmp_workdir, ".gitreins")
        os.makedirs(config_dir, exist_ok=True)
        config = {
            "pipeline": {
                "stages": [
                    {
                        "id": "bad",
                        "parallel": True,
                        "on": ["pre-eval"],
                        "steps": [{"id": "x", "type": "script", "run": "exit 1"}],
                    },
                ],
            }
        }
        with open(os.path.join(config_dir, "config.yaml"), "w") as f:
            yaml.dump(config, f)

        task = Task(id="t1", title="Test", criteria=["c1"])
        result = judge.evaluate_task(task)
        assert result.pipeline_result is not None

    def test_judge_result_summary_with_failed_stage(self) -> None:
        """JudgeResult summary shows FAIL for failed stages."""
        pipeline = {
            "passed": False,
            "stages": {
                "tier1": {
                    "id": "tier1",
                    "passed": False,
                    "summary": "  ✗ secrets: failed",
                },
            },
        }
        result = JudgeResult(task_id="t2", passed=False, pipeline_result=pipeline)
        summary = result.summary
        assert "FAIL" in summary
        assert "tier1" in summary

    def test_degraded_tier1_is_first_summary_line(self) -> None:
        result = JudgeResult(
            task_id="t-degraded",
            passed=True,
            pipeline_result={
                "passed": True,
                "stages": {
                    "tier1": {
                        "passed": True,
                        "degraded": True,
                        "summary": "  ~ guard: skipped (no staged files)",
                    },
                    "tier2": {"passed": True, "summary": "COMPLETE"},
                },
            },
        )
        assert result.summary.splitlines()[0] == "Stage tier1: PASS (DEGRADED - skipped checks)"

    def test_judge_result_tier2_verdict_items_shown(self) -> None:
        """JudgeResult summary shows verdict items from Tier 2."""
        items = [
            VerdictItem(criterion="c1", status="PASS", detail="verified"),
            VerdictItem(criterion="c2", status="FAIL", detail="missing"),
        ]
        verdict = Verdict(verdict="INCOMPLETE", items=items, summary="partial")
        result = JudgeResult(task_id="t3", passed=False, verdict=verdict)
        summary = result.summary
        assert "c1" in summary
        assert "c2" in summary
        assert "INCOMPLETE" in summary

    def test_judge_result_stores_tier1_and_tier2(self) -> None:
        """BUGFIX: JudgeResult.tier1 and .tier2 are populated by _run_legacy."""
        from engine.judge import JudgeResult

        verdict = Verdict(
            verdict="COMPLETE",
            items=[VerdictItem(criterion="c1", status="PASS", detail="ok")],
            summary="all good",
        )
        tier1 = Tier1Result(
            passed=True, results=[GuardResult(name="secrets", passed=True, output="clean")]
        )

        result = JudgeResult(task_id="t4", passed=True, tier1=tier1, tier2=verdict)
        assert result.tier1 is not None
        assert result.tier1.passed is True
        assert result.tier2 is not None
        assert result.tier2.verdict == "COMPLETE"

    def test_tier1_none_safe_access(self) -> None:
        """BUGFIX: tier1 is None safe (e.g. pipeline path)."""
        result = JudgeResult(task_id="t5", passed=True)
        assert result.tier1 is None
        assert result.tier2 is None
        # The MCP handler pattern: result.tier1.passed if result.tier1 else None
        tier1_passed = result.tier1.passed if result.tier1 else None
        assert tier1_passed is None


class TestLspDiagnosticsParsing:
    """Test Judge._parse_lsp_output and _extract_lsp_diagnostics — step GR-NNN."""

    def test_parse_lsp_output_empty(self, judge: Judge) -> None:
        """Empty output returns empty list."""
        result = judge._parse_lsp_output("")
        assert result == []

    def test_parse_lsp_output_clean(self, judge: Judge) -> None:
        """Output with only clean lines returns empty list."""
        output = "  pylsp — clean"
        result = judge._parse_lsp_output(output)
        assert result == []

    def test_parse_lsp_output_error_diag(self, judge: Judge) -> None:
        """Parses '✗' prefixed line as error severity."""
        output = "  ✗ /path/to/file.py:5 [pylsp] Undefined variable 'x'"
        result = judge._parse_lsp_output(output)
        assert len(result) == 1
        d = result[0]
        assert d["file"] == "/path/to/file.py"
        assert d["line"] == 5
        assert d["severity"] == "error"
        assert d["message"] == "Undefined variable 'x'"
        assert d["tool"] == "pylsp"

    def test_parse_lsp_output_warning_diag(self, judge: Judge) -> None:
        """Parses '⚠' prefixed line as warning severity."""
        output = "  ⚠ /path/to/file.py:10 [pylsp] unused variable 'y'"
        result = judge._parse_lsp_output(output)
        assert len(result) == 1
        assert result[0]["severity"] == "warning"
        assert result[0]["line"] == 10
        assert result[0]["message"] == "unused variable 'y'"

    def test_parse_lsp_output_multiple(self, judge: Judge) -> None:
        """Multiple diagnostic lines are all parsed."""
        output = (
            "  ✗ /path/to/file.py:5 [pylsp] Undefined variable 'x'\n"
            "  ⚠ /path/to/file.py:8 [pylsp] unused import os\n"
            "  ✗ /path/to/file.py:12 [pylsp] syntax error\n"
        )
        result = judge._parse_lsp_output(output)
        assert len(result) == 3
        assert result[0]["severity"] == "error"
        assert result[1]["severity"] == "warning"
        assert result[2]["severity"] == "error"

    def test_parse_lsp_output_mixed_content(self, judge: Judge) -> None:
        """Non-diagnostic lines (status, blank) are ignored."""
        output = (
            "  pylsp — clean\n"
            "  No staged files for LSP tool 'pylsp'\n"
            "  ✗ /path/to/file.py:3 [pylsp] undefined name 'foo'\n"
            ""
        )
        result = judge._parse_lsp_output(output)
        assert len(result) == 1
        assert result[0]["line"] == 3
        assert result[0]["message"] == "undefined name 'foo'"

    def test_extract_lsp_diagnostics_found(self, judge: Judge) -> None:
        """_extract_lsp_diagnostics finds the lsp GuardResult and parses it."""
        from engine.guard_manager import Tier1Result, GuardResult

        lsp_result = GuardResult(
            name="lsp", passed=False, output="  ✗ /path/to/file.py:5 [pylsp] Undefined variable 'x'"
        )
        tier1 = Tier1Result(
            passed=False,
            results=[
                GuardResult("secrets", True, "ok"),
                lsp_result,
                GuardResult("lint", True, "ok"),
            ],
        )
        diags = judge._extract_lsp_diagnostics(tier1)
        assert len(diags) == 1
        assert diags[0]["file"] == "/path/to/file.py"
        assert diags[0]["severity"] == "error"

    def test_extract_lsp_diagnostics_not_present(self, judge: Judge) -> None:
        """No lsp guard → empty list, no crash."""
        from engine.guard_manager import Tier1Result, GuardResult

        tier1 = Tier1Result(
            passed=True,
            results=[
                GuardResult("secrets", True, "ok"),
                GuardResult("lint", True, "ok"),
            ],
        )
        diags = judge._extract_lsp_diagnostics(tier1)
        assert diags == []

    def test_extract_lsp_diagnostics_empty_output(self, judge: Judge) -> None:
        """LSP guard with empty output → empty list."""
        from engine.guard_manager import Tier1Result, GuardResult

        tier1 = Tier1Result(
            passed=True,
            results=[
                GuardResult("lsp", True, "  pylsp — clean"),
            ],
        )
        diags = judge._extract_lsp_diagnostics(tier1)
        assert diags == []


# ── DF-006: judge_result_to_dict — legacy + pipeline shapes ──────────────────


class TestJudgeResultToDict:
    """judge_result_to_dict covers both result shapes (legacy tier2 and
    pipeline-path verdicts buried in pipeline_result stages)."""

    def _legacy_result(self, passed: object = True) -> object:
        from engine.judge import judge_result_to_dict

        tier1 = Tier1Result(passed=True, results=[])
        tier2 = Verdict(
            verdict="COMPLETE",
            items=[VerdictItem("c1", "PASS", "ok")],
            summary="all good",
        )
        result = JudgeResult(task_id="t1", passed=passed, tier1=tier1, tier2=tier2, verdict=tier2)
        return judge_result_to_dict("t1", "/wd", result)

    def _pipeline_result(self, passed: object = True) -> object:
        from engine.judge import judge_result_to_dict

        tier1 = Tier1Result(passed=True, results=[])
        result = JudgeResult(
            task_id="t1",
            passed=passed,
            tier1=tier1,
            pipeline_result={
                "passed": passed,
                "stages": {
                    "tier1": {"passed": True, "steps": []},
                    "tier2": {
                        "passed": True,
                        "steps": [
                            {
                                "id": "eval",
                                "type": "ai_eval",
                                "passed": True,
                                "output": "COMPLETE\n  ✓ c1: ok\nall good",
                                "data": {
                                    "verdict": "COMPLETE",
                                    "items": [
                                        {"criterion": "c1", "status": "PASS", "detail": "ok"}
                                    ],
                                    "summary": "all good",
                                },
                            }
                        ],
                    },
                },
            },
        )
        return judge_result_to_dict("t1", "/wd", result)

    def test_legacy_shape(self) -> None:
        d = self._legacy_result()
        assert d["passed"] is True
        assert d["tier1_passed"] is True
        assert d["verdict"] == "COMPLETE"
        assert d["items"][0]["criterion"] == "c1"
        assert d["summary"] == "all good"

    def test_pipeline_shape(self) -> None:
        d = self._pipeline_result()
        assert d["passed"] is True
        assert d["tier1_passed"] is True
        assert d["verdict"] == "COMPLETE"
        assert d["items"][0]["criterion"] == "c1"
        assert d["items"][0]["status"] == "PASS"
        assert d["summary"] == "all good"

    def test_pipeline_shape_no_ai_eval_data(self) -> None:
        from engine.judge import judge_result_to_dict

        tier1 = Tier1Result(passed=True, results=[])
        result = JudgeResult(
            task_id="t1",
            passed=True,
            tier1=tier1,
            pipeline_result={"passed": True, "stages": {}},
        )
        d = judge_result_to_dict("t1", "/wd", result)
        assert d["passed"] is True
        assert "verdict" not in d
        assert "items" not in d

    def test_provenance_is_stamped_when_a_tier2_verdict_exists(self) -> None:
        """REVIEW-GITREINS-030: the status dict names the judge's client."""
        from engine.judge import _judge_provenance, judge_result_to_dict

        tier1 = Tier1Result(passed=True, results=[])
        tier2 = Verdict(
            verdict="COMPLETE",
            items=[VerdictItem("c1", "PASS", "ok")],
            summary="all good",
        )
        result = JudgeResult(
            task_id="t1",
            passed=True,
            tier1=tier1,
            tier2=tier2,
            verdict=tier2,
            provenance={
                "model": "deepseek-v4-flash",
                "base_url_source": "GITREINS_LLM_BASE_URL",
                "api_key_source": "GITREINS_LLM_API_KEY",
            },
        )

        d = judge_result_to_dict("t1", "/wd", result)

        assert d["model"] == "deepseek-v4-flash"
        assert d["base_url_source"] == "GITREINS_LLM_BASE_URL"
        assert d["api_key_source"] == "GITREINS_LLM_API_KEY"
        # The item shape is the one consumers already parse.
        assert d["items"] == [{"criterion": "c1", "status": "PASS", "detail": "ok"}]

        # A client with no identity attributes yields an EMPTY dict, so the
        # key never appears at all (absent, not null).
        assert _judge_provenance(object()) == {}

    def test_provenance_is_absent_without_a_client_identity(self) -> None:
        from engine.judge import judge_result_to_dict

        tier1 = Tier1Result(passed=True, results=[])
        tier2 = Verdict(verdict="COMPLETE", items=[], summary="ok")
        result = JudgeResult(task_id="t1", passed=True, tier1=tier1, tier2=tier2, verdict=tier2)

        d = judge_result_to_dict("t1", "/wd", result)

        for key in ("model", "base_url_source", "api_key_source", "provenance"):
            assert key not in d


# ── GR-179: graded failure-mode block ────────────────────────────────────────


class TestFailureModeBlock:
    """GR-179: every verdict can carry up to 3 graded failure modes.

    Each entry names what fails, where (file:function), the realistic trigger,
    what would detect it (detected_by) and whether a test in this change
    covers it (covered_in_this_pr). An empty block is normal and unpenalised;
    an entry whose detected_by claims a test that no tool result of the run
    shows running degrades the summary instead of silently passing.
    """

    EMPTY_BLOCK_CONTENT = (
        '{"verdict":"COMPLETE","items":[{"criterion":"c1","status":"PASS","detail":"ok"}],'
        '"summary":"all good","failure_modes":[]}'
    )

    BACKED_CONTENT = (
        '{"verdict":"COMPLETE","items":[{"criterion":"c1","status":"PASS","detail":"ok"}],'
        '"summary":"all good","failure_modes":[{"what":"import error when plugin dir missing",'
        '"where":"engine/plugins.py:load","trigger":"worktree without an empty plugins/ dir",'
        '"detected_by":"tests/test_judge.py::test_failure_modes_empty_is_unpenalised",'
        '"covered_in_this_pr":true}]}'
    )

    UNBACKED_CONTENT = (
        '{"verdict":"COMPLETE","items":[{"criterion":"c1","status":"PASS","detail":"ok"}],'
        '"summary":"all good","failure_modes":[{"what":"import error when plugin dir missing",'
        '"where":"engine/plugins.py:load","trigger":"worktree without an empty plugins/ dir",'
        '"detected_by":"tests/test_never_ran.py::test_absent","covered_in_this_pr":true}]}'
    )

    def _parse(self, content: str, run_record: list[str]) -> Verdict:
        """Parse *content* with the run's tool record set to *run_record*."""
        import json

        from engine.evaluator import AgenticEvaluator

        evaluator = AgenticEvaluator.__new__(AgenticEvaluator)
        evaluator._sandbox = {}
        evaluator._tool_record = [json.dumps(r) if isinstance(r, dict) else r for r in run_record]
        return evaluator._parse_verdict(content)

    def test_failure_modes_empty_is_unpenalised(self) -> None:
        """Arms 4+5: an empty block parses to [] and the summary stays byte-identical.

        A verdict that OMITS the block entirely must parse to the same empty
        list (R1) — the block is additive, never required.
        """
        verdict = self._parse(self.EMPTY_BLOCK_CONTENT, run_record=[])
        assert verdict.verdict == "COMPLETE"
        assert verdict.failure_modes == []
        assert verdict.summary == "all good"  # no flags — unpenalised

        omitted = self._parse('{"verdict":"COMPLETE","items":[],"summary":"s"}', run_record=[])
        assert omitted.failure_modes == []
        assert omitted.summary == "s"

    def test_failure_modes_populated_backed_entry_is_unpenalised(self) -> None:
        """Arm 6: populated entry whose detected_by names a test that DID run.

        The run record holds the run_command the evaluator actually executed
        (tool + arguments + result); the cited node id appears in it, so the
        citation is backed and the summary keeps its original text.
        """
        record_entry = {
            "tool": "run_command",
            "arguments": {
                "cmd": ".venv/bin/python -m pytest "
                "tests/test_judge.py::test_failure_modes_empty_is_unpenalised -q"
            },
            "result": {"exit_code": 0, "output": "1 passed in 0.02s"},
        }
        verdict = self._parse(self.BACKED_CONTENT, run_record=[record_entry])
        assert verdict.verdict == "COMPLETE"
        assert len(verdict.failure_modes) == 1
        fm = verdict.failure_modes[0]
        assert fm.what == "import error when plugin dir missing"
        assert fm.where == "engine/plugins.py:load"
        assert fm.trigger == "worktree without an empty plugins/ dir"
        assert fm.detected_by == "tests/test_judge.py::test_failure_modes_empty_is_unpenalised"
        assert fm.covered_in_this_pr is True
        assert verdict.summary == "all good"  # backed citation → no degradation

    def test_failure_modes_unbacked_citation_degrades_summary(self) -> None:
        """Arm 7: detected_by names a test with no tool result behind it.

        The claim degrades the SUMMARY (the uncovered failure path is named
        there) without flipping the verdict — the parse is honest, the
        unsupported detection claim is not.
        """
        verdict = self._parse(self.UNBACKED_CONTENT, run_record=[])
        assert verdict.verdict == "COMPLETE"  # degradation ≠ verdict flip
        assert "uncovered failure paths" in verdict.summary
        assert "tests/test_never_ran.py::test_absent" in verdict.summary
        assert "all good" in verdict.summary  # original summary survives, degraded

    def test_failure_modes_empty_detected_by_is_flagged_not_rejected(self) -> None:
        """Arm 2: an entry with no detector is allowed — but the summary flags it."""
        content = (
            '{"verdict":"INCOMPLETE","items":[{"criterion":"c1","status":"FAIL","detail":"x"}],'
            '"summary":"one fail","failure_modes":[{"what":"lockfile race",'
            '"where":"engine/pipeline.py:run","trigger":"two writers stage at once",'
            '"detected_by":"","covered_in_this_pr":false}]}'
        )
        verdict = self._parse(content, run_record=[])
        assert verdict.verdict == "INCOMPLETE"
        assert len(verdict.failure_modes) == 1  # allowed, not dropped
        assert "detected_by is empty" in verdict.summary
        assert "uncovered failure path" in verdict.summary

    def test_failure_modes_capped_at_three(self) -> None:
        """R3: entries beyond 3 are truncated, not an error."""
        entries = ",".join(
            '{"what":"m%d","where":"a.py:f","trigger":"t","detected_by":"","covered_in_this_pr":false}'
            % i
            for i in range(5)
        )
        content = (
            '{"verdict":"COMPLETE","items":[],"summary":"s","failure_modes":[' + entries + "]}"
        )
        verdict = self._parse(content, run_record=[])
        assert len(verdict.failure_modes) == 3

    def test_detected_by_backing_helper_is_direct_and_deterministic(self) -> None:
        """R2: the backing check is a plain helper tests call directly."""
        from engine.evaluator import detected_by_is_backed, failure_mode_test_refs

        # Prose naming no test needs no backing.
        assert failure_mode_test_refs("manual browser check") == []
        assert detected_by_is_backed("manual browser check", []) is True
        # A node id that ran (in the command AND its output) is backed.
        assert detected_by_is_backed(
            "tests/test_a.py::test_x", ["pytest tests/test_a.py::test_x", "1 passed"]
        )
        # The same node id with an empty record has nothing behind it.
        assert detected_by_is_backed("tests/test_a.py::test_x", []) is False
        # A bare test_ identifier resolves the same way.
        assert detected_by_is_backed("test_expired_token", ["ran test_expired_token"]) is True
        assert detected_by_is_backed("test_expired_token", []) is False

    def test_failure_modes_persisted_with_five_fields(self) -> None:
        """build_verdict_data persists the block additively with five plain fields.

        Populated block → verdict.json carries exactly the five fields per
        entry. Empty block → NO ``failure_modes`` key at all (asserted as
        absent, not empty), so a pre-GR-179 verdict serializes byte-identically.
        """
        from engine.persist import build_verdict_data
        from engine.task_manager import Task

        populated = self._parse(self.BACKED_CONTENT, run_record=[])
        data = build_verdict_data(
            ".", Task(id="T-1", title="t", criteria=["c"]), _VerdictHolder(populated)
        )
        assert data["failure_modes"] == [
            {
                "what": "import error when plugin dir missing",
                "where": "engine/plugins.py:load",
                "trigger": "worktree without an empty plugins/ dir",
                "detected_by": "tests/test_judge.py::test_failure_modes_empty_is_unpenalised",
                "covered_in_this_pr": True,
            }
        ]

        empty = self._parse(self.EMPTY_BLOCK_CONTENT, run_record=[])
        empty_data = build_verdict_data(
            ".", Task(id="T-1", title="t", criteria=["c"]), _VerdictHolder(empty)
        )
        assert "failure_modes" not in empty_data  # absent, never an empty list

    def test_failure_modes_status_dict_includes_block_only_when_nonempty(self) -> None:
        """judge_result_to_dict carries the block only when it has entries.

        The degraded-flag contract is untouched either way: a summary whose
        detected_by names a test with no tool result behind it still carries
        its ``[uncovered failure paths: …]`` flag in ``d["summary"]`` exactly
        as failure_mode_summary_flags appended it.
        """
        from engine.judge import judge_result_to_dict

        populated = self._parse(self.BACKED_CONTENT, run_record=[])
        result = JudgeResult(task_id="t1", passed=True, tier2=populated, verdict=populated)
        d = judge_result_to_dict("t1", "/wd", result)
        assert d["failure_modes"] == [
            {
                "what": "import error when plugin dir missing",
                "where": "engine/plugins.py:load",
                "trigger": "worktree without an empty plugins/ dir",
                "detected_by": "tests/test_judge.py::test_failure_modes_empty_is_unpenalised",
                "covered_in_this_pr": True,
            }
        ]

        empty = self._parse(self.EMPTY_BLOCK_CONTENT, run_record=[])
        empty_result = JudgeResult(task_id="t1", passed=True, tier2=empty, verdict=empty)
        empty_d = judge_result_to_dict("t1", "/wd", empty_result)
        assert "failure_modes" not in empty_d

        # Degradation flags survive, verbatim, in the machine-readable summary.
        degraded = self._parse(self.UNBACKED_CONTENT, run_record=[])
        degraded_result = JudgeResult(task_id="t1", passed=True, tier2=degraded, verdict=degraded)
        degraded_d = judge_result_to_dict("t1", "/wd", degraded_result)
        assert "uncovered failure paths" in degraded_d["summary"]
        assert "tests/test_never_ran.py::test_absent" in degraded_d["summary"]

    def test_failure_modes_rendered_in_judge_summary(self) -> None:
        """JudgeResult.summary lists populated entries; renders nothing when empty."""
        populated = self._parse(self.BACKED_CONTENT, run_record=[])
        result = JudgeResult(task_id="t1", passed=True, verdict=populated)
        summary = result.summary
        assert "Failure modes (what can still go wrong):" in summary
        assert "import error when plugin dir missing" in summary  # what
        assert "engine/plugins.py:load" in summary  # where
        assert (
            "tests/test_judge.py::test_failure_modes_empty_is_unpenalised" in summary
        )  # detected_by
        assert "covered in this PR" in summary

        empty = self._parse(self.EMPTY_BLOCK_CONTENT, run_record=[])
        empty_result = JudgeResult(task_id="t1", passed=True, verdict=empty)
        assert "Failure modes" not in empty_result.summary


class _VerdictHolder:
    """Minimal JudgeResult stand-in for build_verdict_data (persist reads
    ``verdict`` / ``passed`` / ``pipeline_result`` only)."""

    def __init__(self, verdict: Verdict) -> None:
        self.verdict = verdict
        self.passed = verdict.verdict == "COMPLETE"
        self.summary = verdict.summary
        self.pipeline_result: dict[str, object] = {}


# ── GR-180: reflection pass vetting the second-pass findings ─────────────────
#
# ch:trace GR-180 row=GR-180 spec=docs/evaluator-loop.md#the-author-independent-second-pass-gr-171 test=tests/test_judge.py::TestFindingReflection test=tests/test_judge.py::TestReflectionJudgeWiring evidence=none:pre-fix-red-proof-quoted-in-worker-report witness=none:worker-terminal-run commit=none:open
#
# The reflection pass (engine.review_profiles.FindingReflector) scores each
# second-pass candidate finding for correctness and evidence and DROPS the
# ones below review.reflection_threshold before the verdict record is written.
# Fail-closed (AC2): a reflection that fails or cannot be parsed drops every
# finding, never promotes one. Visibility (AC3): the drop record rides the
# review block into build_verdict_data's persisted verdict shape unchanged.


def _llm_response(content: str) -> object:
    from engine.llm import LLMResponse, LLMUsage

    return LLMResponse(content=content, usage=LLMUsage())


class _ScriptedLLM:
    """Queued chat() responses in order; records every call; raises on empty."""

    def __init__(self, responses: list[object]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, object]] = []

    def chat(self, messages: object, tools: object = None, max_tokens: object = None) -> object:
        self.calls.append({"messages": messages, "max_tokens": max_tokens})
        if not self.responses:
            raise AssertionError("scripted LLM ran out of responses")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _scores_json(entries: list[dict[str, object]]) -> str:
    import json

    return json.dumps({"scores": entries, "summary": "scored"})


GR180_FINDINGS_JSON = (
    '{"findings":['
    '{"severity":"high","file":"engine/a.py","line":1,"detection":"rule a","detail":"d a"},'
    '{"severity":"medium","file":"engine/b.py","line":2,"detection":"rule b","detail":"d b"}],'
    '"summary":"two candidates"}'
)

GR180_SCORES_DROP_B = (
    '{"scores":['
    '{"index":0,"correctness":0.9,"evidence":0.8,"reason":"visible in diff"},'
    '{"index":1,"correctness":0.2,"evidence":0.2,"reason":"speculation"}],'
    '"summary":"one survives"}'
)

GR180_SCORES_KEEP_ALL = (
    '{"scores":['
    '{"index":0,"correctness":0.9,"evidence":0.8},'
    '{"index":1,"correctness":0.8,"evidence":0.9}],'
    '"summary":"both solid"}'
)


class TestFindingReflection:
    """GR-180 AC1/AC2: score each candidate finding for correctness and
    evidence; drop below the threshold; fail closed on any reflection
    failure — a broken filter never promotes."""

    def _findings(self, n: int) -> list[object]:
        from engine.review_profiles import SecondPassFinding

        return [
            SecondPassFinding(
                severity="high",
                file=f"engine/f{i}.py",
                line=i + 1,
                detection=f"rule {i}",
                detail="evidence citation",
            )
            for i in range(n)
        ]

    def test_findings_below_threshold_are_dropped(self) -> None:
        """AC1: a score below review.reflection_threshold drops the finding;
        the drop reason names the dimension that fell and the threshold."""
        from engine.review_profiles import FindingReflector

        findings = self._findings(3)
        reply = _scores_json(
            [
                {"index": 0, "correctness": 0.9, "evidence": 0.8, "reason": "visible"},
                {"index": 1, "correctness": 0.2, "evidence": 0.9, "reason": "wrong claim"},
                {"index": 2, "correctness": 0.9, "evidence": 0.1, "reason": "no such line"},
            ]
        )
        result = FindingReflector(_ScriptedLLM([_llm_response(reply)]), ".").reflect(findings)
        assert [f.file for f in result.kept] == ["engine/f0.py"]
        assert result.error is None
        assert [d.index for d in result.dropped] == [1, 2]
        assert "correctness" in result.dropped[0].reason
        assert "below threshold" in result.dropped[0].reason
        assert "evidence" in result.dropped[1].reason
        record = result.to_dict()
        assert record is not None
        assert record["dropped"] == 2
        assert record["dropped_findings"][1]["detection"] == "rule 2"

    def test_score_at_the_threshold_survives(self) -> None:
        """ "Below a configurable threshold" — exactly AT the threshold is kept,
        and a no-drop reflection adds no record (absence contract)."""
        from engine.review_profiles import FindingReflector

        findings = self._findings(1)
        reply = _scores_json([{"index": 0, "correctness": 0.6, "evidence": 0.6}])
        result = FindingReflector(_ScriptedLLM([_llm_response(reply)]), ".").reflect(findings)
        assert len(result.kept) == 1
        assert result.to_dict() is None

    def test_unparseable_reply_drops_every_finding(self) -> None:
        """AC2 fail-closed: a reflection that cannot be parsed drops the
        findings — it never promotes them."""
        from engine.review_profiles import FindingReflector

        findings = self._findings(2)
        result = FindingReflector(
            _ScriptedLLM([_llm_response("they all look fine to me")]), "."
        ).reflect(findings)
        assert result.kept == []
        assert result.error is not None and "unparseable" in result.error
        assert [d.index for d in result.dropped] == [0, 1]
        record = result.to_dict()
        assert record is not None
        assert record["dropped"] == 2 and "error" in record

    def test_llm_failure_drops_every_finding(self) -> None:
        """AC2 fail-closed: a reflection whose LLM call fails drops everything."""
        from engine.review_profiles import FindingReflector

        findings = self._findings(2)
        result = FindingReflector(_ScriptedLLM([RuntimeError("boom")]), ".").reflect(findings)
        assert result.kept == []
        assert result.error is not None and "LLM call failed" in result.error
        assert result.to_dict()["dropped"] == 2

    def test_missing_or_malformed_scores_drop_only_their_finding(self) -> None:
        """AC2 fail-closed per finding: a missing score entry, or a non-numeric
        dimension, drops THAT finding with the reason — others are untouched."""
        from engine.review_profiles import FindingReflector

        findings = self._findings(3)
        reply = _scores_json(
            [
                {"index": 0, "correctness": 0.9, "evidence": 0.9},
                # index 1 never scored
                {"index": 2, "correctness": "high", "evidence": 0.9},
            ]
        )
        result = FindingReflector(_ScriptedLLM([_llm_response(reply)]), ".").reflect(findings)
        assert [f.file for f in result.kept] == ["engine/f0.py"]
        reasons = {d.index: d.reason for d in result.dropped}
        assert "no score" in reasons[1]
        assert "unusable reflection score" in reasons[2]

    def test_empty_findings_make_no_llm_call(self) -> None:
        """Nothing to vet → no reflection call, no drops, no record."""
        from engine.review_profiles import FindingReflector

        llm = _ScriptedLLM([])
        result = FindingReflector(llm, ".").reflect([])
        assert result.kept == [] and result.dropped == []
        assert result.to_dict() is None
        assert llm.calls == []


def _wired_repo(tmp_path: object) -> str:
    """A minimal REAL git repo with one commit, so the second pass and the
    reflection both resolve a non-empty diff (collect_review_diff)."""
    import subprocess

    repo = tmp_path / "gr180-repo"  # type: ignore[attr-defined]
    repo.mkdir()
    run = subprocess.run
    run(["git", "init", "-q"], cwd=repo, check=True)
    run(["git", "config", "user.email", "author@example.com"], cwd=repo, check=True)
    run(["git", "config", "user.name", "Author A"], cwd=repo, check=True)
    (repo / "code.py").write_text("def f():\n    return 1\n")
    run(["git", "add", "code.py"], cwd=repo, check=True)
    run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
    return str(repo)


def _write_review_config(workdir: str, review_block: str) -> None:
    import pathlib

    cfg = pathlib.Path(workdir) / ".gitreins" / "config.yaml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text("review:\n" + review_block)


class TestReflectionJudgeWiring:
    """GR-180 AC3/AC5: the drop record rides the verdict record (persisted
    shape unchanged otherwise); the threshold and the opt-in come from the
    existing review config surface."""

    def _judge(self, workdir: str, llm: object) -> Judge:
        return Judge(llm, workdir, persist_log=False, persist_telemetry=False)

    def test_drop_record_rides_the_review_block(self, tmp_path: object) -> None:
        """AC3: dropped count and per-drop reasons are visible on the verdict
        record — and reach build_verdict_data's persisted shape unchanged."""
        from engine.persist import build_verdict_data
        from engine.task_manager import Task

        workdir = _wired_repo(tmp_path)
        _write_review_config(workdir, "  second_pass: true\n  reflection: true\n")
        llm = _ScriptedLLM([_llm_response(GR180_FINDINGS_JSON), _llm_response(GR180_SCORES_DROP_B)])
        result = JudgeResult(task_id="t1")
        self._judge(workdir, llm)._run_second_pass(result)
        block = result.second_pass_review
        assert isinstance(block, dict)
        assert [f["file"] for f in block["findings"]] == ["engine/a.py"]
        reflection = block["reflection"]
        assert reflection["dropped"] == 1
        assert reflection["dropped_findings"][0]["detection"] == "rule b"
        assert "below threshold" in reflection["dropped_findings"][0]["reason"]

        persisted = JudgeResult(task_id="t1", passed=True)
        persisted.second_pass_review = block
        data = build_verdict_data(workdir, Task(id="t1", title="t", criteria=["c"]), persisted)
        assert data["review"]["reflection"]["dropped"] == 1
        assert data["review"]["reflection"]["threshold"] == 0.6

    def test_reflection_off_keeps_the_block_unchanged(self, tmp_path: object) -> None:
        """Backward compat: without review.reflection, findings ride untouched,
        the review block carries NO reflection key (absent, never empty), and
        the judge makes exactly one LLM call."""
        workdir = _wired_repo(tmp_path)
        _write_review_config(workdir, "  second_pass: true\n")
        llm = _ScriptedLLM([_llm_response(GR180_FINDINGS_JSON)])
        result = JudgeResult(task_id="t1")
        self._judge(workdir, llm)._run_second_pass(result)
        block = result.second_pass_review
        assert isinstance(block, dict)
        assert len(block["findings"]) == 2
        assert "reflection" not in block
        assert len(llm.calls) == 1

    def test_reflection_threshold_is_configurable(self, tmp_path: object) -> None:
        """AC1: the threshold comes from review.reflection_threshold — a strict
        one drops findings the default keeps."""
        workdir = _wired_repo(tmp_path)
        _write_review_config(
            workdir, "  second_pass: true\n  reflection: true\n  reflection_threshold: 0.95\n"
        )
        llm = _ScriptedLLM(
            [_llm_response(GR180_FINDINGS_JSON), _llm_response(GR180_SCORES_KEEP_ALL)]
        )
        result = JudgeResult(task_id="t1")
        self._judge(workdir, llm)._run_second_pass(result)
        block = result.second_pass_review
        assert isinstance(block, dict)
        assert block["findings"] == []
        reflection = block["reflection"]
        assert reflection["dropped"] == 2
        assert reflection["threshold"] == 0.95

    def test_reflection_failure_drops_findings_in_the_record(self, tmp_path: object) -> None:
        """AC2 through the judge: an unparseable reflection reply leaves the
        verdict record with zero findings and the failure named — fail-closed
        and visible, never promoted."""
        workdir = _wired_repo(tmp_path)
        _write_review_config(workdir, "  second_pass: true\n  reflection: true\n")
        llm = _ScriptedLLM(
            [_llm_response(GR180_FINDINGS_JSON), _llm_response("no JSON here at all")]
        )
        result = JudgeResult(task_id="t1")
        self._judge(workdir, llm)._run_second_pass(result)
        block = result.second_pass_review
        assert isinstance(block, dict)
        assert block["findings"] == []
        assert block["reflection"]["dropped"] == 2
        assert "unparseable" in block["reflection"]["error"]


# ═══════════════════════════════════════════════════════════════
# GR-240: a reviewed-by-error verdict is degraded, never a judged pass
# ═══════════════════════════════════════════════════════════════
#
# Measured on .gitreins/history: 27 verdicts carry a reviewer-failure error in
# their second-pass review block ("unparseable reviewer reply: ..."), and 8 of
# those 27 read passed=true with findings=[] — recorded as judged passes
# although the reviewer never reported anything. The poison is in the PAST
# records (GR-240 does not rewrite history); what changes is what NEW verdicts
# MEAN: pass_class classifies the reviewed-by-error shape ``degraded``, the
# merge gate refuses it, and the merge recommendation degrades to
# needs_review.

_POISONED_REVIEW_BLOCK = {
    "error": (
        "unparseable reviewer reply: no parseable JSON object in reply: "
        "'I found no issues, the code looks great!'"
    ),
    "findings": [],
}

_NO_DIFF_REVIEW_BLOCK = {
    "error": "no diff to review (clean tree, no commit anchor)",
    "findings": [],
}


def _passing_record(review_block: dict) -> dict:
    """A persisted verdict the evaluator graded COMPLETE over one criterion,
    with *review_block* as its second-pass review block."""
    return {
        "task_id": "GR-240",
        "passed": True,
        "verdict": "COMPLETE",
        "items": [{"criterion": "c1", "status": "PASS", "detail": "verified"}],
        "review": review_block,
    }


class TestDegradedReviewVerdict:
    """GR-240 AC3/AC4: reviewer-failure + zero findings degrades a judged pass
    in every shape; the benign no-diff error does not; the merge gate refuses
    the degraded class and the recommendation degrades."""

    def test_live_judge_result_with_reviewer_failure_is_degraded(self) -> None:
        """The live JudgeResult shape: a Tier 2 artifact plus a poisoned review
        block classifies degraded, not judged."""
        from engine.gate import PASS_CLASS_DEGRADED, pass_class

        result = JudgeResult(
            task_id="GR-240",
            passed=True,
            tier2=Verdict(verdict="COMPLETE", items=[VerdictItem("c1", "PASS", "verified")]),
        )
        result.second_pass_review = dict(_POISONED_REVIEW_BLOCK)
        assert pass_class(result) == PASS_CLASS_DEGRADED

    def test_persisted_dict_with_reviewer_failure_is_degraded(self) -> None:
        """The persisted verdict.json shape classifies the same — here with no
        explicit pass_class at all, derived purely from the artifacts."""
        from engine.gate import PASS_CLASS_DEGRADED, pass_class

        record = _passing_record(dict(_POISONED_REVIEW_BLOCK))
        assert pass_class(record) == PASS_CLASS_DEGRADED

    def test_forged_judged_claim_is_still_degraded(self) -> None:
        """A record that CLAIMS pass_class=judged over a poisoned review block
        is still degraded — the class claim is not proof the reviewer reported
        (the GR-231 precedent, applied to the review block)."""
        from engine.gate import PASS_CLASS_DEGRADED, pass_class

        forged = dict(_passing_record(dict(_POISONED_REVIEW_BLOCK)), pass_class="judged")
        assert pass_class(forged) == PASS_CLASS_DEGRADED

    def test_gr231_unverified_keeps_precedence_over_degraded(self) -> None:
        """A judged CLAIM with no Tier 2 artifact stays unverified-never-pass
        even with a poisoned review block — GR-231's handling for records with
        no artifact is intact (both classes refuse the pass; the claim without
        an artifact is the deeper defect)."""
        from engine.gate import PASS_CLASS_UNVERIFIED, pass_class

        claimed = {
            "task_id": "GR-240",
            "passed": True,
            "pass_class": "judged",
            "verdict": "COMPLETE",
            "items": [],
            "review": dict(_POISONED_REVIEW_BLOCK),
        }
        assert pass_class(claimed) == PASS_CLASS_UNVERIFIED

    def test_no_diff_error_is_not_degraded(self) -> None:
        """AC4: the benign no-diff case keeps today's class behavior exactly —
        judged with an artifact, and the advisory recommendation stays
        no_concerns_found."""
        from engine.gate import MERGE_RECOMMENDATION_OK, merge_recommendation, pass_class

        record = _passing_record(dict(_NO_DIFF_REVIEW_BLOCK))
        assert pass_class(record) != "degraded"
        assert merge_recommendation(record, ["c1"]) == MERGE_RECOMMENDATION_OK

    def test_merge_recommendation_degrades_for_reviewer_failure(self) -> None:
        """A degraded record recommends needs_review, never no_concerns_found —
        in the persisted dict shape AND through the live-Verdict path persist.py
        uses (where the review block must be supplied, or the verdict object
        alone would still read no_concerns_found)."""
        from engine.gate import MERGE_RECOMMENDATION_REVIEW, merge_recommendation

        record = _passing_record(dict(_POISONED_REVIEW_BLOCK))
        assert merge_recommendation(record, ["c1"]) == MERGE_RECOMMENDATION_REVIEW

        live_verdict = Verdict(verdict="COMPLETE", items=[VerdictItem("c1", "PASS", "verified")])
        assert merge_recommendation(live_verdict, ["c1"]) != MERGE_RECOMMENDATION_REVIEW
        assert (
            merge_recommendation(live_verdict, ["c1"], review=dict(_POISONED_REVIEW_BLOCK))
            == MERGE_RECOMMENDATION_REVIEW
        )

    def test_persisted_stamp_degrades_end_to_end(self, tmp_path: object) -> None:
        """build_verdict_data stamps pass_class=degraded and a needs_review
        recommendation for a live result the reviewer never reported on."""
        from engine.persist import build_verdict_data
        from engine.task_manager import Task

        workdir = _wired_repo(tmp_path)
        result = JudgeResult(
            task_id="GR-240",
            passed=True,
            tier2=Verdict(verdict="COMPLETE", items=[VerdictItem("c1", "PASS", "verified")]),
            verdict=Verdict(verdict="COMPLETE", items=[VerdictItem("c1", "PASS", "verified")]),
        )
        result.second_pass_review = dict(_POISONED_REVIEW_BLOCK)
        data = build_verdict_data(workdir, Task(id="GR-240", title="t", criteria=["c1"]), result)
        assert data["pass_class"] == "degraded"
        assert data["merge_recommendation"] == "needs_review"

    def test_merge_gate_refuses_degraded_verdict(self, tmp_path: object) -> None:
        """The merge gate routes a degraded PASS to review instead of landing
        it — the same entrypoint the GR-231 unverified refusal tests drive."""
        from engine.worktree_manager import (
            WorktreeError,
            WorktreeManager,
            write_disk_verdict,
        )

        repo = tmp_path / "gr240-repo"  # type: ignore[attr-defined]
        repo.mkdir()
        _git_init_repo(repo)
        manager = WorktreeManager(repo)
        record, _created = manager.create("GR-240-DEGRADED")
        tree = Path(record.path)
        (tree / "lane.txt").write_text("lane\n", encoding="utf-8")
        _git_run(tree, "add", "lane.txt")
        _git_run(tree, "commit", "-qm", "lane work")
        tip = _git_run(tree, "rev-parse", "HEAD").stdout.strip()
        verdict = {
            "task_id": record.task_id,
            "passed": True,
            "pass_class": "judged",
            "verdict": "COMPLETE",
            "items": [{"criterion": "c1", "status": "PASS", "detail": "verified"}],
            "review": dict(_POISONED_REVIEW_BLOCK),
            "worktree": str(tree.resolve()),
            "branch": record.branch,
            "commit": tip,
        }
        write_disk_verdict(tree, verdict)

        with pytest.raises(WorktreeError, match="degraded"):
            manager.merge(record.task_id)
        assert _git_run(repo, "rev-parse", "HEAD").stdout.strip() != tip


def _git_init_repo(repo: Path) -> None:
    """A minimal canonical-main checkout: one commit, clean tree."""
    import subprocess

    run = subprocess.run
    run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    run(["git", "config", "user.email", "author@example.com"], cwd=repo, check=True)
    run(["git", "config", "user.name", "Author A"], cwd=repo, check=True)
    (repo / "base.txt").write_text("base\n", encoding="utf-8")
    run(["git", "add", "base.txt"], cwd=repo, check=True)
    run(["git", "commit", "-qm", "initial"], cwd=repo, check=True)


def _git_run(repo: Path, *args: str):
    import subprocess

    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True, timeout=60
    )
