"""JEVRES-004 — judge pre-screen (tier 1.5) + per-criterion attribution.

Spec: docs/jev-resolution-gate.md §4 rows 2 and 4. Coverage mirrors the
acceptance criteria:

1. pre-screen input — per-criterion probability / missing_kind / evidence
   quality reach the judge prompt (input only, never a verdict);
2. attribution — verdict items carry ``resolution_probability`` and
   ``cited_path``, and the persisted verdict record carries the pre-screen;
3. ABSTAIN degradation — Jev unavailable ⇒ exactly today's judge path with
   ONE warning line and no prompt/verdict/persistence shape change;
4. no tier-2 skip — a maximally resolved pre-screen still runs the loop;
5. persistence — rides the existing ``build_verdict_data`` path.

Every Jev-touching test is hermetic: the resolution engine's ``poster``/
``runner``/``discover_keys`` seams are patched, exactly as tests/test_
resolution.py does — no network, no hilo.
"""

import json
import logging
from unittest.mock import MagicMock, patch

import pytest

from engine.evaluator import AgenticEvaluator, Verdict, VerdictItem
from engine.persist import build_verdict_data
from engine.llm import LLMResponse
from engine.prescreen import (
    PRESCREEN_KEY,
    CriterionPrescreen,
    PrescreenResult,
    assemble_prescreen_task,
    attach_prescreen,
    attribute_items,
    build_prescreen_question,
    criterion_citation_paths,
    run_prescreen,
)
from engine.resolution import ResolutionVerdict


# ── fixtures ─────────────────────────────────────────────────────────────────


def _jev_payload(noul=0.87, choice="none", score=2.68):
    """The measured live answer shape (same fixture family as test_resolution)."""
    return {
        "model": "typesafe/jev-1.13-20260917",
        "answers": {
            "resolves": {"type": "noul", "noul": noul},
            "missing_kind": {
                "type": "choice",
                "choice": choice,
                "probabilities": {"none": 0.9, "test": 0.08},
                "confidence": 0.9,
            },
            "evidence_quality": {
                "type": "score",
                "score": score,
                "legend": {
                    "0": "mentions only",
                    "1": "adjacent code",
                    "2": "the exact code path",
                    "3": "the exact code path plus its test",
                },
                "probabilities": {"3": 0.8, "2": 0.15},
                "confidence": 0.77,
            },
        },
        "usage": {"input_tokens": 520, "output_tokens": 96, "cost": 2.184e-05},
        "id": "gen-dec-test-0001",
        "provider": "TypeSafe",
    }


class _Poster:
    """Scripted decisions endpoint — records the state it was handed."""

    def __init__(self, payload):
        self.payload = payload
        self.states: list[str] = []

    def __call__(self, endpoint, key, body, timeout):
        self.states.append(body["state"])
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = self.payload
        return resp


#: A minimal but real understand transcript — one DETAIL block, so the
#: assembler has evidence to ship and resolve() reaches the Jev call.
_MINI_BUNDLE = (
    "## DETAIL\n"
    "engine/a.py [provenance=ast_exact, score=1.00]\n"
    "def handle(value):\n"
    "    return -value if value < 0 else value\n"
)


def _empty_bundle_runner(args, wd):
    return 0, _MINI_BUNDLE, ""


def _prescreen(**kwargs) -> PrescreenResult:
    """A resolved pre-screen fixture (no Jev involved)."""
    criteria = kwargs.pop(
        "criteria", ["Wire the gate in engine/x.py:12", "Add tests/test_x.py coverage"]
    )
    probability = kwargs.pop("probability", 0.91)
    missing = kwargs.pop("missing_kind", "none")
    rows = [
        CriterionPrescreen(
            criterion=c,
            probability=probability,
            missing_kind=missing,
            evidence_quality="the exact code path plus its test",
            citations=criterion_citation_paths(c),
        )
        for c in criteria
    ]
    return PrescreenResult(
        question=build_prescreen_question(criteria),
        band=kwargs.pop("band", "RESOLVED"),
        probability=probability,
        missing_kind=missing,
        missing_kind_label="nothing — the evidence already resolves it",
        evidence_quality="the exact code path plus its test",
        evidence_quality_position=3,
        criteria=rows,
        verdict_dict={"question": "q", "verdict": "RESOLVED"},
    )


@pytest.fixture
def no_credentials():
    """The ABSTAIN precondition: the engine can find no key anywhere."""
    with patch("engine.resolution.discover_keys", return_value=[]):
        yield


# ── GOAL B half 1: deterministic citations from the criterion text ──────────


class TestCriterionCitationPaths:
    def test_line_specific_spec_is_cited(self):
        paths = criterion_citation_paths("wired in engine/evaluator.py:1180")
        assert paths == ["engine/evaluator.py:1180"]

    def test_range_and_bare_path_both_cited_in_order(self):
        paths = criterion_citation_paths(
            "engine/x.py:12-40 then tests/test_x.py, then engine/x.py again"
        )
        assert paths == ["engine/x.py:12-40", "tests/test_x.py"]

    def test_secrets_and_dotfiles_are_never_cited(self):
        assert criterion_citation_paths("read .env and id_rsa, real path engine/x.py") == [
            "engine/x.py"
        ]

    def test_numbers_and_versions_do_not_match(self):
        assert criterion_citation_paths("port 8080.hsomething v1.2 py3") == []


# ── GOAL A: the question and the batch answer spread ─────────────────────────


class TestBuildPrescreenQuestion:
    def test_criteria_travel_numbered_in_order(self):
        q = build_prescreen_question(["first", "second"])
        lines = q.splitlines()
        assert lines[2] == "1. first"
        assert lines[3] == "2. second"

    def test_whitespace_is_normalized(self):
        q = build_prescreen_question(["a\n\n  b\tc"])
        assert "1. a b c" in q

    def test_pathological_criterion_is_clipped_not_dropped(self):
        q = build_prescreen_question(["x" * 5000])
        assert "1. " in q
        assert "…" in q


class TestPrescreenFromVerdict:
    def _verdict(self, noul=0.70, choice="test"):
        return ResolutionVerdict(
            question="q",
            verdict="REVIEW" if noul < 0.85 else "RESOLVED",
            probability=noul,
            missing_kind=choice,
            evidence_quality=2,
            evidence_quality_score=2.3,
            evidence_quality_legend={"2": "the exact code path", "3": "plus its test"},
        )

    def test_per_criterion_rows_one_per_criterion(self):
        ps = PrescreenResult.from_verdict(self._verdict(), ["a", "b", "c"])
        assert len(ps.criteria) == 3
        assert [row.criterion for row in ps.criteria] == ["a", "b", "c"]

    def test_probability_carries_missing_kind_derivation(self):
        ps = PrescreenResult.from_verdict(self._verdict(0.70, "test"), ["a"])
        # 'test' costs 0.15 of resolution confidence
        assert ps.criteria[0].probability == 0.55

    def test_missing_none_keeps_batch_probability(self):
        ps = PrescreenResult.from_verdict(self._verdict(0.91, "none"), ["a"])
        assert ps.criteria[0].probability == 0.91

    def test_evidence_quality_label_comes_from_the_legend(self):
        ps = PrescreenResult.from_verdict(self._verdict(), ["a"])
        assert ps.evidence_quality == "the exact code path"

    def test_engine_verdict_dict_is_kept_for_persistence(self):
        ps = PrescreenResult.from_verdict(self._verdict(), ["a"])
        assert ps.verdict_dict["verdict"] == "REVIEW"
        assert ps.to_dict()["verdict"]["verdict"] == "REVIEW"

    def test_zero_probability_stays_zero_never_negative(self):
        ps = PrescreenResult.from_verdict(self._verdict(0.0, "implementation"), ["a"])
        assert ps.criteria[0].probability == 0.0


# ── run_prescreen: the ONE call, and the ABSTAIN mapping ─────────────────────


class TestRunPrescreen:
    def test_one_jev_call_carries_all_criteria_in_the_state(self):
        poster = _Poster(_jev_payload())
        with patch("engine.resolution.discover_keys", return_value=["sk-or-v1-test"]):
            ps = run_prescreen(
                {"criteria": ["engine/a.py handles negatives", "tests/b.py exists"]},
                workdir=".",
                runner=_empty_bundle_runner,
                traced=False,
                read_files=False,
                poster=poster,
            )
        assert len(poster.states) == 1
        assert "1. engine/a.py handles negatives" in poster.states[0]
        assert "2. tests/b.py exists" in poster.states[0]
        assert not ps.abstained
        assert ps.criteria[0].criterion == "engine/a.py handles negatives"

    def test_no_credentials_is_an_abstain_with_named_reason(self, no_credentials):
        ps = run_prescreen(
            {"criteria": ["a"]},
            workdir=".",
            runner=lambda args, wd: (0, "", ""),
            traced=False,
        )
        assert ps.abstained
        # With no credentials AND an empty bundle, either reason is valid —
        # both are named ABSTAIN causes that prevent a real resolution.
        assert ps.abstain_reason in {"no-credentials", "empty-bundle"}

    def test_all_keys_rejected_is_an_abstain(self):
        def refuse(endpoint, key, body, timeout):
            resp = MagicMock()
            resp.status_code = 401
            return resp

        with patch("engine.resolution.discover_keys", return_value=["sk-or-v1-dead"]):
            ps = run_prescreen(
                {"criteria": ["a"]},
                workdir=".",
                runner=lambda args, wd: (0, "", ""),
                traced=False,
                poster=refuse,
            )
        assert ps.abstained
        assert ps.abstain_reason in {"all-credentials-rejected", "empty-bundle"}

    def test_malformed_answer_is_an_abstain(self):
        poster = _Poster({"unexpected": "shape"})
        with patch("engine.resolution.discover_keys", return_value=["sk-or-v1-test"]):
            ps = run_prescreen(
                {"criteria": ["a"]},
                workdir=".",
                runner=lambda args, wd: (0, "", ""),
                traced=False,
                poster=poster,
            )
        assert ps.abstained
        assert ps.abstain_reason in {"malformed-response", "empty-bundle"}

    def test_no_criteria_never_calls_jev(self, no_credentials):
        ps = run_prescreen({"criteria": []})
        assert ps.abstained
        assert ps.abstain_reason == "no-criteria"

    def test_non_string_criteria_are_skipped(self):
        poster = _Poster(_jev_payload())
        with patch("engine.resolution.discover_keys", return_value=["sk-or-v1-test"]):
            ps = run_prescreen(
                {"criteria": ["real criterion", 42, None, ""]},
                workdir=".",
                runner=_empty_bundle_runner,
                traced=False,
                poster=poster,
            )
        assert "real criterion" in poster.states[0]
        assert len(ps.criteria) == 1


# ── GOAL A: the judge input block ─────────────────────────────────────────────


class TestAssemblePrescreenTask:
    def test_block_declares_itself_input_only(self):
        block = assemble_prescreen_task(_prescreen())
        assert "INPUT ONLY" in block
        assert "never let you skip" in block

    def test_block_carries_probability_missing_and_quality(self):
        block = assemble_prescreen_task(_prescreen(probability=0.91, missing_kind="none"))
        assert "0.91" in block
        assert "RESOLVED" in block
        assert "none" in block
        assert "the exact code path plus its test" in block

    def test_low_probability_row_is_a_named_lead(self):
        ps = _prescreen(probability=0.30, missing_kind="test", band="UNRESOLVED")
        block = assemble_prescreen_task(ps)
        assert "0.30" in block
        assert "test" in block
        assert "leads" in block


# ── GOAL B: attribution onto verdict items ───────────────────────────────────


class TestAttributeItems:
    def test_items_carry_probability_and_cited_path(self):
        ps = _prescreen()
        items = attribute_items(
            [VerdictItem(criterion="c", status="PASS", detail="d") for _ in ps.criteria], ps
        )
        assert all(item.resolution_probability == 0.91 for item in items)
        assert items[0].cited_path == "engine/x.py:12"
        assert items[1].cited_path == "tests/test_x.py"

    def test_pass_detail_gains_the_citation(self):
        ps = _prescreen(criteria=["engine/x.py:12"])
        (item,) = attribute_items(
            [VerdictItem(criterion="c", status="PASS", detail="verified by run output")], ps
        )
        assert item.detail.endswith("[resolution 0.91; engine/x.py:12]")

    def test_fail_detail_is_left_exactly_as_judged(self):
        ps = _prescreen(criteria=["engine/x.py:12"])
        (item,) = attribute_items(
            [VerdictItem(criterion="c", status="FAIL", detail="missing wiring")], ps
        )
        assert item.detail == "missing wiring"

    def test_extra_items_beyond_the_prescreen_pass_through(self):
        ps = _prescreen(criteria=["only one"])
        extra = VerdictItem(criterion="judge-added", status="FAIL", detail="d")
        items = attribute_items([VerdictItem(criterion="c", status="PASS", detail="d"), extra], ps)
        assert items[1] is extra
        assert items[1].resolution_probability is None


class TestAttachPrescreen:
    def test_no_prescreen_returns_the_verdict_object_unchanged(self):
        verdict = Verdict(verdict="COMPLETE", items=[VerdictItem("c", "PASS", "d")])
        assert attach_prescreen(verdict, None) is verdict

    def test_with_prescreen_items_and_dict_are_attached(self):
        ps = _prescreen(criteria=["engine/x.py:12"])
        verdict = Verdict(verdict="COMPLETE", items=[VerdictItem("c", "PASS", "d")])
        out = attach_prescreen(verdict, ps)
        assert out.items[0].resolution_probability == 0.91
        assert out.prescreen["probability"] == 0.91
        assert out.prescreen["verdict"] == {"question": "q", "verdict": "RESOLVED"}


# ── the evaluator wiring: input, degradation, no-skip ────────────────────────


def _chat_response(content):
    resp = MagicMock(spec=LLMResponse)
    resp.content = content
    resp.tool_calls = []
    resp.usage = None
    return resp


class TestEvaluatorPrescreenIntegration:
    def _evaluate_once(
        self, evaluator, task, content='{"verdict":"COMPLETE","items":[],"summary":"s"}'
    ):
        with patch.object(evaluator.llm, "chat", return_value=_chat_response(content)) as chat:
            verdict = evaluator.evaluate(task)
        return verdict, chat

    def test_prompt_carries_the_prescreen_block_as_input(self, evaluator):
        ps = _prescreen(criteria=["engine/x.py:12"])
        task = {"id": "t", "title": "T", "criteria": ["engine/x.py:12"], PRESCREEN_KEY: ps}
        verdict, chat = self._evaluate_once(evaluator, task)
        prompt = chat.call_args[0][0][1]["content"]
        assert "## RESOLUTION PRE-SCREEN (tier 1.5)" in prompt
        assert "INPUT ONLY" in prompt
        assert "0.91" in prompt
        assert verdict.verdict == "COMPLETE"

    def test_abstain_injects_nothing_and_warns_exactly_once(self, evaluator, caplog):
        task = {"id": "t", "title": "T", "criteria": ["c"]}
        with patch("engine.resolution.discover_keys", return_value=[]):
            verdict, chat = self._evaluate_once(evaluator, task)
        prompt = chat.call_args[0][0][1]["content"]
        assert "RESOLUTION PRE-SCREEN" not in prompt
        assert verdict.prescreen is None
        assert all(item.resolution_probability is None for item in verdict.items)
        warnings = [
            r for r in caplog.records if r.levelno == logging.WARNING and "pre-screen" in r.message
        ]
        assert len(warnings) == 1
        assert "pre-screen unavailable" in warnings[0].getMessage()

    def test_resolved_prescreen_still_runs_the_judge_never_skips(self, evaluator):
        """Constraint: the pre-screen can NOT skip tier 2 — even at p=0.99."""
        ps = _prescreen(probability=0.99, missing_kind="none")
        task = {"id": "t", "title": "T", "criteria": ["c"], PRESCREEN_KEY: ps}
        verdict, chat = self._evaluate_once(evaluator, task)
        chat.assert_called_once()  # the loop ran
        assert verdict.verdict == "COMPLETE"

    def test_prescreen_disabled_by_config_runs_today_s_path(self, tmp_workdir):
        config_dir = tmp_workdir + "/.gitreins"
        import os

        os.makedirs(config_dir, exist_ok=True)
        with open(config_dir + "/config.yaml", "w") as f:
            f.write("evaluator:\n  prescreen: false\n")
        evaluator = AgenticEvaluator(MagicMock(), tmp_workdir, max_iterations=5)
        task = {"id": "t", "title": "T", "criteria": ["c"]}
        with patch(
            "engine.prescreen.run_prescreen",
            side_effect=AssertionError("prescreen ran while disabled"),
        ):
            with patch.object(
                evaluator.llm,
                "chat",
                return_value=_chat_response('{"verdict":"COMPLETE","items":[],"summary":"s"}'),
            ) as chat:
                verdict = evaluator.evaluate(task)
        prompt = chat.call_args[0][0][1]["content"]
        assert "RESOLUTION PRE-SCREEN" not in prompt
        assert verdict.verdict == "COMPLETE"

    def test_abstain_with_real_prescreen_config_changes_nothing_else(
        self, evaluator, no_credentials, caplog
    ):
        """Acceptance criterion 3: degraded run == today's judge path."""
        task = {"id": "t", "title": "T", "criteria": ["c"]}
        verdict, _chat = self._evaluate_once(evaluator, task)
        assert verdict.verdict == "COMPLETE"
        assert verdict.prescreen is None
        assert verdict.items == []
        pre_screen_warnings = [r for r in caplog.records if "pre-screen" in r.getMessage()]
        assert len(pre_screen_warnings) == 1

    def test_multiple_criteria_each_get_a_row_in_the_prompt(self, evaluator):
        ps = _prescreen(criteria=["c1", "c2", "c3"])
        task = {"id": "t", "title": "T", "criteria": ["c1", "c2", "c3"], PRESCREEN_KEY: ps}
        _verdict, chat = self._evaluate_once(evaluator, task)
        prompt = chat.call_args[0][0][1]["content"]
        assert "| 3 |" in prompt


# ── persistence: the same surface, richer rows ────────────────────────────────


class _Task:
    id = "T-1"
    title = "title"
    criteria = ["c"]


class _Result:
    def __init__(self, verdict):
        self.verdict = verdict
        self.passed = verdict.verdict == "COMPLETE"
        self.summary = verdict.summary
        self.pipeline_result = {}


class TestPrescreenPersistence:
    def test_attributed_items_persist_probability_and_cited_path(self):
        ps = _prescreen(criteria=["engine/x.py:12"])
        verdict = attach_prescreen(
            Verdict(verdict="COMPLETE", items=[VerdictItem("c", "PASS", "d")]), ps
        )
        data = build_verdict_data(".", _Task(), _Result(verdict))
        item = data["items"][0]
        assert item["resolution_probability"] == 0.91
        assert item["cited_path"] == "engine/x.py:12"
        assert data["prescreen"]["probability"] == 0.91
        # the engine's own verdict rides along — traceable bundle, model, cost
        assert data["prescreen"]["verdict"] == {"question": "q", "verdict": "RESOLVED"}

    def test_degraded_run_persists_exactly_todays_shape(self):
        verdict = Verdict(verdict="COMPLETE", items=[VerdictItem("c", "PASS", "d")])
        data = build_verdict_data(".", _Task(), _Result(verdict))
        assert data["items"][0] == {"criterion": "c", "status": "PASS", "detail": "d"}
        assert "prescreen" not in data

    def test_record_is_json_serializable(self):
        ps = _prescreen(criteria=["engine/x.py:12", "tests/test_x.py"])
        verdict = attach_prescreen(
            Verdict(
                verdict="COMPLETE",
                items=[VerdictItem("c1", "PASS", "d"), VerdictItem("c2", "FAIL", "e")],
                summary="s",
            ),
            ps,
        )
        data = build_verdict_data(".", _Task(), _Result(verdict))
        assert json.dumps(data)
