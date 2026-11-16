"""GR-234 regression tests for multi-stage tier2 context plumbing."""

import copy
import json
from unittest.mock import patch

from engine.evaluator import Verdict, VerdictItem
from engine.llm import LLMResponse
from engine.pipeline import Pipeline


class TranscriptEvaluator:
    def __init__(self, findings: list[dict[str, str]], transcript: list[object]) -> None:
        self._findings = findings
        self.transcript = transcript

    def evaluate(self, task: dict[str, object]) -> Verdict:
        self.transcript.append(copy.deepcopy(task.get("_stage_context")))
        return Verdict(
            verdict="INCOMPLETE",
            items=[
                VerdictItem(
                    criterion="criterion",
                    status="FAIL",
                    detail=json.dumps(self._findings, separators=(",", ":")),
                )
            ],
            summary="stage finding",
        )


def _config(stage_ids: list[str]) -> dict[str, object]:
    return {
        "evaluator": {"prescreen": False, "max_iterations": 1},
        "pipeline": {
            "stages": [
                {
                    "id": stage_id,
                    "type": "ai_eval",
                    "condition": "true",
                    "on": ["pre-eval"],
                }
                for stage_id in stage_ids
            ]
        },
    }


def test_two_stages_forward_findings_and_preserve_outputs(tmp_path) -> None:
    transcript: list[object] = []
    evaluator = TranscriptEvaluator(
        [{"id": "F-1", "detail": "distinctive first-pass finding"}], transcript
    )
    pipeline = Pipeline(_config(["first", "second"]), str(tmp_path), llm=object())
    with patch("engine.evaluator.AgenticEvaluator", return_value=evaluator):
        result = pipeline.run({"id": "task", "title": "task", "criteria": ["criterion"]})

    assert transcript[0] is None
    assert transcript[1]["prior_findings"] == result["stages"]["first"]["steps"]
    assert "distinctive first-pass finding" in json.dumps(transcript[1])
    assert list(result["stages"]) == ["first", "second"]
    assert result["stages"]["first"]["steps"]
    assert result["stages"]["second"]["steps"]


def test_three_stages_forward_immediately_prior_output(tmp_path) -> None:
    transcript: list[object] = []
    evaluator = TranscriptEvaluator([{"id": "F", "detail": "finding"}], transcript)
    pipeline = Pipeline(_config(["one", "two", "three"]), str(tmp_path), llm=object())
    with patch("engine.evaluator.AgenticEvaluator", return_value=evaluator):
        result = pipeline.run({"id": "task", "title": "task", "criteria": ["criterion"]})

    assert transcript[1]["prior_findings"] == result["stages"]["one"]["steps"]
    assert transcript[2]["prior_findings"] == result["stages"]["two"]["steps"]
    assert transcript[2]["prior_findings"] != result["stages"]["one"]["steps"]
    assert len(transcript[2]["diff_digest"]) == 64


def test_single_stage_keeps_context_out_of_task_and_result_shape(tmp_path) -> None:
    transcript: list[object] = []
    evaluator = TranscriptEvaluator([], transcript)
    pipeline = Pipeline(_config(["only"]), str(tmp_path), llm=object())
    with patch("engine.evaluator.AgenticEvaluator", return_value=evaluator):
        result = pipeline.run({"id": "task", "title": "task", "criteria": ["criterion"]})

    assert transcript == [None]
    assert set(result) == {"passed", "stages"}
    assert set(result["stages"]["only"]) == {
        "id",
        "passed",
        "any_failed",
        "summary",
        "steps",
    }


def test_second_stage_prompt_contains_prior_findings_verbatim(tmp_path) -> None:
    """The LLM-facing transcript, not just storage, carries the prior finding."""

    class RecordingLLM:
        def __init__(self) -> None:
            self.prompts: list[str] = []

        def chat(self, messages: list[dict[str, object]], **kwargs: object) -> LLMResponse:
            self.prompts.append(str(messages[1]["content"]))
            detail = "FIRST_PASS_FINDING_9f3c"
            return LLMResponse(
                content=json.dumps(
                    {
                        "verdict": "INCOMPLETE",
                        "items": [{"criterion": "criterion", "status": "FAIL", "detail": detail}],
                        "summary": "stage",
                    }
                )
            )

    llm = RecordingLLM()
    result = Pipeline(_config(["first", "second"]), str(tmp_path), llm=llm).run(
        {"id": "task", "title": "task", "criteria": ["criterion"]}
    )

    assert len(llm.prompts) == 2
    assert "FIRST_PASS_FINDING_9f3c" not in llm.prompts[0]
    assert "FIRST_PASS_FINDING_9f3c" in llm.prompts[1]
    assert "## PRIOR STAGE CONTEXT" in llm.prompts[1]
    assert set(result["stages"]) == {"first", "second"}
