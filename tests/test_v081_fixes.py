"""
Regression tests for v0.8.1 bug fixes.

- Bug #1: max_output_tokens default lowered from 1M → 128K + provider clamping
- Bug #2: Language-aware default pipeline (not hardcoded Python tools)
- Bug #3: pass_on_error config key gates Tier 2
- Bug #4: API key fallback chain includes KIMI/GROQ/OPENROUTER
"""
import os
import tempfile

import pytest

from engine.config import GitReinsDefaults
from engine.llm import LLMClient
from engine.pipeline import _default_tier1_steps, load_pipeline_config


# ═══════════════════════════════════════════════════════════════
# Bug #1: max_output_tokens default + provider clamping
# ═══════════════════════════════════════════════════════════════

class TestMaxOutputTokensDefault:
    """Bug #1: Default max_output_tokens should be a safe floor."""

    def test_default_is_safe_floor(self):
        """Default max_output_tokens is 131072 (not 1M)."""
        defaults = GitReinsDefaults()
        assert defaults.max_output_tokens == 131_072, (
            f"Expected 131072, got {defaults.max_output_tokens}"
        )

    def test_default_produces_valid_deepseek_value(self):
        """Default (131072) is within DeepSeek's valid range [1, 393216]."""
        defaults = GitReinsDefaults()
        assert 1 <= defaults.max_output_tokens <= 393_216

    def test_clamp_for_deepseek(self):
        """Provider clamping: values > DeepSeek cap clamped to 393216."""
        assert LLMClient._clamp_max_tokens(1_000_000, provider_hint="deepseek") == 393_216
        assert LLMClient._clamp_max_tokens(500_000, provider_hint="deepseek") == 393_216
        assert LLMClient._clamp_max_tokens(200_000, provider_hint="deepseek") == 200_000

    def test_clamp_no_provider_hint_passes_through(self):
        """Unknown provider: no clamping."""
        assert LLMClient._clamp_max_tokens(500_000) == 500_000

    def test_clamp_unlimited_unchanged(self):
        """max_tokens <= 0 (unlimited) returns unchanged."""
        assert LLMClient._clamp_max_tokens(0, provider_hint="deepseek") == 0
        assert LLMClient._clamp_max_tokens(-1, provider_hint="deepseek") == -1

    def test_clamp_for_openai_high_ceiling(self):
        """OpenAI has a high ceiling — reasonable values pass through."""
        assert LLMClient._clamp_max_tokens(500_000, provider_hint="openai") == 500_000

    def test_provider_caps_dict_loaded(self):
        """_PROVIDER_MAX_OUTPUT_TOKENS has expected providers."""
        caps = LLMClient._PROVIDER_MAX_OUTPUT_TOKENS
        assert "deepseek" in caps
        assert caps["deepseek"] == 393_216


# ═══════════════════════════════════════════════════════════════
# Bug #2: Language-aware default pipeline
# ═══════════════════════════════════════════════════════════════

class TestDefaultPipelineLanguageDetection:
    """Bug #2: Default pipeline should detect project language."""

    def _make_temp_workdir(self, file: str | None = None) -> str:
        """Create a temp dir containing a single ecosystem file."""
        d = tempfile.mkdtemp(prefix="gitreins-test-")
        if file:
            with open(os.path.join(d, file), "w") as f:
                f.write("placeholder\n")
        return d

    def test_python_project_defaults(self):
        """Python project gets ruff + pytest."""
        d = self._make_temp_workdir("pyproject.toml")
        try:
            steps = _default_tier1_steps(d)
            assert any(s["id"] == "lint" for s in steps), "Missing lint step"
            assert any(s["id"] == "tests" for s in steps), "Missing tests step"
            lint_cmd = next(s["run"] for s in steps if s["id"] == "lint")
            test_cmd = next(s["run"] for s in steps if s["id"] == "tests")
            assert "ruff" in lint_cmd
            assert "pytest" in test_cmd
        finally:
            import shutil
            shutil.rmtree(d, ignore_errors=True)

    def test_go_project_defaults(self):
        """Go project gets go vet + go test."""
        d = self._make_temp_workdir("go.mod")
        try:
            steps = _default_tier1_steps(d)
            lint_cmd = next(s["run"] for s in steps if s["id"] == "lint")
            test_cmd = next(s["run"] for s in steps if s["id"] == "tests")
            assert "go vet" in lint_cmd
            assert "go test" in test_cmd
        finally:
            import shutil
            shutil.rmtree(d, ignore_errors=True)

    def test_rust_project_defaults(self):
        """Rust project gets cargo clippy + cargo test."""
        d = self._make_temp_workdir("Cargo.toml")
        try:
            steps = _default_tier1_steps(d)
            lint_cmd = next(s["run"] for s in steps if s["id"] == "lint")
            test_cmd = next(s["run"] for s in steps if s["id"] == "tests")
            assert "cargo clippy" in lint_cmd
            assert "cargo test" in test_cmd
        finally:
            import shutil
            shutil.rmtree(d, ignore_errors=True)

    def test_js_project_defaults(self):
        """JS project gets eslint + npm test."""
        d = self._make_temp_workdir("package.json")
        try:
            steps = _default_tier1_steps(d)
            lint_cmd = next(s["run"] for s in steps if s["id"] == "lint")
            test_cmd = next(s["run"] for s in steps if s["id"] == "tests")
            assert "eslint" in lint_cmd
            assert "npm test" in test_cmd
        finally:
            import shutil
            shutil.rmtree(d, ignore_errors=True)

    def test_unknown_project_secrets_only(self):
        """Unknown project (no ecosystem files) gets only secrets step."""
        d = self._make_temp_workdir(None)
        try:
            steps = _default_tier1_steps(d)
            assert len(steps) == 1, f"Expected 1 step (secrets), got {len(steps)}"
            assert steps[0]["id"] == "secrets"
        finally:
            import shutil
            shutil.rmtree(d, ignore_errors=True)

    def test_secrets_step_always_present(self):
        """All language pipelines include a secrets step."""
        for lang_file in ("pyproject.toml", "go.mod", "Cargo.toml", "package.json"):
            d = self._make_temp_workdir(lang_file)
            try:
                steps = _default_tier1_steps(d)
                assert any(s["id"] == "secrets" for s in steps), (
                    f"Missing secrets step for {lang_file}"
                )
            finally:
                import shutil
                shutil.rmtree(d, ignore_errors=True)

    def test_primary_language_is_first_match(self):
        """First matching signature file wins (go.mod before pyproject.toml)."""
        d = tempfile.mkdtemp(prefix="gitreins-test-")
        try:
            # Create both go.mod and pyproject.toml — go wins
            with open(os.path.join(d, "go.mod"), "w") as f:
                f.write("module test\n")
            with open(os.path.join(d, "pyproject.toml"), "w") as f:
                f.write("[project]\n")
            steps = _default_tier1_steps(d)
            lint_cmd = next(s["run"] for s in steps if s["id"] == "lint")
            assert "go vet" in lint_cmd, "go.mod should take priority over pyproject.toml"
        finally:
            import shutil
            shutil.rmtree(d, ignore_errors=True)

    def test_load_pipeline_config_returns_language_aware_default(self):
        """load_pipeline_config returns lang-appropriate default when no config exists."""
        d = self._make_temp_workdir("pyproject.toml")
        try:
            config = load_pipeline_config(d)
            steps = config["pipeline"]["stages"][0]["steps"]
            assert isinstance(steps, list)
            assert any("ruff" in s.get("run", "") for s in steps if s["id"] == "lint")
        finally:
            import shutil
            shutil.rmtree(d, ignore_errors=True)


# ═══════════════════════════════════════════════════════════════
# Bug #3: pass_on_error config key
# ═══════════════════════════════════════════════════════════════

class TestPassOnError:
    """Bug #3: pass_on_error gates Tier 2 when LLM is unavailable."""

    def test_default_is_false(self):
        """pass_on_error defaults to False in GitReinsDefaults."""
        defaults = GitReinsDefaults()
        assert defaults.pass_on_error is False

    def test_overlay_picks_up_pass_on_error(self):
        """Config overlay reads pass_on_error from YAML."""
        defaults = GitReinsDefaults()
        overlaid = defaults.overlay({
            "defaults": {"pass_on_error": True}
        })
        assert overlaid.pass_on_error is True

    def test_overlay_false_when_not_set(self):
        """When pass_on_error is not in config, stays False."""
        defaults = GitReinsDefaults(pass_on_error=False)
        overlaid = defaults.overlay({"defaults": {}})
        assert overlaid.pass_on_error is False

    def test_to_config_dict_includes_pass_on_error(self):
        """to_config_dict serializes pass_on_error."""
        defaults = GitReinsDefaults(pass_on_error=True)
        d = defaults.to_config_dict()
        assert d["pass_on_error"] is True

    def test_to_config_dict_default_false(self):
        """Default pass_on_error=False serialized correctly."""
        defaults = GitReinsDefaults()
        d = defaults.to_config_dict()
        assert d["pass_on_error"] is False

    def test_judge_reads_pass_on_error_from_config(self):
        """Judge._read_pass_on_error reads from .gitreins/config.yaml."""
        d = tempfile.mkdtemp(prefix="gitreins-test-")
        try:
            import yaml
            os.makedirs(os.path.join(d, ".gitreins"))
            with open(os.path.join(d, ".gitreins", "config.yaml"), "w") as f:
                yaml.dump({"defaults": {"pass_on_error": True}}, f)

            from engine.judge import Judge
            from engine.llm import LLMClient
            llm = LLMClient(api_key="sk-test", model="test/model")
            judge = Judge(llm=llm, workdir=d)
            assert judge._read_pass_on_error() is True
        finally:
            import shutil
            shutil.rmtree(d, ignore_errors=True)

    def test_judge_pass_on_error_default_false_no_config(self):
        """Judge._read_pass_on_error returns False when no config exists."""
        d = tempfile.mkdtemp(prefix="gitreins-test-")
        try:
            from engine.judge import Judge
            from engine.llm import LLMClient
            llm = LLMClient(api_key="sk-test", model="test/model")
            judge = Judge(llm=llm, workdir=d)
            assert judge._read_pass_on_error() is False
        finally:
            import shutil
            shutil.rmtree(d, ignore_errors=True)


# ═══════════════════════════════════════════════════════════════
# Bug #4: API key fallback chain
# ═══════════════════════════════════════════════════════════════

class TestApiKeyFallbackChain:
    """Bug #4: API key fallback chain includes KIMI/GROQ/OPENROUTER."""

    def test_kimi_key_in_fallback(self):
        """KIMI_API_KEY is picked up when GITREINS_LLM_API_KEY unset."""
        import os
        os.environ.pop("GITREINS_LLM_API_KEY", None)
        os.environ.pop("NEURALWATT_API_KEY", None)
        os.environ.pop("OPENAI_API_KEY", None)
        os.environ.pop("ANTHROPIC_API_KEY", None)
        os.environ.pop("DEEPSEEK_API_KEY", None)
        os.environ["KIMI_API_KEY"] = "sk-kimi-test"
        try:
            client = LLMClient(model="test/model")
            assert client.api_key == "sk-kimi-test"
        finally:
            os.environ.pop("KIMI_API_KEY", None)

    def test_groq_key_in_fallback(self):
        """GROQ_API_KEY is picked up."""
        import os
        for k in ("GITREINS_LLM_API_KEY", "NEURALWATT_API_KEY", "OPENAI_API_KEY",
                  "ANTHROPIC_API_KEY", "DEEPSEEK_API_KEY", "KIMI_API_KEY"):
            os.environ.pop(k, None)
        os.environ["GROQ_API_KEY"] = "sk-groq-test"
        try:
            client = LLMClient(model="test/model")
            assert client.api_key == "sk-groq-test"
        finally:
            os.environ.pop("GROQ_API_KEY", None)

    def test_openrouter_key_in_fallback(self):
        """OPENROUTER_API_KEY is picked up."""
        import os
        for k in ("GITREINS_LLM_API_KEY", "NEURALWATT_API_KEY", "OPENAI_API_KEY",
                  "ANTHROPIC_API_KEY", "DEEPSEEK_API_KEY", "KIMI_API_KEY",
                  "GROQ_API_KEY"):
            os.environ.pop(k, None)
        os.environ["OPENROUTER_API_KEY"] = "sk-or-test"
        try:
            client = LLMClient(model="test/model")
            assert client.api_key == "sk-or-test"
        finally:
            os.environ.pop("OPENROUTER_API_KEY", None)

    def test_existing_keys_take_priority(self):
        """GITREINS_LLM_API_KEY takes priority over fallback keys."""
        import os
        os.environ["GITREINS_LLM_API_KEY"] = "sk-primary"
        os.environ["KIMI_API_KEY"] = "sk-kimi-backup"
        try:
            client = LLMClient(model="test/model")
            assert client.api_key == "sk-primary"
        finally:
            os.environ.pop("GITREINS_LLM_API_KEY", None)
            os.environ.pop("KIMI_API_KEY", None)
