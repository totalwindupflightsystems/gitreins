"""
Unit tests for engine/judge.py — Judge orchestrator and pipeline dispatch.
axiom:trace work_item=GR-001 spec=specs/07-Judge-Orchestrator.md plan=.memory-bank/work-items/GR-001/plan.yaml
"""
import pytest
from unittest import mock
from unittest.mock import MagicMock, PropertyMock, patch

from engine.judge import Judge, JudgeResult
from engine.guard_manager import GuardResult, Tier1Result
from engine.evaluator import Verdict, VerdictItem


# ── Phase 1-5-1: JudgeResult, legacy path ────────────────────────────────────


class TestJudgeResult:
    """Test JudgeResult dataclass and summary — step-1-5-1-1."""

    def test_judge_result_with_pipeline_result(self):
        """JudgeResult with pipeline_result → summary shows stage results."""
        pipeline = {
            "passed": True,
            "stages": {
                "tier1": {
                    "id": "tier1",
                    "passed": True,
                    "summary": "  ✓ secrets: ok\n  ✓ lint: ok"
                },
            },
        }
        result = JudgeResult(task_id="t1", passed=True, pipeline_result=pipeline)
        assert result.task_id == "t1"
        assert result.passed is True
        summary = result.summary
        assert "Stage tier1" in summary
        assert "PASS" in summary

    def test_judge_result_with_verdict_legacy(self):
        """JudgeResult with verdict (legacy) → summary shows verdict items."""
        items = [VerdictItem(criterion="c1", status="PASS", detail="ok")]
        verdict = Verdict(verdict="COMPLETE", items=items, summary="all good")
        result = JudgeResult(task_id="t1", passed=True, verdict=verdict)
        summary = result.summary
        assert "Tier 2" in summary or "Agentic Evaluator" in summary
        assert "COMPLETE" in summary

    def test_judge_result_both_pipeline_and_verdict(self):
        """JudgeResult with both pipeline and verdict shows both."""
        pipeline = {"passed": True, "stages": {}}
        items = [VerdictItem(criterion="c1", status="PASS", detail="ok")]
        verdict = Verdict(verdict="INCOMPLETE", items=items, summary="partial")
        result = JudgeResult(task_id="t1", passed=False, pipeline_result=pipeline, verdict=verdict)
        summary = result.summary
        assert "Judge Result" in summary
        assert "Overall" in summary

    def test_passed_true_shows_pass_check(self):
        """passed=True → 'PASS ✓' in summary."""
        result = JudgeResult(task_id="t1", passed=True)
        assert "PASS" in result.summary
        assert "✓" in result.summary

    def test_passed_false_shows_fail_cross(self):
        """passed=False → 'FAIL ✗' in summary."""
        result = JudgeResult(task_id="t1", passed=False)
        assert "FAIL" in result.summary
        assert "✗" in result.summary


class TestJudgeLegacyPath:
    """Test Judge._run_legacy — Tier 1 → Tier 2 — step-1-5-1-2."""

    def test_legacy_guards_pass_tier2_runs(self, judge, llm_client):
        """When guards pass, Tier 2 evaluator is called."""
        from engine.task_manager import Task
        task = Task(id="t1", title="Test", criteria=["c1"])

        # Mock guards to pass
        tier1_pass = Tier1Result(passed=True, results=[
            GuardResult("secrets", True, "ok"),
            GuardResult("lint", True, "ok"),
            GuardResult("tests", True, "ok"),
        ])
        with patch.object(judge.guard_manager, 'run_all', return_value=tier1_pass):
            # Mock evaluator to return COMPLETE
            verdict_json = '{"verdict":"COMPLETE","items":[{"criterion":"c1","status":"PASS","detail":"verified"}],"summary":"good"}'
            mock_resp = mock.MagicMock(content=verdict_json, tool_calls=None)
            with patch.object(llm_client, 'chat', return_value=mock_resp):
                result = judge._run_legacy(task)
        assert result.passed is True
        assert result.verdict is not None
        assert result.verdict.verdict == "COMPLETE"
        # BUGFIX: tier1 and tier2 are now populated
        assert result.tier1 is not None
        assert result.tier1.passed is True
        assert result.tier2 is not None
        assert result.tier2.verdict == "COMPLETE"

    def test_legacy_guards_fail_tier2_skipped(self, judge):
        """When guards fail, Tier 2 is skipped, result.passed=False."""
        from engine.task_manager import Task
        task = Task(id="t1", title="Test", criteria=["c1"])

        # Mock guards to fail
        tier1_fail = Tier1Result(passed=False, results=[
            GuardResult("secrets", False, "key detected"),
        ])
        with patch.object(judge.guard_manager, 'run_all', return_value=tier1_fail):
            result = judge._run_legacy(task)
        assert result.passed is False
        assert result.pipeline_result == {}
        assert result.tier1 is not None
        assert result.tier1.passed is False
        assert result.tier2 is None  # Skipped because guards failed


class TestJudgeEvaluateTask:
    """Test Judge.evaluate_task() pipeline vs legacy dispatch."""

    def test_evaluate_task_uses_pipeline_when_config_has_stages(self, judge, tmp_workdir):
        """evaluate_task runs pipeline when pipeline config has stages."""
        from engine.task_manager import Task
        # Write a config with pipeline stages
        import os, yaml
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
                            {"id": "secrets", "type": "script",
                             "run": "echo clean", "on_fail": "continue"},
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

    def test_evaluate_task_falls_back_to_legacy_without_config(self, judge, llm_client):
        """evaluate_task falls back to legacy when no pipeline config exists."""
        from engine.task_manager import Task
        task = Task(id="t1", title="Test", criteria=["c1"])

        tier1_pass = Tier1Result(passed=True, results=[
            GuardResult("secrets", True, "ok"),
            GuardResult("lint", True, "ok"),
            GuardResult("tests", True, "ok"),
        ])
        with patch.object(judge.guard_manager, 'run_all', return_value=tier1_pass):
            verdict_json = '{"verdict":"COMPLETE","items":[{"criterion":"c1","status":"PASS","detail":"ok"}],"summary":"done"}'
            mock_resp = mock.MagicMock(content=verdict_json, tool_calls=None)
            with patch.object(llm_client, 'chat', return_value=mock_resp):
                result = judge.evaluate_task(task)
        assert result.passed is True

    def test_run_precommit_runs_pipeline(self, judge, tmp_workdir):
        """run_precommit runs pipeline with trigger='pre-commit'."""
        import os, yaml
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
                            {"id": "secrets", "type": "script",
                             "run": "echo clean", "on_fail": "continue"},
                        ],
                    },
                ],
            }
        }
        with open(os.path.join(config_dir, "config.yaml"), "w") as f:
            yaml.dump(config, f)

        result = judge.run_precommit()
        assert result is True


class TestJudgeInit:
    """Test Judge initialization."""

    def test_judge_constructor_creates_guard_manager(self, judge):
        """Judge constructor creates a GuardManager."""
        assert judge.guard_manager is not None

    def test_judge_constructor_accepts_guard_config(self, tmp_workdir, llm_client):
        """Judge constructor accepts guard_config dict."""
        judge = Judge(llm_client, tmp_workdir, guard_config={"guards": {"secrets": False}})
        assert judge.guard_manager._enabled["secrets"] is False


class TestExtendedJudge:
    """Extended coverage for Judge module."""

    def test_judge_result_empty_pipeline(self):
        """JudgeResult with empty pipeline_result shows Overall."""
        result = JudgeResult(task_id="t1")
        assert result.task_id == "t1"
        assert result.passed is False
        summary = result.summary
        assert "Overall" in summary

    def test_judge_result_no_pipeline_no_verdict(self):
        """JudgeResult without pipeline or verdict still produces summary."""
        result = JudgeResult(task_id="t1", passed=False)
        summary = result.summary
        assert "Judge Result: t1" in summary
        assert "FAIL" in summary

    def test_evaluate_task_pipeline_exception_returns_error(self, judge, tmp_workdir, llm_client):
        """evaluate_task catches pipeline exception and returns error in result."""
        from engine.task_manager import Task
        import os, yaml
        config_dir = os.path.join(tmp_workdir, ".gitreins")
        os.makedirs(config_dir, exist_ok=True)
        config = {
            "pipeline": {
                "stages": [
                    {"id": "bad", "parallel": True, "on": ["pre-eval"],
                     "steps": [{"id": "x", "type": "script", "run": "exit 1"}]},
                ],
            }
        }
        with open(os.path.join(config_dir, "config.yaml"), "w") as f:
            yaml.dump(config, f)

        task = Task(id="t1", title="Test", criteria=["c1"])
        result = judge.evaluate_task(task)
        assert result.pipeline_result is not None

    def test_judge_result_summary_with_failed_stage(self):
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

    def test_judge_result_tier2_verdict_items_shown(self):
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

    def test_judge_result_stores_tier1_and_tier2(self):
        """BUGFIX: JudgeResult.tier1 and .tier2 are populated by _run_legacy."""
        from engine.judge import Judge, JudgeResult
        from engine.llm import LLMClient

        verdict = Verdict(verdict="COMPLETE", items=[
            VerdictItem(criterion="c1", status="PASS", detail="ok")
        ], summary="all good")
        tier1 = Tier1Result(passed=True, results=[
            GuardResult(name="secrets", passed=True, output="clean")
        ])

        result = JudgeResult(task_id="t4", passed=True, tier1=tier1, tier2=verdict)
        assert result.tier1 is not None
        assert result.tier1.passed is True
        assert result.tier2 is not None
        assert result.tier2.verdict == "COMPLETE"

    def test_tier1_none_safe_access(self):
        """BUGFIX: tier1 is None safe (e.g. pipeline path)."""
        result = JudgeResult(task_id="t5", passed=True)
        assert result.tier1 is None
        assert result.tier2 is None
        # The MCP handler pattern: result.tier1.passed if result.tier1 else None
        tier1_passed = result.tier1.passed if result.tier1 else None
        assert tier1_passed is None
