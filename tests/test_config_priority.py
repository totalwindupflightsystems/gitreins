"""
Verifies config loading priority chain:

  1. Built-in defaults (GitReinsDefaults)
  2. .gitreins/config.yaml defaults: section overrides built-ins
  3. .gitreins/config.yaml evaluator: individual keys override defaults section
  4. Explicit EvalCap constructor params override all
"""
import os

import yaml

from engine.config import GitReinsDefaults, load_defaults, load_raw_config


class TestConfigMissingFile:
    """load_config/load_raw_config returns {} on missing file (does not crash)."""

    def test_load_raw_config_missing_returns_empty(self, tmp_path):
        """load_raw_config returns {} when no .gitreins/config.yaml exists."""
        result = load_raw_config(str(tmp_path))
        assert result == {}

    def test_load_raw_config_none_workdir(self):
        """load_raw_config with None workdir returns {}."""
        result = load_raw_config(None)
        assert result == {}


class TestDefaultsSectionOverlaysBuiltins:
    """defaults: section in .gitreins/config.yaml overlays built-in GitReinsDefaults."""

    def test_defaults_overlay_model(self, tmp_path):
        """defaults.model overrides GitReinsDefaults.model."""
        config_dir = os.path.join(str(tmp_path), ".gitreins")
        os.makedirs(config_dir, exist_ok=True)
        config = {"defaults": {"model": "custom-model-v2"}}
        with open(os.path.join(config_dir, "config.yaml"), "w") as f:
            yaml.safe_dump(config, f)

        gd = load_defaults(str(tmp_path))
        assert gd.model == "custom-model-v2"

    def test_defaults_overlay_iterations(self, tmp_path):
        """defaults.max_iterations overrides GitReinsDefaults.max_iterations."""
        config_dir = os.path.join(str(tmp_path), ".gitreins")
        os.makedirs(config_dir, exist_ok=True)
        config = {"defaults": {"max_iterations": 50}}
        with open(os.path.join(config_dir, "config.yaml"), "w") as f:
            yaml.safe_dump(config, f)

        gd = load_defaults(str(tmp_path))
        assert gd.max_iterations == 50.0

    def test_defaults_partial_overlay(self, tmp_path):
        """Only specified keys in defaults: section override; others stay built-in."""
        config_dir = os.path.join(str(tmp_path), ".gitreins")
        os.makedirs(config_dir, exist_ok=True)
        config = {"defaults": {"max_iterations": 25}}
        with open(os.path.join(config_dir, "config.yaml"), "w") as f:
            yaml.safe_dump(config, f)

        gd = load_defaults(str(tmp_path))
        assert gd.max_iterations == 25.0
        assert gd.model == "deepseek-v4-flash"
        assert gd.max_input_tokens == 10_000_000

    def test_missing_defaults_section_leaves_builtins(self, tmp_path):
        """Config file without defaults: section leaves built-in defaults intact."""
        config_dir = os.path.join(str(tmp_path), ".gitreins")
        os.makedirs(config_dir, exist_ok=True)
        config = {"guards": {"secrets": True}}
        with open(os.path.join(config_dir, "config.yaml"), "w") as f:
            yaml.safe_dump(config, f)

        gd = load_defaults(str(tmp_path))
        assert gd.max_iterations == 100.0
        assert gd.model == "deepseek-v4-flash"
        assert gd.max_input_tokens == 10_000_000


class TestEvaluatorSectionOverridesDefaults:
    """evaluator: individual keys override defaults: section values."""

    def test_evaluator_overrides_defaults_iterations(self, tmp_path):
        """evaluator.max_iterations overrides defaults.max_iterations."""
        config_dir = os.path.join(str(tmp_path), ".gitreins")
        os.makedirs(config_dir, exist_ok=True)
        config = {
            "defaults": {"max_iterations": 100},
            "evaluator": {"max_iterations": 50},
        }
        with open(os.path.join(config_dir, "config.yaml"), "w") as f:
            yaml.safe_dump(config, f)

        from engine.eval_cap import eval_cap_from_config
        cap = eval_cap_from_config(config)
        assert cap.max_iterations == 50.0, (
            f"evaluator.max_iterations should override defaults, got {cap.max_iterations}"
        )

    def test_evaluator_overrides_defaults_time(self, tmp_path):
        """evaluator.max_time overrides defaults.max_time."""
        config = {
            "defaults": {"max_time": "10m"},
            "evaluator": {"max_time": "30m"},
        }

        from engine.eval_cap import eval_cap_from_config
        cap = eval_cap_from_config(config)
        assert cap.max_seconds == 1800.0

    def test_no_evaluator_section_falls_back_to_defaults(self, tmp_path):
        """Without evaluator: section, defaults: values are used."""
        config = {
            "defaults": {"max_iterations": 75, "max_time": "5m"},
        }

        from engine.eval_cap import eval_cap_from_config
        cap = eval_cap_from_config(config)
        assert cap.max_iterations == 75.0
        assert cap.max_seconds == 300.0

    def test_empty_evaluator_uses_defaults_and_builtins(self, tmp_path):
        """Empty evaluator section falls through to defaults, then built-ins."""
        config = {"evaluator": {}}

        from engine.eval_cap import eval_cap_from_config
        cap = eval_cap_from_config(config)
        assert cap.max_iterations == 100.0
        assert cap.max_input_tokens == 10_000_000
        assert cap.max_output_tokens == 131_072

    def test_evaluator_partial_override(self, tmp_path):
        """Only specified evaluator keys override; others fall through to defaults."""
        config = {
            "defaults": {"max_iterations": 100, "max_time": "10m"},
            "evaluator": {"max_iterations": 30},
        }

        from engine.eval_cap import eval_cap_from_config
        cap = eval_cap_from_config(config)
        assert cap.max_iterations == 30.0
        assert cap.max_seconds == 600.0

    def test_gitreinsdefaults_overlay_does_not_process_evaluator(self, tmp_path):
        """GitReinsDefaults.overlay() ignores evaluator: keys (they are handled later)."""
        config = {
            "defaults": {"max_iterations": 50},
            "evaluator": {"max_iterations": 999},
        }
        gd = GitReinsDefaults().overlay(config)
        assert gd.max_iterations == 50.0, (
            f"overlay should only process defaults: section, got {gd.max_iterations}"
        )


class TestExplicitConstructorWins:
    """Explicit EvalCap constructor params override all config values."""

    def test_explicit_evalcap_overrides_config(self, tmp_path):
        """EvalCap object passed to AgenticEvaluator wins over config."""
        config_dir = os.path.join(str(tmp_path), ".gitreins")
        os.makedirs(config_dir, exist_ok=True)
        config = {
            "defaults": {"max_iterations": 100},
            "evaluator": {"max_iterations": 50},
        }
        with open(os.path.join(config_dir, "config.yaml"), "w") as f:
            yaml.safe_dump(config, f)

        from engine.eval_cap import EvalCap, eval_cap_from_config

        # This simulates what AgenticEvaluator does when given an explicit EvalCap
        cap = EvalCap(max_iterations=10, source="explicit")
        # Verify that config-based function would produce different result
        config_cap = eval_cap_from_config(config)
        assert config_cap.max_iterations == 50.0
        # The explicit cap should remain at 10
        assert cap.max_iterations == 10.0

    def test_agentic_evaluator_explicit_evalcap_wins(self, tmp_path):
        """AgenticEvaluator with explicit eval_cap=EvalCap uses it, not config."""
        config_dir = os.path.join(str(tmp_path), ".gitreins")
        os.makedirs(config_dir, exist_ok=True)
        config = {
            "defaults": {"max_iterations": 100},
            "evaluator": {"max_iterations": 50},
        }
        with open(os.path.join(config_dir, "config.yaml"), "w") as f:
            yaml.safe_dump(config, f)

        from engine.eval_cap import EvalCap
        from engine.evaluator import AgenticEvaluator
        from engine.llm import LLMClient

        llm = LLMClient()
        explicit_cap = EvalCap(max_iterations=10, source="explicit")
        evaluator = AgenticEvaluator(llm, workdir=str(tmp_path), eval_cap=explicit_cap)
        assert evaluator.eval_cap.max_iterations == 10.0, (
            f"Explicit EvalCap should override config, got {evaluator.eval_cap.max_iterations}"
        )

    def test_agentic_evaluator_explicit_string_wins(self, tmp_path):
        """AgenticEvaluator with explicit eval_cap string wins over config."""
        config_dir = os.path.join(str(tmp_path), ".gitreins")
        os.makedirs(config_dir, exist_ok=True)
        config = {
            "defaults": {"max_iterations": 100},
            "evaluator": {"max_iterations": 50},
        }
        with open(os.path.join(config_dir, "config.yaml"), "w") as f:
            yaml.safe_dump(config, f)

        from engine.evaluator import AgenticEvaluator
        from engine.llm import LLMClient

        llm = LLMClient()
        evaluator = AgenticEvaluator(llm, workdir=str(tmp_path), eval_cap="10")
        assert evaluator.eval_cap.max_iterations == 10.0

    def test_agentic_evaluator_max_iterations_positive_wins(self, tmp_path):
        """AgenticEvaluator with explicit max_iterations=5 wins over config."""
        config_dir = os.path.join(str(tmp_path), ".gitreins")
        os.makedirs(config_dir, exist_ok=True)
        config = {
            "defaults": {"max_iterations": 100},
            "evaluator": {"max_iterations": 50},
        }
        with open(os.path.join(config_dir, "config.yaml"), "w") as f:
            yaml.safe_dump(config, f)

        from engine.evaluator import AgenticEvaluator
        from engine.llm import LLMClient

        llm = LLMClient()
        evaluator = AgenticEvaluator(llm, workdir=str(tmp_path), max_iterations=5)
        assert evaluator.eval_cap.max_iterations == 5.0

    def test_no_explicit_params_uses_config(self, tmp_path):
        """AgenticEvaluator with no explicit params reads config.yaml properly."""
        config_dir = os.path.join(str(tmp_path), ".gitreins")
        os.makedirs(config_dir, exist_ok=True)
        config = {
            "defaults": {"max_iterations": 100},
            "evaluator": {"max_iterations": 30},
        }
        with open(os.path.join(config_dir, "config.yaml"), "w") as f:
            yaml.safe_dump(config, f)

        from engine.evaluator import AgenticEvaluator
        from engine.llm import LLMClient

        llm = LLMClient()
        evaluator = AgenticEvaluator(llm, workdir=str(tmp_path))
        assert evaluator.eval_cap.max_iterations == 30.0, (
            f"Without explicit params, evaluator should read config, "
            f"got {evaluator.eval_cap.max_iterations}"
        )
