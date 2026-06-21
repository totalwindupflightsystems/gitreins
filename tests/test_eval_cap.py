"""
Tests for EvalCap: parser, limit checking, tool-call weighting, and real LLM integration.

Run fast tests only:  pytest tests/test_eval_cap.py -m "not llm"
"""
import os
import time

import pytest
import yaml

from engine.eval_cap import (
    EvalCap, parse_eval_cap, eval_cap_from_config,
)
from engine.evaluator import AgenticEvaluator
from engine.llm import LLMClient


# ═══════════════════════════════════════════════════════════════
# Parser tests
# ═══════════════════════════════════════════════════════════════

class TestEvalCapParser:
    """Parse individual values and legacy combined strings."""

    def test_numeric_iteration(self):
        cap = parse_eval_cap("100")
        assert cap.max_iterations == 100
        assert cap.max_seconds == -1
        assert cap.max_input_tokens == -1
        assert cap.max_output_tokens == -1

    def test_unlimited(self):
        for val in ("-1", "0", "unlimited", "none"):
            cap = parse_eval_cap(val)
            assert cap.max_iterations == -1.0, f"failed for {val}"
            assert cap.is_unlimited, f"failed for {val}"

    def test_time_only(self):
        assert parse_eval_cap("30s").max_seconds == 30
        assert parse_eval_cap("5m").max_seconds == 300
        assert parse_eval_cap("2h").max_seconds == 7200

    def test_output_tokens_only(self):
        assert parse_eval_cap("200k").max_output_tokens == 200_000
        assert parse_eval_cap("50k").max_output_tokens == 50_000

    def test_million_tokens(self):
        cap = parse_eval_cap("1M")
        assert cap.max_output_tokens == 1_000_000
        assert cap.max_seconds == -1

    def test_slash_token_pair(self):
        cap = parse_eval_cap("100k/50k")
        assert cap.max_input_tokens == 100_000
        assert cap.max_output_tokens == 50_000

    def test_iter_and_time(self):
        cap = parse_eval_cap("100/30m")
        assert cap.max_iterations == 100
        assert cap.max_seconds == 1800

    def test_unlimited_iter_with_time(self):
        cap = parse_eval_cap("-1/30m")
        assert cap.max_iterations == -1.0
        assert cap.max_seconds == 1800

    def test_decimal_tokens(self):
        assert parse_eval_cap("0.1M").max_output_tokens == 100_000
        assert parse_eval_cap("0.5M").max_output_tokens == 500_000
        assert parse_eval_cap("1.5M").max_output_tokens == 1_500_000
        assert parse_eval_cap("0.1k").max_output_tokens == 100

    def test_decimal_slash_tokens(self):
        cap = parse_eval_cap("0.1M/0.05M")
        assert cap.max_input_tokens == 100_000
        assert cap.max_output_tokens == 50_000


# ═══════════════════════════════════════════════════════════════
# Individual config key tests (v0.3.0+)
# ═══════════════════════════════════════════════════════════════

class TestEvalCapFromConfig:
    """New individual config keys + backward compat."""

    def test_individual_keys(self):
        config = {
            "evaluator": {
                "max_iterations": 50,
                "max_time": "30m",
                "max_input_tokens": "200k",
                "max_output_tokens": "100k",
                "tool_call_weight": 0.05,
            }
        }
        cap = eval_cap_from_config(config)
        assert cap.max_iterations == 50
        assert cap.max_seconds == 1800
        assert cap.max_input_tokens == 200_000
        assert cap.max_output_tokens == 100_000
        assert cap.tool_call_weight == 0.05

    def test_individual_override_legacy(self):
        """Individual keys take priority over legacy cap string."""
        config = {
            "evaluator": {
                "cap": "999/99h/999k/999k",
                "max_iterations": 50,
            }
        }
        cap = eval_cap_from_config(config)
        assert cap.max_iterations == 50  # individual wins
        assert cap.max_seconds == 356400  # legacy time still applies
        assert cap.max_input_tokens == 999_000  # legacy tokens still apply
        assert cap.max_output_tokens == 999_000

    def test_legacy_only(self):
        config = {"evaluator": {"cap": "25/10m/200k/100k"}}
        cap = eval_cap_from_config(config)
        assert cap.max_iterations == 25
        assert cap.max_seconds == 600
        assert cap.max_input_tokens == 200_000
        assert cap.max_output_tokens == 100_000

    def test_guards_fallback(self):
        config = {"guards": {"eval_cap": "100k/50k"}}
        cap = eval_cap_from_config(config)
        assert cap.max_input_tokens == 100_000
        assert cap.max_output_tokens == 50_000

    def test_empty_defaults(self):
        cap = eval_cap_from_config({})
        assert cap.max_iterations == 100.0
        assert cap.max_input_tokens == 10_000_000
        assert cap.max_output_tokens == 1_000_000


# ═══════════════════════════════════════════════════════════════
# Cap checking and tool-call weighting
# ═══════════════════════════════════════════════════════════════

class TestEvalCapChecking:
    """Test the new record_llm_call / record_tool_call API."""

    def test_llm_call_costs_one(self):
        cap = EvalCap(max_iterations=10)
        cap.start()
        for _ in range(5):
            err = cap.record_llm_call()
            assert err is None
        assert cap.iteration_credit == 5.0

    def test_llm_call_exceeded(self):
        cap = EvalCap(max_iterations=3)
        cap.start()
        for _ in range(3):
            assert cap.record_llm_call() is None
        err = cap.record_llm_call()
        assert err is not None
        assert "Iteration cap" in err

    def test_tool_call_costs_fraction(self):
        cap = EvalCap(max_iterations=10, tool_call_weight=0.1)
        cap.start()
        cap.record_llm_call()  # 1.0
        cap.record_tool_call()  # 1.1
        cap.record_tool_call()  # 1.2
        cap.record_llm_call()   # 2.2
        assert cap.iteration_credit == 2.2

    def test_tool_call_custom_weight(self):
        cap = EvalCap(max_iterations=10, tool_call_weight=0.5)
        cap.start()
        cap.record_llm_call()  # 1.0
        cap.record_tool_call()  # 1.5
        cap.record_tool_call()  # 2.0
        assert cap.iteration_credit == 2.0

    def test_lenient_overflow(self):
        """At 99.9/100, a full LLM call (1.0) is allowed — brings you to 100.9."""
        cap = EvalCap(max_iterations=100, tool_call_weight=0.1)
        cap.start()
        # Simulate: 99 LLM calls + 9 tool calls = 99.9
        for _ in range(99):
            cap.record_llm_call()
        for _ in range(9):
            cap.record_tool_call()
        assert cap.iteration_credit == pytest.approx(99.9)
        # One more full call should be allowed
        err = cap.record_llm_call()
        assert err is None
        assert cap.iteration_credit == pytest.approx(100.9)
        # Now we're over — next call is blocked
        err = cap.record_llm_call()
        assert err is not None
        assert "Iteration cap" in err

    def test_time_cap_exceeded(self):
        cap = EvalCap(max_seconds=1)
        cap.start()
        time.sleep(1.1)
        err = cap.check()
        assert err is not None
        assert "Time cap" in err

    def test_input_tokens_exceeded(self):
        cap = EvalCap(max_input_tokens=1000)
        cap.start()
        err = cap.record_llm_call(prompt_tokens=600)
        assert err is None
        err = cap.record_llm_call(prompt_tokens=500)
        assert err is not None
        assert "Input token budget" in err

    def test_output_tokens_exceeded(self):
        cap = EvalCap(max_output_tokens=500)
        cap.start()
        err = cap.record_llm_call(completion_tokens=300)
        assert err is None
        err = cap.record_llm_call(completion_tokens=300)
        assert err is not None
        assert "Output token budget" in err

    def test_unlimited_never_exceeds(self):
        cap = EvalCap()
        cap.start()
        for _ in range(1000):
            assert cap.record_llm_call(prompt_tokens=1000, completion_tokens=1000) is None
            assert cap.record_tool_call() is None

    def test_summary_format(self):
        cap = EvalCap(max_iterations=100, max_seconds=300,
                       max_input_tokens=200000, max_output_tokens=50000,
                       tool_call_weight=0.1)
        cap.start()
        cap.record_llm_call(prompt_tokens=50000, completion_tokens=10000)
        cap.record_tool_call()
        s = cap.summary()
        assert "iterations:" in s
        assert "time:" in s
        assert "in:" in s
        assert "out:" in s

    def test_summary_shows_cache(self):
        """Cache hits and writes show in summary."""
        cap = EvalCap(max_input_tokens=1_000_000, max_output_tokens=500_000)
        cap.start()
        cap.record_llm_call(
            prompt_tokens=100_000,
            completion_tokens=50_000,
            cache_read_tokens=200_000,
            cache_write_tokens=50_000,
        )
        s = cap.summary()
        assert "cache-hit 200k" in s
        assert "cache-write 50k" in s

    def test_cache_tokens_count_toward_input_budget(self):
        """Cache tokens are included in the input token budget."""
        cap = EvalCap(max_input_tokens=1_000, max_output_tokens=10_000)
        cap.start()
        # 400 regular + 299 cache read + 300 cache write = 999 total input
        err = cap.record_llm_call(
            prompt_tokens=400,
            cache_read_tokens=299,
            cache_write_tokens=300,
        )
        assert err is None  # still under cap
        # Next call should exceed
        err = cap.record_llm_call(prompt_tokens=1)
        assert err is not None
        assert "Input token budget" in err

    def test_cache_tracking_fields(self):
        """EvalCap tracks cache reads and writes separately."""
        cap = EvalCap(max_input_tokens=1_000_000)
        cap.start()
        cap.record_llm_call(
            prompt_tokens=100_000,
            cache_read_tokens=50_000,
            cache_write_tokens=25_000,
        )
        cap.record_llm_call(
            prompt_tokens=80_000,
            cache_read_tokens=40_000,
            cache_write_tokens=10_000,
        )
        assert cap.cumulative_cache_read == 90_000
        assert cap.cumulative_cache_write == 35_000
        assert cap.cumulative_input_tokens == 305_000  # 175k (call1) + 130k (call2) = 305k


# ═══════════════════════════════════════════════════════════════
# Evaluator integration tests
# ═══════════════════════════════════════════════════════════════

class TestAgenticEvaluatorCapIntegration:
    """AgenticEvaluator accepts EvalCap objects and strings."""

    def test_evaluator_accepts_eval_cap_string(self):
        llm = LLMClient()
        evaluator = AgenticEvaluator(llm, eval_cap="50")
        assert evaluator.eval_cap.max_iterations == 50

    def test_evaluator_accepts_eval_cap_object(self):
        llm = LLMClient()
        cap = EvalCap(max_iterations=42, tool_call_weight=0.2)
        evaluator = AgenticEvaluator(llm, eval_cap=cap)
        assert evaluator.eval_cap.max_iterations == 42
        assert evaluator.eval_cap.tool_call_weight == 0.2

    def test_evaluator_still_accepts_max_iterations(self):
        llm = LLMClient()
        evaluator = AgenticEvaluator(llm, max_iterations=42)
        assert evaluator.eval_cap.max_iterations == 42

    def test_eval_cap_wins_over_max_iterations(self):
        llm = LLMClient()
        evaluator = AgenticEvaluator(llm, max_iterations=42, eval_cap="10")
        assert evaluator.eval_cap.max_iterations == 10

    def test_max_iterations_negative_defers_to_config(self, tmp_path):
        """When max_iterations=-1 (from Pipeline default), evaluator reads config."""
        gitreins_dir = tmp_path / ".gitreins"
        gitreins_dir.mkdir()
        config = {"evaluator": {"max_iterations": 30, "max_time": "5m"}}
        with open(gitreins_dir / "config.yaml", "w") as f:
            yaml.dump(config, f)
        llm = LLMClient()
        evaluator = AgenticEvaluator(llm, workdir=str(tmp_path), max_iterations=-1)
        assert evaluator.eval_cap.max_iterations == 30
        assert evaluator.eval_cap.max_seconds == 300

    def test_evaluator_reads_config_yaml(self, tmp_path):
        gitreins_dir = tmp_path / ".gitreins"
        gitreins_dir.mkdir()
        config = {"evaluator": {"max_iterations": 25, "max_time": "10m"}}
        with open(gitreins_dir / "config.yaml", "w") as f:
            yaml.dump(config, f)
        llm = LLMClient()
        evaluator = AgenticEvaluator(llm, workdir=str(tmp_path))
        assert evaluator.eval_cap.max_iterations == 25
        assert evaluator.eval_cap.max_seconds == 600


# ═══════════════════════════════════════════════════════════════
# Real LLM integration — caps actually stop the evaluator
# ═══════════════════════════════════════════════════════════════

class TestEvalCapRealEvaluator:
    """These run the full evaluator loop with a real LLM and tight caps.

    Skip with: pytest -m "not llm"
    """

    @pytest.fixture(autouse=True)
    def _require_llm_key(self):
        if os.getenv("GITREINS_REAL_LLM_TESTS") != "1":
            pytest.skip("GITREINS_REAL_LLM_TESTS=1 not set")
        api_key = os.getenv("GITREINS_LLM_API_KEY", "") or os.getenv("DEEPSEEK_API_KEY", "")
        if not api_key:
            pytest.skip("No LLM API key configured")

    def _make_repo(self, tmp_path):
        import subprocess
        d = str(tmp_path)
        subprocess.run(["git", "init"], cwd=d, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@evalcap.test"], cwd=d, capture_output=True)
        subprocess.run(["git", "config", "user.name", "EvalCap Test"], cwd=d, capture_output=True)
        gitreins_dir = os.path.join(d, ".gitreins")
        os.makedirs(gitreins_dir, exist_ok=True)
        config = {"guards": {"secrets": False, "lint": False, "tests": False, "dead_code": False, "skylos": False}}
        with open(os.path.join(gitreins_dir, "config.yaml"), "w") as f:
            yaml.dump(config, f)
        with open(os.path.join(d, "README.md"), "w") as f:
            f.write("# Test Repo\n")
        subprocess.run(["git", "add", "README.md", ".gitreins/"], cwd=d, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=d, capture_output=True)
        task = {
            "id": "simple-check",
            "title": "Simple file check",
            "criteria": ["README.md exists in the repo root with content '# Test Repo'"],
        }
        return d, task

    def test_iteration_cap_stops_evaluator(self, tmp_path):
        """cap=2 — evaluator MUST stop after 2 LLM calls."""
        d, task = self._make_repo(tmp_path)
        llm = LLMClient()
        evaluator = AgenticEvaluator(llm, workdir=d, eval_cap="2")
        verdict = evaluator.evaluate(task)
        assert verdict.verdict == "INCOMPLETE"
        assert "Cap exceeded" in verdict.summary
        assert "LLM call failed" not in verdict.summary
        assert evaluator.eval_cap.iteration_credit >= 2.0

    def test_time_cap_stops_evaluator(self, tmp_path):
        """cap=5s — with many criteria, evaluator MUST time out."""
        d, task = self._make_repo(tmp_path)
        # Many criteria to keep the evaluator busy
        task["criteria"] = [
            "README.md exists in the repo root",
            "The file .gitreins/config.yaml exists and is valid YAML",
            "The repo has at least one git commit",
            "README.md has non-empty content",
            "The repo root is a valid git repository",
            "No binary files exist in the repo",
            "The git log shows the initial commit",
            "The config.yaml contains guards section",
        ]
        llm = LLMClient()
        evaluator = AgenticEvaluator(llm, workdir=d, eval_cap="5s")
        verdict = evaluator.evaluate(task)
        assert verdict.verdict == "INCOMPLETE"
        assert "Cap exceeded" in verdict.summary
        assert "Time cap" in verdict.summary
        assert evaluator.eval_cap.iteration_credit >= 1.0

    def test_unlimited_completes_normally(self, tmp_path):
        """cap=-1 — simple task should complete."""
        d, task = self._make_repo(tmp_path)
        llm = LLMClient()
        evaluator = AgenticEvaluator(llm, workdir=d, eval_cap="-1")
        verdict = evaluator.evaluate(task)
        assert verdict.verdict in ("COMPLETE", "INCOMPLETE")
        assert "Cap exceeded" not in verdict.summary
        assert len(verdict.items) >= 1

    def test_tool_calls_discounted(self, tmp_path):
        """Verify tool_call_weight works — with tiny cap, evaluator gets
        several tool calls before hitting the limit (vs 2 full LLM calls)."""
        d, task = self._make_repo(tmp_path)
        task["criteria"] = [
            "README.md exists", "The file .gitreins/config.yaml exists",
            "Repo has at least one git commit", "README.md contains text",
            "The repo directory exists",
        ]
        llm = LLMClient()
        cap = EvalCap(max_iterations=3, tool_call_weight=0.1)
        evaluator = AgenticEvaluator(llm, workdir=d, eval_cap=cap)
        verdict = evaluator.evaluate(task)
        # With 3.0 cap and 0.1 per tool call, the evaluator should
        # make 3 LLM calls + several tool calls before stopping
        assert evaluator.eval_cap.iteration_credit > 3.0, \
            f"Expected > 3.0 (tool calls add fractional cost), got {evaluator.eval_cap.iteration_credit}"
        # Should have stopped because of cap
        assert "Cap exceeded" in verdict.summary or verdict.verdict == "COMPLETE"


# ═══════════════════════════════════════════════════════════════
# Pipeline caps regression tests — prevent hardcoded defaults
# ═══════════════════════════════════════════════════════════════

class TestPipelineCapRegression:
    """Verify Pipeline tier2 and _run_ai_eval respect evaluator caps.

    Regression: Pipeline defaulted to max_iterations=20, overriding
    the evaluator's own config. Fixed in v0.3.1 — these tests
    ensure it never regresses.
    """

    def test_default_pipeline_tier2_has_negative_max_iterations(self):
        """The DEFAULT pipeline config (used when no .gitreins/config.yaml
        exists) must have max_iterations=-1 so evaluator config takes over."""
        from engine.pipeline import load_pipeline_config
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            config = load_pipeline_config(d)
            tier2 = [s for s in config["pipeline"]["stages"] if s["id"] == "tier2"]
            assert len(tier2) == 1, "Default pipeline missing tier2 stage"
            assert tier2[0].get("max_iterations") == -1, \
                f"Default pipeline tier2 max_iterations must be -1, got {tier2[0].get('max_iterations')}"

    def test_run_ai_eval_default_is_negative(self):
        """_run_ai_eval's default for max_iterations must be -1."""
        from engine.pipeline import Pipeline
        p = Pipeline({}, workdir=".")
        # Simulate a minimal step_def with no max_iterations key
        # The _run_ai_eval method defaults to -1
        # (We verify via code inspection — the method reads step_def.get("max_iterations", -1))
        # This test is here to document and assert the contract.
        import inspect
        source = inspect.getsource(p._run_ai_eval)
        assert 'step_def.get("max_iterations", -1)' in source, \
            "_run_ai_eval must default max_iterations to -1, not 20"

    def test_pipeline_passes_max_iterations_negative_to_evaluator(self, tmp_path):
        """Pipeline._run_ai_eval passes max_iterations=-1 to AgenticEvaluator
        when the step doesn't set it, and the evaluator reads config."""
        import subprocess
        d = str(tmp_path)
        subprocess.run(["git", "init"], cwd=d, capture_output=True)
        subprocess.run(["git", "config", "user.email", "t@t"], cwd=d, capture_output=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=d, capture_output=True)
        gitreins_dir = os.path.join(d, ".gitreins")
        os.makedirs(gitreins_dir, exist_ok=True)
        # Set evaluator caps in config — these should be picked up
        config = {
            "evaluator": {
                "max_iterations": 30,
                "max_time": "10m",
                "tool_call_weight": 0.05,
            },
            "guards": {"secrets": False, "lint": False, "tests": False, "dead_code": False, "skylos": False},
        }
        with open(os.path.join(gitreins_dir, "config.yaml"), "w") as f:
            yaml.dump(config, f)
        with open(os.path.join(d, "README.md"), "w") as f:
            f.write("# test\n")
        subprocess.run(["git", "add", "README.md", ".gitreins/"], cwd=d, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=d, capture_output=True)

        # Simulate what Pipeline._run_ai_eval does: create evaluator with max_iterations=-1
        llm = LLMClient()
        evaluator = AgenticEvaluator(llm, workdir=d, max_iterations=-1)
        # The evaluator should have read config caps, not created an unlimited cap
        assert evaluator.eval_cap.max_iterations == 30, \
            f"Evaluator should read config max_iterations=30, got {evaluator.eval_cap.max_iterations}"
        assert evaluator.eval_cap.max_seconds == 600
        assert evaluator.eval_cap.tool_call_weight == 0.05

    def test_pipeline_explicit_max_iterations_is_honored(self, tmp_path):
        """When pipeline step explicitly sets max_iterations, it IS used
        (not overridden by config)."""
        import subprocess
        d = str(tmp_path)
        subprocess.run(["git", "init"], cwd=d, capture_output=True)
        subprocess.run(["git", "config", "user.email", "t@t"], cwd=d, capture_output=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=d, capture_output=True)
        gitreins_dir = os.path.join(d, ".gitreins")
        os.makedirs(gitreins_dir, exist_ok=True)
        config = {
            "evaluator": {"max_iterations": 999},  # should NOT be used
        }
        with open(os.path.join(gitreins_dir, "config.yaml"), "w") as f:
            yaml.dump(config, f)

        llm = LLMClient()
        # Pipeline step sets max_iterations=5 explicitly — evaluator should use it
        evaluator = AgenticEvaluator(llm, workdir=d, max_iterations=5)
        assert evaluator.eval_cap.max_iterations == 5, \
            f"Explicit max_iterations=5 should win, got {evaluator.eval_cap.max_iterations}"
