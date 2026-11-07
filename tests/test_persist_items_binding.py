"""GR-184: the persisted verdict must carry the graded evidence items.

Measured defect (scratch repo, gitreins 0.19.0, 2026-11-07): a staged-pipeline
judge run graded its criterion PASS with proof (visible in stages.tier2 steps
data AND in the tier2 summary), the verdict recorded passed=True /
pass_class=judged — and the top-level ``items`` array was still empty, because
``build_verdict_data`` only reads ``result.verdict``, which the pipeline path
never sets; the graded evidence lives in ``pipeline_result`` and only
``judge.py``'s own status dict knows the ``_find_pipeline_verdict`` fallback.

Every verdict written through the staged pipeline — i.e. all of them in the
modern flow — hit this, which is why 116/116 judged passes measured empty
items on 2026-11-06/07.
"""

from __future__ import annotations

import json

import pytest

from engine.judge import JudgeResult
from engine.persist import build_verdict_data
from engine.task_manager import Task

PIPELINE_RESULT = {
    "stages": {
        "tier1": {
            "id": "tier1",
            "passed": True,
            "any_failed": False,
            "steps": [{"id": "tests", "type": "script", "passed": True, "output": "1 passed"}],
        },
        "tier2": {
            "id": "tier2",
            "passed": True,
            "any_failed": False,
            "steps": [
                {
                    "id": "tier2",
                    "type": "ai_eval",
                    "passed": True,
                    "output": "COMPLETE",
                    "data": {
                        "verdict": "COMPLETE",
                        "summary": "criterion graded on observed proof output",
                        "items": [
                            {
                                "criterion": 'FAILIF: greet("world") does not return hello world',
                                "status": "PASS",
                                "detail": "PROOF ran: pytest tests/test_greet.py::test_greet -> 1 passed (exit 0)",
                            }
                        ],
                    },
                }
            ],
        },
    }
}


@pytest.fixture()
def task(tmp_path) -> Task:
    t = Task(id="T1", title="greet module")
    t.criteria = ['FAILIF: greet("world") does not return hello world']
    return t


def test_pipeline_graded_items_survive_persistence(tmp_path, task) -> None:
    result = JudgeResult(
        task_id=task.id,
        passed=True,
        pipeline_result=PIPELINE_RESULT,
        verdict=None,  # the staged-pipeline path never sets result.verdict
    )
    data = build_verdict_data(str(tmp_path), task, result)
    assert len(data["items"]) == 1, (
        "the judge graded the criterion with proof, the evidence is in "
        "pipeline_result, and the persisted verdict dropped it"
    )
    assert data["items"][0]["status"] == "PASS"
    assert "pytest" in data["items"][0]["detail"]


def test_non_pipeline_path_unchanged(tmp_path, task) -> None:
    """The legacy result.verdict path must keep its exact shape."""
    from engine.evaluator import Verdict, VerdictItem

    verdict = Verdict(
        verdict="COMPLETE",
        items=[VerdictItem(criterion="c1", status="PASS", detail="ok")],
        summary="all good",
    )
    result = JudgeResult(task_id=task.id, passed=True, verdict=verdict, pipeline_result=None)
    data = build_verdict_data(str(tmp_path), task, result)
    assert len(data["items"]) == 1
    assert data["items"][0]["criterion"] == "c1"
    assert json.dumps(data["items"][0])  # serializable


def test_no_evidence_stays_explicitly_empty(tmp_path, task) -> None:
    """No pipeline evidence and no verdict object -> explicit empty list."""
    result = JudgeResult(task_id=task.id, passed=False, verdict=None, pipeline_result=None)
    data = build_verdict_data(str(tmp_path), task, result)
    assert data["items"] == []
