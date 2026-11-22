"""GR-235 regression tests — the fix-verification stage.

Red at the parent commit: the ``fix_verification`` stage type did not exist
there, so the pipeline fell through to the script-step branch ("No command
specified") and no disposition, verdict cap or gate behavior existed.
"""

import os
import subprocess
from unittest.mock import patch

from engine.evaluator import Verdict, VerdictItem
from engine.gate import (
    MERGE_RECOMMENDATION_OK,
    MERGE_RECOMMENDATION_REVIEW,
    fix_verification_unhandled,
    merge_recommendation,
)
from engine.pipeline import FIX_VERIFICATION_STAGE_ID, Pipeline, load_pipeline_config

_FINDING_DETAIL = "foo.py:2 `eval(user_input)` — high severity finding"


class _FindingEvaluator:
    """First review round: one FAIL item carrying a finding."""

    def __init__(self, detail: str = _FINDING_DETAIL) -> None:
        self._detail = detail

    def evaluate(self, task: dict[str, object]) -> Verdict:
        return Verdict(
            verdict="INCOMPLETE",
            items=[
                VerdictItem(criterion="criterion", status="FAIL", detail=self._detail),
            ],
            summary="first-round finding",
        )


def _stage_config() -> dict[str, object]:
    return {
        "evaluator": {"prescreen": False, "max_iterations": 1},
        "pipeline": {
            "stages": [
                {
                    "id": "review",
                    "type": "ai_eval",
                    "condition": "true",
                    "on": ["pre-eval"],
                },
                {
                    "id": FIX_VERIFICATION_STAGE_ID,
                    "type": "fix_verification",
                    "condition": "true",
                    "on": ["pre-eval"],
                },
            ]
        },
    }


def _git(cwd, *args: str) -> None:
    env = dict(os.environ)
    env.update(
        {
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.com",
        }
    )
    subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
        env=env,
    )


def _init_repo(tmp_path, code: str) -> str:
    """A committed HEAD whose tree the test then modifies."""
    _git(tmp_path, "init", "-q")
    (tmp_path / "foo.py").write_text(code)
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "init")
    return str(tmp_path)


def _run(pipeline: Pipeline, evaluator: object):
    with patch("engine.evaluator.AgenticEvaluator", return_value=evaluator):
        return pipeline.run({"id": "task", "title": "task", "criteria": ["criterion"]})


def _stage(result: dict) -> dict:
    return result["stages"][FIX_VERIFICATION_STAGE_ID]


# ── AC2(a): flagged code removed by the judged diff → handled ──────────────


def test_removed_finding_reports_fixed_and_no_cap(tmp_path) -> None:
    workdir = _init_repo(tmp_path, "import sys\nx = eval(user_input)\n")
    # The fix: remove the flagged line in the working tree (the judged diff).
    (tmp_path / "foo.py").write_text("import sys\nx = safe_parse(user_input)\n")
    result = _run(Pipeline(_stage_config(), workdir, llm=object()), _FindingEvaluator())

    stage = _stage(result)
    assert stage["passed"] is True
    dispositions = stage["steps"][0]["data"]["dispositions"]
    assert len(dispositions) == 1
    assert dispositions[0]["disposition"] == "fixed"
    assert dispositions[0]["severity"] == "high"
    assert dispositions[0]["path"] == "foo.py"
    assert "no longer present" in dispositions[0]["evidence"]
    # handled → no needs_review cap anywhere on the run
    assert "verdict_cap" not in stage["steps"][0]["data"]
    assert "verdict_cap" not in stage
    assert "verdict_cap" not in result
    assert fix_verification_unhandled({"stages": result["stages"]}) == []


def test_removed_file_reports_fixed(tmp_path) -> None:
    """A finding whose whole file the judged diff deletes is 'fixed'."""
    workdir = _init_repo(tmp_path, "import sys\nx = eval(user_input)\n")
    _git(tmp_path, "rm", "-q", "foo.py")
    result = _run(Pipeline(_stage_config(), workdir, llm=object()), _FindingEvaluator())

    dispositions = _stage(result)["steps"][0]["data"]["dispositions"]
    assert dispositions[0]["disposition"] == "fixed"
    assert "removed with the file" in dispositions[0]["evidence"]


def test_handled_run_keeps_clean_merge_recommendation(tmp_path) -> None:
    """A judged pass with every finding 'fixed' reads no_concerns_found."""
    workdir = _init_repo(tmp_path, "import sys\nx = eval(user_input)\n")
    (tmp_path / "foo.py").write_text("import sys\nx = safe_parse(user_input)\n")
    result = _run(Pipeline(_stage_config(), workdir, llm=object()), _FindingEvaluator())

    record = {
        "stages": result["stages"],
        "verdict": "COMPLETE",
        "items": [{"criterion": "criterion", "status": "PASS", "detail": "graded"}],
        "pass_class": "judged",
    }
    assert merge_recommendation(record, ["criterion"]) == MERGE_RECOMMENDATION_OK


# ── AC2(b): flagged region untouched → unhandled + needs_review cap ────────


def test_untouched_finding_reports_not_fixed_and_caps(tmp_path) -> None:
    workdir = _init_repo(tmp_path, "import sys\nx = eval(user_input)\n")
    # The judged diff touches an unrelated file; foo.py's flagged line survives.
    (tmp_path / "notes.txt").write_text("unrelated change\n")
    _git(tmp_path, "add", ".")
    result = _run(Pipeline(_stage_config(), workdir, llm=object()), _FindingEvaluator())

    stage = _stage(result)
    assert stage["passed"] is True  # the stage reports; it does not hard-fail
    dispositions = stage["steps"][0]["data"]["dispositions"]
    assert dispositions[0]["disposition"] == "not_fixed"
    assert dispositions[0]["severity"] == "high"
    assert "region unchanged" in dispositions[0]["evidence"]
    # the cap rides the step data, the folded stage dict, and the run itself
    assert stage["steps"][0]["data"]["verdict_cap"] == "needs_review"
    assert stage["verdict_cap"] == "needs_review"
    assert result["verdict_cap"] == "needs_review"


def test_unhandled_finding_downgrades_merge_recommendation(tmp_path) -> None:
    """AC2(b) gate arm: the merge recommendation is needs_review even when
    every criterion item is PASS — the flagged code survives the diff."""
    workdir = _init_repo(tmp_path, "import sys\nx = eval(user_input)\n")
    result = _run(Pipeline(_stage_config(), workdir, llm=object()), _FindingEvaluator())

    record = {
        "stages": result["stages"],
        "verdict": "COMPLETE",
        "items": [{"criterion": "criterion", "status": "PASS", "detail": "graded"}],
        "pass_class": "judged",
    }
    unhandled = fix_verification_unhandled(record)
    assert [d["finding_id"] for d in unhandled] == ["review:item_0"]
    assert merge_recommendation(record, ["criterion"]) == MERGE_RECOMMENDATION_REVIEW


# ── severity semantics ─────────────────────────────────────────────────────


def test_low_severity_unhandled_finding_does_not_cap(tmp_path) -> None:
    workdir = _init_repo(tmp_path, "import sys\nx = eval(user_input)\n")
    evaluator = _FindingEvaluator("foo.py:2 `eval(user_input)` — low severity nit")
    result = _run(Pipeline(_stage_config(), workdir, llm=object()), evaluator)

    stage = _stage(result)
    dispositions = stage["steps"][0]["data"]["dispositions"]
    assert dispositions[0]["disposition"] == "not_fixed"
    assert dispositions[0]["severity"] == "low"
    assert "verdict_cap" not in stage
    assert "verdict_cap" not in result
    assert fix_verification_unhandled({"stages": result["stages"]}) == []


def test_unnamed_severity_unhandled_finding_still_caps(tmp_path) -> None:
    """Fail-closed: a finding that names no severity is never provably low."""
    workdir = _init_repo(tmp_path, "import sys\nx = eval(user_input)\n")
    evaluator = _FindingEvaluator("foo.py:2 `eval(user_input)` is dangerous")
    result = _run(Pipeline(_stage_config(), workdir, llm=object()), evaluator)

    stage = _stage(result)
    dispositions = stage["steps"][0]["data"]["dispositions"]
    assert dispositions[0]["disposition"] == "not_fixed"
    assert dispositions[0]["severity"] == ""
    assert stage["verdict_cap"] == "needs_review"
    assert result["verdict_cap"] == "needs_review"


# ── remaining dispositions ─────────────────────────────────────────────────


def test_moved_code_reports_partially_fixed_and_caps(tmp_path) -> None:
    """The region changed but the flagged code survives → partially_fixed."""
    workdir = _init_repo(tmp_path, "import sys\nx = eval(user_input)\n")
    # The flagged code moves down a line; the diff touches foo.py.
    (tmp_path / "foo.py").write_text("import sys\ny = 1\nx = eval(user_input)\n")
    result = _run(Pipeline(_stage_config(), workdir, llm=object()), _FindingEvaluator())

    dispositions = _stage(result)["steps"][0]["data"]["dispositions"]
    assert dispositions[0]["disposition"] == "partially_fixed"
    assert "survives" in dispositions[0]["evidence"]
    assert _stage(result)["verdict_cap"] == "needs_review"


def test_moved_to_another_file_reports_superseded(tmp_path) -> None:
    """The flagged code moved to another file of the judged change →
    superseded (invalidated by the upstream change), no cap."""
    workdir = _init_repo(tmp_path, "import sys\nx = eval(user_input)\n")
    # The flagged code leaves foo.py entirely and reappears in bar.py.
    (tmp_path / "foo.py").write_text("import sys\nx = safe_parse(user_input)\n")
    (tmp_path / "bar.py").write_text("x = eval(user_input)\n")
    _git(tmp_path, "add", ".")
    result = _run(Pipeline(_stage_config(), workdir, llm=object()), _FindingEvaluator())

    stage = _stage(result)
    dispositions = stage["steps"][0]["data"]["dispositions"]
    assert dispositions[0]["disposition"] == "superseded"
    assert "now lives at bar.py:1" in dispositions[0]["evidence"]
    assert "verdict_cap" not in stage
    assert "verdict_cap" not in result


def test_unresolvable_finding_reports_not_fixed_fail_closed(tmp_path) -> None:
    """No path in the finding text → not_fixed (an unverifiable finding is
    never reported handled), and it caps."""
    workdir = _init_repo(tmp_path, "import sys\nx = eval(user_input)\n")
    evaluator = _FindingEvaluator("somewhere the input handling is dangerous — high")
    result = _run(Pipeline(_stage_config(), workdir, llm=object()), evaluator)

    dispositions = _stage(result)["steps"][0]["data"]["dispositions"]
    assert dispositions[0]["disposition"] == "not_fixed"
    assert "could not be resolved" in dispositions[0]["evidence"]
    assert _stage(result)["verdict_cap"] == "needs_review"


# ── stage mechanics ────────────────────────────────────────────────────────


def test_no_prior_stage_context_returns_named_skip(tmp_path) -> None:
    """A fix_verification stage with no predecessor skips, named — it never
    reads as a graded verification."""
    config = {
        "pipeline": {
            "stages": [
                {
                    "id": FIX_VERIFICATION_STAGE_ID,
                    "type": "fix_verification",
                    "condition": "true",
                    "on": ["pre-eval"],
                }
            ]
        },
    }
    result = Pipeline(config, str(tmp_path)).run({"id": "t", "title": "t", "criteria": []})
    stage = _stage(result)
    step = stage["steps"][0]
    assert step["data"]["skipped"] is True
    assert "no prior stage" in step["output"]
    assert "dispositions" not in step["data"]


def test_default_pipeline_registers_fix_verification_after_tier2(tmp_path) -> None:
    config = load_pipeline_config(str(tmp_path))
    ids = [stage["id"] for stage in config["pipeline"]["stages"]]
    assert ids == ["tier1", "tier2", FIX_VERIFICATION_STAGE_ID]
    stage = config["pipeline"]["stages"][-1]
    assert stage["type"] == "fix_verification"
    assert "pre-eval" in stage["on"]


def test_declared_pipeline_stages_keep_working_unchanged(tmp_path) -> None:
    """A repo whose .gitreins/config.yaml declares pipeline.stages keeps its
    config verbatim; fix_verification is opt-in there (docs/fix-verification.md)."""
    (tmp_path / ".gitreins").mkdir()
    (tmp_path / ".gitreins" / "config.yaml").write_text(
        "pipeline:\n"
        "  stages:\n"
        "    - id: tier2\n"
        "      type: ai_eval\n"
        "      on: [pre-eval]\n"
        '      condition: "true"\n'
    )
    config = load_pipeline_config(str(tmp_path))
    assert [stage["id"] for stage in config["pipeline"]["stages"]] == ["tier2"]


# ── the gate reader, directly ──────────────────────────────────────────────


def test_fix_verification_unhandled_reader_shapes() -> None:
    capped_stage = {
        "id": "fix_verification",
        "dispositions": [
            {"finding_id": "a", "severity": "high", "disposition": "not_fixed", "evidence": "e"},
            {"finding_id": "b", "severity": "low", "disposition": "not_fixed", "evidence": "e"},
            {"finding_id": "c", "severity": "high", "disposition": "fixed", "evidence": "e"},
        ],
    }
    record = {"stages": {"fix_verification": capped_stage}}
    assert [d["finding_id"] for d in fix_verification_unhandled(record)] == ["a"]

    unnamed_stage = {
        "id": "fix_verification",
        "dispositions": [
            {
                "finding_id": "d",
                "severity": "",
                "disposition": "partially_fixed",
                "evidence": "e",
            }
        ],
    }
    assert fix_verification_unhandled({"stages": {"fix_verification": unnamed_stage}})

    assert fix_verification_unhandled({"stages": {}}) == []
    assert fix_verification_unhandled({}) == []
    assert fix_verification_unhandled({"stages": {"fix_verification": {}}}) == []
