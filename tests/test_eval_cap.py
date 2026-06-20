"""
Tests for EvalCap — parser, limit checking, and AgenticEvaluator integration.
"""
import time

from engine.eval_cap import parse_eval_cap, eval_cap_from_config, EvalCap
from engine.evaluator import AgenticEvaluator
from engine.llm import LLMClient


class TestEvalCapParser:
    """Unit tests for parse_eval_cap()."""

    def test_numeric_iteration(self):
        cap = parse_eval_cap("100")
        assert cap.max_iterations == 100
        assert cap.max_seconds == -1
        assert cap.max_input_tokens == -1
        assert cap.max_output_tokens == -1

    def test_unlimited(self):
        for val in ("-1", "0", "unlimited", "none"):
            cap = parse_eval_cap(val)
            assert cap.max_iterations == -1, f"failed for {val}"
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
        # 1M should NOT be parsed as 1 minute (61 seconds)
        assert cap.max_seconds == -1

    def test_slash_token_pair(self):
        cap = parse_eval_cap("100k/50k")
        assert cap.max_input_tokens == 100_000
        assert cap.max_output_tokens == 50_000

    def test_slash_token_pair_reverse(self):
        cap = parse_eval_cap("200k/100k")
        assert cap.max_input_tokens == 200_000
        assert cap.max_output_tokens == 100_000

    def test_iter_and_time(self):
        cap = parse_eval_cap("100/30m")
        assert cap.max_iterations == 100
        assert cap.max_seconds == 1800

    def test_iter_time_input_output(self):
        cap = parse_eval_cap("100/5m/200k/50k")
        assert cap.max_iterations == 100
        assert cap.max_seconds == 300
        assert cap.max_input_tokens == 200_000
        assert cap.max_output_tokens == 50_000

    def test_unlimited_iter_with_time(self):
        cap = parse_eval_cap("-1/30m")
        assert cap.max_iterations == -1
        assert cap.max_seconds == 1800

    def test_time_with_output_tokens(self):
        cap = parse_eval_cap("30m/200k")
        assert cap.max_seconds == 1800
        assert cap.max_output_tokens == 200_000
        assert cap.max_input_tokens == -1

    def test_time_with_both_tokens(self):
        cap = parse_eval_cap("30m/200k/50k")
        assert cap.max_seconds == 1800
        assert cap.max_input_tokens == 200_000
        assert cap.max_output_tokens == 50_000

    def test_none_with_tokens(self):
        cap = parse_eval_cap("none/200k/50k")
        assert cap.max_iterations == -1
        assert cap.max_input_tokens == 200_000
        assert cap.max_output_tokens == 50_000

    def test_decimal_tokens(self):
        """Decimal notation: 0.1M = 100k, 1.5M = 1.5 million."""
        assert parse_eval_cap("0.1M").max_output_tokens == 100_000
        assert parse_eval_cap("0.5M").max_output_tokens == 500_000
        assert parse_eval_cap("1.5M").max_output_tokens == 1_500_000
        assert parse_eval_cap("0.1k").max_output_tokens == 100
        assert parse_eval_cap("1.5k").max_output_tokens == 1_500

    def test_decimal_slash_tokens(self):
        cap = parse_eval_cap("0.1M/0.05M")
        assert cap.max_input_tokens == 100_000
        assert cap.max_output_tokens == 50_000
        cap = parse_eval_cap("0.2M/0.1M")
        assert cap.max_input_tokens == 200_000
        assert cap.max_output_tokens == 100_000

    def test_raw_numeric_is_iterations(self):
        """Raw numbers without suffix are iteration caps, not tokens."""
        cap = parse_eval_cap("50000")
        assert cap.max_iterations == 50000
        assert cap.max_output_tokens == -1


class TestEvalCapChecking:
    """Unit tests for EvalCap.check() and record_iteration()."""

    def test_iteration_cap_not_exceeded(self):
        cap = EvalCap(max_iterations=10)
        cap.start()
        for _ in range(5):
            err = cap.record_iteration()
            assert err is None

    def test_iteration_cap_exceeded(self):
        cap = EvalCap(max_iterations=3)
        cap.start()
        for _ in range(2):
            assert cap.record_iteration() is None
        err = cap.record_iteration()
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
        err = cap.record_iteration(prompt_tokens=600, completion_tokens=0)
        assert err is None
        err = cap.record_iteration(prompt_tokens=500, completion_tokens=0)
        assert err is not None
        assert "Input token budget" in err

    def test_output_tokens_exceeded(self):
        cap = EvalCap(max_output_tokens=500)
        cap.start()
        err = cap.record_iteration(prompt_tokens=0, completion_tokens=300)
        assert err is None
        err = cap.record_iteration(prompt_tokens=0, completion_tokens=300)
        assert err is not None
        assert "Output token budget" in err

    def test_unlimited_never_exceeds(self):
        cap = EvalCap()  # all -1
        cap.start()
        for _ in range(10000):
            assert cap.record_iteration(prompt_tokens=1000, completion_tokens=1000) is None

    def test_summary_format(self):
        cap = EvalCap(max_iterations=100, max_seconds=300, max_input_tokens=200000, max_output_tokens=50000)
        cap.start()
        cap.record_iteration(prompt_tokens=50000, completion_tokens=10000)
        s = cap.summary()
        assert "iterations:" in s
        assert "time:" in s
        assert "input tokens:" in s
        assert "output tokens:" in s
        assert "50k" in s  # 50000 formatted


class TestEvalCapFromConfig:
    """Tests for eval_cap_from_config()."""

    def test_reads_evaluator_cap(self):
        config = {"evaluator": {"cap": "50/30m"}}
        cap = eval_cap_from_config(config)
        assert cap.max_iterations == 50
        assert cap.max_seconds == 1800

    def test_reads_guards_eval_cap_fallback(self):
        config = {"guards": {"eval_cap": "200k/100k"}}
        cap = eval_cap_from_config(config)
        assert cap.max_input_tokens == 200_000
        assert cap.max_output_tokens == 100_000

    def test_prioritizes_evaluator_cap_over_guards(self):
        config = {
            "evaluator": {"cap": "50"},
            "guards": {"eval_cap": "200k/100k"},
        }
        cap = eval_cap_from_config(config)
        assert cap.max_iterations == 50
        # Should NOT pick up guards.eval_cap when evaluator.cap is set
        assert cap.max_input_tokens == -1

    def test_empty_config_uses_default(self):
        cap = eval_cap_from_config({})
        assert cap.max_iterations == 100


class TestAgenticEvaluatorCapIntegration:
    """Tests that AgenticEvaluator properly uses eval_cap."""

    def test_evaluator_accepts_eval_cap_string(self):
        """AgenticEvaluator can be constructed with eval_cap string."""
        llm = LLMClient()
        evaluator = AgenticEvaluator(llm, eval_cap="50")
        assert evaluator.eval_cap.max_iterations == 50

    def test_evaluator_accepts_eval_cap_object(self):
        llm = LLMClient()
        cap = parse_eval_cap("200k/100k")
        evaluator = AgenticEvaluator(llm, eval_cap=cap)
        assert evaluator.eval_cap.max_input_tokens == 200_000
        assert evaluator.eval_cap.max_output_tokens == 100_000

    def test_evaluator_still_accepts_max_iterations(self):
        """Backward compat: max_iterations kwarg still works."""
        llm = LLMClient()
        evaluator = AgenticEvaluator(llm, max_iterations=42)
        assert evaluator.max_iterations == 42
        assert evaluator.eval_cap.max_iterations == 42

    def test_eval_cap_wins_over_max_iterations(self):
        llm = LLMClient()
        evaluator = AgenticEvaluator(llm, max_iterations=42, eval_cap="10")
        assert evaluator.eval_cap.max_iterations == 10

    def test_evaluator_reads_config_yaml(self, tmp_path):
        """When no cap specified, evaluator reads evaluator.cap from .gitreins/config.yaml."""
        import os, yaml
        gitreins_dir = tmp_path / ".gitreins"
        gitreins_dir.mkdir()
        config = {"evaluator": {"cap": "25/10m"}}
        with open(gitreins_dir / "config.yaml", "w") as f:
            yaml.dump(config, f)

        llm = LLMClient()
        evaluator = AgenticEvaluator(llm, workdir=str(tmp_path))
        assert evaluator.eval_cap.max_iterations == 25
        assert evaluator.eval_cap.max_seconds == 600
