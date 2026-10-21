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
from pathlib import Path
from unittest import mock

import yaml

from engine.evaluator import AgenticEvaluator, item_glyph
from engine.judge import Judge
from engine.llm import LLMClient
from engine.pipeline import Pipeline
from engine.task_manager import TaskManager


def _make_workdir(pipeline_stages: object = None) -> object:
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

    def test_exception_returns_fail_by_default(self, tmp_workdir: str) -> None:
        wd = _make_workdir(pipeline_stages=[{"id": "t1", "type": "script", "run": "exit 3"}])
        task = TaskManager(wd).create("t1", "T1", ["c1"])
        judge = Judge(LLMClient(), wd)
        with mock.patch("engine.pipeline.Pipeline.run", side_effect=RuntimeError("boom")):
            result = judge.evaluate_task(task)
        assert result.passed is False

    def test_exception_passes_only_with_pass_on_error(self, tmp_workdir: str) -> None:
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

    def test_failing_script_with_continue_reports_failed(self, tmp_workdir: str) -> None:
        stages = [
            {"id": "s", "type": "script", "run": "exit 1", "on_fail": "continue"},
        ]
        p = Pipeline({"pipeline": {"stages": stages}}, tmp_workdir)
        res = p.run({"id": "x", "title": "x", "criteria": []}, trigger="pre-eval")
        step = res["stages"]["s"]["steps"][0]
        assert step["passed"] is False
        assert res["stages"]["s"]["passed"] is False

    def test_timeout_with_continue_reports_failed(self, tmp_workdir: str) -> None:
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

    def test_no_exit_zeroing_suffixes(self) -> None:
        """DF-GITREINS-POC-16: the tables live in engine.lang_detect — assert
        on the real table values (no suffix may swallow a failure)."""
        from engine.lang_detect import LANG_COMMANDS

        assert LANG_COMMANDS, "language command table must not be empty"
        for language, (lint_cmd, test_cmd) in LANG_COMMANDS.items():
            for cmd in (lint_cmd, test_cmd):
                assert "|| true" not in cmd, f"{language} zeroes its exit code: {cmd}"
                assert "2>/dev/null" not in cmd, f"{language} swallows its output: {cmd}"

    def test_detection_tables_defined_once(self) -> None:
        """One language-detection source of truth (DF-GITREINS-POC-16).

        The signature-file table and the language->command map must be DEFINED
        only in engine/lang_detect.py; every other module imports them.
        """
        import re

        import engine.lang_detect as lang_detect_mod

        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(lang_detect_mod.__file__)))
        pattern = re.compile(r"^(LANG_COMMANDS|SIGNATURE_FILES)\s*[:=]", re.MULTILINE)
        offenders: list[str] = []
        for base in ("engine", "gitreins"):
            for root, dirs, files in os.walk(os.path.join(repo_root, base)):
                dirs[:] = [d for d in dirs if d != "__pycache__"]
                for name in files:
                    if not name.endswith(".py"):
                        continue
                    path = os.path.join(root, name)
                    if os.path.abspath(path) == os.path.abspath(lang_detect_mod.__file__):
                        continue
                    with open(path, encoding="utf-8", errors="replace") as f:
                        if pattern.search(f.read()):
                            offenders.append(os.path.relpath(path, repo_root))
        assert offenders == [], (
            f"language tables re-defined outside engine/lang_detect.py: {offenders}"
        )


class TestPartialVerdictRequiresAllPass:
    """A cap-hit partial verdict must be INCOMPLETE unless ALL criteria PASS."""

    def test_partial_verdict_with_unverified_criteria_is_incomplete(self, tmp_workdir: str) -> None:
        ev = AgenticEvaluator(LLMClient(), tmp_workdir)
        ev._sandbox = {"verified_0": "PASS — pytest: 1 passed, exit 0"}
        verdict = ev._extract_partial_verdict(["c0", "c1"])
        assert verdict is not None
        assert verdict.verdict == "INCOMPLETE"

    def test_partial_verdict_all_verified_pass_is_complete(self, tmp_workdir: str) -> None:
        ev = AgenticEvaluator(LLMClient(), tmp_workdir)
        ev._sandbox = {
            "verified_0": "PASS — pytest: 1 passed, exit 0",
            "verified_1": "PASS — grep found handler",
        }
        verdict = ev._extract_partial_verdict(["c0", "c1"])
        assert verdict is not None
        assert verdict.verdict == "COMPLETE"

    def test_partial_verdict_any_fail_is_incomplete(self, tmp_workdir: str) -> None:
        ev = AgenticEvaluator(LLMClient(), tmp_workdir)
        ev._sandbox = {
            "verified_0": "PASS — pytest: 1 passed, exit 0",
            "verified_1": "FAIL — handler not found",
        }
        verdict = ev._extract_partial_verdict(["c0", "c1"])
        assert verdict is not None
        assert verdict.verdict == "INCOMPLETE"


class TestUnreachedCriterionIsNotAFailure:
    """A criterion the run never reached is UNVERIFIED, never FAIL.

    Measured defect (DF-CRIER-461, run 68649f1e): the 75m cap fired after one
    criterion passed, the unreached one was written status="FAIL" with detail
    "Not verified", and every renderer printed ✗ —
    indistinguishable from a criterion that was examined and found false.
    """

    def test_unreached_criterion_status_is_unverified(self, tmp_workdir: str) -> None:
        ev = AgenticEvaluator(LLMClient(), tmp_workdir)
        ev._sandbox = {"verified_0": "PASS — pytest: 1 passed, exit 0"}
        verdict = ev._extract_partial_verdict(["c0", "c1"])
        assert verdict is not None
        unreached = verdict.items[1]
        assert unreached.status == "UNVERIFIED", unreached.status
        assert "Not examined" in unreached.detail
        assert "Not verified" not in unreached.detail

    def test_unverified_criterion_still_fails_closed(self, tmp_workdir: str) -> None:
        ev = AgenticEvaluator(LLMClient(), tmp_workdir)
        ev._sandbox = {"verified_0": "PASS — pytest: 1 passed, exit 0"}
        verdict = ev._extract_partial_verdict(["c0", "c1"])
        assert verdict is not None
        assert verdict.verdict == "INCOMPLETE"

    def test_glyphs_distinguish_not_examined_from_false(self) -> None:
        assert item_glyph("PASS") == "✓"
        assert item_glyph("FAIL") == "✗"
        assert item_glyph("UNVERIFIED") == "—"
        assert item_glyph("") == "—"
        assert item_glyph(None) == "—"

    def test_no_renderer_hardcodes_pass_else_fail(self) -> None:
        """Every renderer must go through item_glyph.

        The bug was not one site: judge.py, pipeline.py and persist.py each
        carried their own `"✓" if status == "PASS" else "✗"`, so fixing one
        would have left two lies in place. This pins all three.
        """
        here = Path(__file__).resolve().parents[1]
        offenders = []
        for name in ("judge.py", "pipeline.py", "persist.py"):
            text = (here / "engine" / name).read_text(encoding="utf-8")
            if '"PASS" else "✗"' in text or "'PASS' else '✗'" in text:
                offenders.append(name)
        assert offenders == [], f"renderers still binary-folding status: {offenders}"
        assert "item_glyph" in (here / "engine" / "judge.py").read_text(encoding="utf-8")
        assert "item_glyph" in (here / "engine" / "pipeline.py").read_text(encoding="utf-8")
        assert "item_glyph" in (here / "engine" / "persist.py").read_text(encoding="utf-8")
