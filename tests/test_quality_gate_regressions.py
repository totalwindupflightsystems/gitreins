"""
Regression tests for the four "PASS-but-failing" quality-gate bugs fixed in
PR #2 (carterlasalle, merged 2026-08-16, merge commit 4779fd2).

Each test reproduces a path where the harness could report PASS while a gate
had actually failed:

1. Pipeline exceptions returned passed=True even with pass_on_error=False.
2. A failing script step with `on_fail: continue` was marked passed.
3. Default tier-1 lint/test commands carried `2>/dev/null || true`, zeroing
   exit codes.
4. Partial verdicts reported COMPLETE whenever ANY criterion was verified,
   even when the rest were FAIL ("Not verified").

The tests assert the FIXED behavior, so any future regression fails loudly.
"""

import os
import tempfile
from unittest import mock

import yaml

from engine.evaluator import AgenticEvaluator
from engine.judge import Judge
from engine.llm import LLMClient
from engine.pipeline import Pipeline
from engine.task_manager import TaskManager


def _make_workdir(pipeline_stages=None):
    wd = tempfile.mkdtemp()
    os.makedirs(os.path.join(wd, ".gitreins"))
    cfg = {}
    if pipeline_stages is not None:
        cfg["pipeline"] = {"stages": pipeline_stages}
    with open(os.path.join(wd, ".gitreins", "config.yaml"), "w") as f:
        yaml.safe_dump(cfg, f)
    return wd


class TestPipelineExceptionNotAutoPass:
    """A pipeline crash must NOT pass the task unless pass_on_error is set."""

    def test_exception_returns_fail_by_default(self, tmp_workdir):
        wd = _make_workdir(pipeline_stages=[{"id": "t1", "type": "script", "run": "exit 3"}])
        task = TaskManager(wd).create("t1", "T1", ["c1"])
        judge = Judge(LLMClient(), wd)
        with mock.patch("engine.pipeline.Pipeline.run", side_effect=RuntimeError("boom")):
            result = judge.evaluate_task(task)
        assert result.passed is False

    def test_exception_passes_only_with_pass_on_error(self, tmp_workdir):
        wd = tempfile.mkdtemp()
        os.makedirs(os.path.join(wd, ".gitreins"))
        with open(os.path.join(wd, ".gitreins", "config.yaml"), "w") as f:
            yaml.safe_dump(
                {
                    "pipeline": {"stages": [{"id": "t1", "type": "script", "run": "exit 3"}]},
                    "defaults": {"pass_on_error": True},
                },
                f,
            )
        task = TaskManager(wd).create("t1", "T1", ["c1"])
        judge = Judge(LLMClient(), wd)
        with mock.patch("engine.pipeline.Pipeline.run", side_effect=RuntimeError("boom")):
            result = judge.evaluate_task(task)
        assert result.passed is True


class TestOnFailContinueIsNotPass:
    """on_fail: continue controls continuation, never the pass/fail verdict."""

    def test_failing_script_with_continue_reports_failed(self, tmp_workdir):
        stages = [
            {"id": "s", "type": "script", "run": "exit 1", "on_fail": "continue"},
        ]
        p = Pipeline({"pipeline": {"stages": stages}}, tmp_workdir)
        res = p.run({"id": "x", "title": "x", "criteria": []}, trigger="pre-eval")
        step = res["stages"]["s"]["steps"][0]
        assert step["passed"] is False
        assert res["stages"]["s"]["passed"] is False

    def test_timeout_with_continue_reports_failed(self, tmp_workdir):
        stages = [
            {
                "id": "s",
                "type": "script",
                "run": "sleep 30",
                "timeout": "1s",
                "on_fail": "continue",
            },
        ]
        p = Pipeline({"pipeline": {"stages": stages}}, tmp_workdir)
        res = p.run({"id": "x", "title": "x", "criteria": []}, trigger="pre-eval")
        step = res["stages"]["s"]["steps"][0]
        assert step["passed"] is False


class TestDefaultTier1CommandsPreserveExitCodes:
    """Default lint/test commands must not zero their exit codes."""

    def test_no_exit_zeroing_suffixes(self):
        import engine.pipeline as pipeline_mod

        src_path = os.path.join(os.path.dirname(pipeline_mod.__file__), "pipeline.py")
        with open(src_path) as f:
            src = f.read()
        block = src.split("_LANG_COMMANDS")[1].split("Detection order")[0]
        assert "|| true" not in block
        assert "2>/dev/null" not in block


class TestPartialVerdictRequiresAllPass:
    """A cap-hit partial verdict must be INCOMPLETE unless ALL criteria PASS."""

    def test_partial_verdict_with_unverified_criteria_is_incomplete(self, tmp_workdir):
        ev = AgenticEvaluator(LLMClient(), tmp_workdir)
        ev._sandbox = {"verified_0": "PASS — pytest: 1 passed, exit 0"}
        verdict = ev._extract_partial_verdict(["c0", "c1"])
        assert verdict is not None
        assert verdict.verdict == "INCOMPLETE"

    def test_partial_verdict_all_verified_pass_is_complete(self, tmp_workdir):
        ev = AgenticEvaluator(LLMClient(), tmp_workdir)
        ev._sandbox = {
            "verified_0": "PASS — pytest: 1 passed, exit 0",
            "verified_1": "PASS — grep found handler",
        }
        verdict = ev._extract_partial_verdict(["c0", "c1"])
        assert verdict is not None
        assert verdict.verdict == "COMPLETE"

    def test_partial_verdict_any_fail_is_incomplete(self, tmp_workdir):
        ev = AgenticEvaluator(LLMClient(), tmp_workdir)
        ev._sandbox = {
            "verified_0": "PASS — pytest: 1 passed, exit 0",
            "verified_1": "FAIL — handler not found",
        }
        verdict = ev._extract_partial_verdict(["c0", "c1"])
        assert verdict is not None
        assert verdict.verdict == "INCOMPLETE"
