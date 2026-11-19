"""Regression coverage for evidence-based Tier 2 stage aggregation."""

from __future__ import annotations

from engine.evaluator import Verdict, VerdictItem
from engine.gate import verdict_is_passing
from engine.pipeline import Pipeline, StageResult, StepResult, _record_runtime_skips


CRITERIA = ["criterion one", "criterion two", "criterion three", "criterion four"]


def _filed_verdict(*, statuses: tuple[str, ...] = ("PASS",) * 4) -> Verdict:
    """The COS-W1 persisted shape: four items, all graded, no coverage gap."""
    return Verdict(
        verdict="INCOMPLETE",
        items=[
            VerdictItem(criterion, status, "verified")
            for criterion, status in zip(CRITERIA, statuses, strict=True)
        ],
        summary="Four criteria passed; ungraded 0.",
    )


def test_all_pass_zero_ungraded_overrides_stale_incomplete_label() -> None:
    verdict = _filed_verdict()

    # Reconstructs the filed shape: the label says INCOMPLETE, but all four
    # criteria are present and PASS. Evidence, not a stale aggregate label,
    # must decide the stage result.
    assert verdict_is_passing(verdict, CRITERIA) is True


def test_pipeline_normalizes_filed_stage_outcome_and_summary(tmp_path, monkeypatch) -> None:
    from unittest.mock import patch

    verdict = _filed_verdict()

    class FakeEvaluator:
        _tool_record: list[dict[str, object]] = []

        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def evaluate(self, _task):
            return verdict

    pipeline = Pipeline({}, str(tmp_path), llm=object(), persist_telemetry=False)
    monkeypatch.setattr(pipeline, "_append_judge_usage", lambda *_args, **_kwargs: None)
    with patch("engine.evaluator.AgenticEvaluator", FakeEvaluator):
        step = pipeline._run_ai_eval({"id": "tier2"}, {"criteria": CRITERIA})

    stage = StageResult(id="tier2", passed=step.passed, steps=[step])
    assert stage.to_dict()["passed"] is True
    assert step.data["verdict"] == "COMPLETE"
    assert "INCOMPLETE" not in step.output
    assert "all 4 task criteria were graded and passed" in step.data["summary"].lower()


def test_one_failed_item_does_not_pass() -> None:
    verdict = _filed_verdict(statuses=("PASS", "FAIL", "PASS", "PASS"))

    assert verdict_is_passing(verdict, CRITERIA) is False


def test_empty_item_list_does_not_pass() -> None:
    verdict = Verdict(verdict="COMPLETE", items=[], summary="")

    assert verdict_is_passing(verdict, CRITERIA) is False


def test_degraded_skipped_marker_remains_readable_downstream() -> None:
    stage = StageResult(
        id="tier1",
        steps=[
            StepResult(
                id="tests",
                type="script",
                passed=True,
                data={"skipped_tests": True, "skip_reason": "contention"},
            )
        ],
    )

    _record_runtime_skips(stage)
    serialized = stage.to_dict()

    assert serialized["degraded"] is True
    assert serialized["skipped_steps"] == ["tests"]
    assert "contention" in serialized["degradation_reason"]
