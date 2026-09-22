"""JEVRES-006 — config knobs for the Jev resolution gate.

Covers the plumbing only; the measured numbers stay in
``docs/jev-resolution-gate.md`` (the single design authority):

* ``engine.config`` parses the ``resolution:`` block (per-surface enables,
  model pin, token ceiling, band thresholds, egress exclusions) and tolerates
  wrong-typed config from other users without crashing.
* The per-surface enable gate (``engine.resolution.surface_enabled``) defaults
  every surface to OFF and answers only an explicit ``true``.
* The egress exclusion filter (``is_excluded_path_for_surface``) keeps the
  built-in secret floor AND drops configured patterns — proven on a synthetic
  tree whose ``.env`` and internal key material must never reach the bundle.
* ``gitreins init`` writes the disabled-by-default block and preserves a
  user-authored one.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest
import yaml

from engine import resolution
from engine.config import GitReinsDefaults, load_defaults

REPO_ROOT = Path(__file__).resolve().parent.parent


def _config_with(block: dict) -> dict:
    """A raw config.yaml dict carrying just a top-level ``resolution:`` block."""
    return {"resolution": block}


# ── Config parsing: engine.config ────────────────────────────


class TestResolutionConfigParsing:
    def test_builtin_defaults_disable_every_surface(self):
        d = GitReinsDefaults()
        assert d.resolution_enabled_cli is False
        assert d.resolution_enabled_mcp is False
        assert d.resolution_enabled_predispatch is False
        assert d.resolution_enabled_judge_prescreen is False
        assert d.resolution_model == resolution.JEV_MODEL
        assert d.resolution_tokens_max == resolution.MAX_BUNDLE_TOKENS
        assert d.resolution_resolved_at == pytest.approx(resolution.RESOLVED_AT)
        assert d.resolution_review_at == pytest.approx(resolution.REVIEW_AT)
        assert d.resolution_egress_exclude == ()

    def test_block_overlays_enables_model_ceiling_bands_and_excludes(self):
        d = GitReinsDefaults().overlay(
            _config_with(
                {
                    "enabled": {
                        "cli": True,
                        "mcp": False,
                        "predispatch": True,
                        "judge_prescreen": False,
                    },
                    "model": "typesafe/jev-1.13-testpin",
                    "tokens_max": "8k",
                    "bands": {"resolved_at": 0.9, "review_at": 0.4},
                    "egress_exclude": ["internal/keys.py", "vendor/*"],
                }
            )
        )
        assert d.resolution_enabled_cli is True
        assert d.resolution_enabled_mcp is False
        assert d.resolution_enabled_predispatch is True
        assert d.resolution_enabled_judge_prescreen is False
        assert d.resolution_model == "typesafe/jev-1.13-testpin"
        assert d.resolution_tokens_max == 8000
        assert d.resolution_resolved_at == pytest.approx(0.9)
        assert d.resolution_review_at == pytest.approx(0.4)
        assert d.resolution_egress_exclude == ("internal/keys.py", "vendor/*")

    def test_wrong_typed_block_degrades_to_defaults_not_a_crash(self):
        """`resolution: true` (a scalar) must parse as 'all off', not raise."""
        d = GitReinsDefaults().overlay({"defaults": {"resolution": True}})
        assert d.resolution_enabled_cli is False
        assert d.resolution_model == GitReinsDefaults().resolution_model
        assert d.resolution_egress_exclude == ()

    def test_wrong_typed_subkeys_degrade_to_defaults(self):
        d = GitReinsDefaults().overlay(
            _config_with(
                {
                    "enabled": "yes-please",
                    "bands": "loose",
                    "egress_exclude": "internal",
                    "tokens_max": "not-a-number",
                }
            )
        )
        assert d.resolution_enabled_cli is False
        assert d.resolution_enabled_judge_prescreen is False
        assert d.resolution_resolved_at == pytest.approx(GitReinsDefaults().resolution_resolved_at)
        assert d.resolution_review_at == pytest.approx(GitReinsDefaults().resolution_review_at)
        assert d.resolution_egress_exclude == ()

    def test_load_defaults_reads_a_real_config_file(self, tmp_path):
        config_dir = tmp_path / ".gitreins"
        config_dir.mkdir()
        with open(config_dir / "config.yaml", "w") as f:
            yaml.safe_dump({"resolution": {"enabled": {"mcp": True}}}, f)
        d = load_defaults(str(tmp_path))
        assert d.resolution_enabled_mcp is True
        assert d.resolution_enabled_cli is False
        assert d._source == ".gitreins/config.yaml"

    def test_to_config_dict_round_trips_the_block(self):
        d = GitReinsDefaults()
        d.resolution_enabled_cli = True
        d.resolution_egress_exclude = ("internal/keys.py",)
        out = d.to_config_dict()
        assert out["resolution"]["enabled"]["cli"] is True
        assert out["resolution"]["enabled"]["judge_prescreen"] is False
        assert out["resolution"]["tokens_max"] == d.resolution_tokens_max
        assert out["resolution"]["bands"]["resolved_at"] == d.resolution_resolved_at
        assert out["resolution"]["egress_exclude"] == ["internal/keys.py"]


# ── The per-surface enable gate ──────────────────────────────


class TestSurfaceEnabled:
    def test_every_known_surface_defaults_to_off(self):
        for surface in resolution.RESOLUTION_SURFACES:
            enabled, reason = resolution.surface_enabled(surface, defaults=GitReinsDefaults())
            assert enabled is False
            assert reason == "surface-disabled"

    def test_explicit_true_is_the_only_on_switch(self):
        for surface in resolution.RESOLUTION_SURFACES:
            d = GitReinsDefaults()
            setattr(d, f"resolution_enabled_{surface}", True)
            enabled, reason = resolution.surface_enabled(surface, defaults=d)
            assert enabled is True
            assert reason is None

    def test_truthy_but_not_true_is_off(self):
        """`enabled.cli: 1` (or "true") is NOT an opt-in — YAML typing aside."""
        d = GitReinsDefaults().overlay(_config_with({"enabled": {"cli": "true"}}))
        assert d.resolution_enabled_cli is False

    def test_unknown_surface_is_a_named_error(self):
        with pytest.raises(ValueError, match="unknown resolution surface"):
            resolution.surface_enabled("carrier-pigeon", defaults=GitReinsDefaults())

    def test_none_defaults_means_off(self):
        enabled, reason = resolution.surface_enabled("cli", defaults=None)
        assert enabled is False
        assert reason == "surface-disabled"


# ── The egress exclusion filter ──────────────────────────────


class TestEgressExclusionFilter:
    @pytest.fixture
    def env_tree(self, tmp_path):
        """A synthetic repo whose secrets must never reach the bundle.

        Exercises BOTH halves of the filter: the built-in floor (`.env`,
        a PEM key, an excluded cache dir) and the configured patterns
        (`internal/keys.py`, everything under `vendor/`).
        """
        repo = tmp_path / "repo"
        (repo / "engine").mkdir(parents=True)
        (repo / "internal").mkdir()
        (repo / "vendor" / "lib").mkdir(parents=True)
        (repo / "engine" / "real.py").write_text("def answer():\n    return 42\n")
        (repo / ".env").write_text("SUPER_SECRET=FAKE_KEY_NOT_A_SECRET\n")
        (repo / "server.pem").write_text("FAKE_PEM_HEADER\n")
        (repo / "internal" / "keys.py").write_text("KEY_MATERIAL = 'rot'\n")
        (repo / "vendor" / "lib" / "third_party.py").write_text("# vendor code\n")
        (repo / ".gitreins").mkdir()
        return repo

    def _defaults_with_excludes(self):
        d = GitReinsDefaults()
        d.resolution_egress_exclude = ("internal/keys.py", "vendor/*")
        return d

    def test_builtin_floor_blocks_env_key_and_caches(self, env_tree):
        for path in (
            ".env",
            "server.pem",
            ".git/config",
            "node_modules/x.js",
            "__pycache__/real.cpython-311.pyc",
        ):
            assert resolution.is_excluded_path_for_surface(
                path, defaults=self._defaults_with_excludes()
            ), path

    def test_configured_patterns_block_internal_and_vendor(self, env_tree):
        d = self._defaults_with_excludes()
        assert resolution.is_excluded_path_for_surface("internal/keys.py", defaults=d)
        assert resolution.is_excluded_path_for_surface("vendor/lib/third_party.py", defaults=d)
        # The floor holds without any configured pattern, too.
        assert resolution.is_excluded_path_for_surface(".env", defaults=GitReinsDefaults())

    def test_plain_source_is_never_excluded(self, env_tree):
        assert not resolution.is_excluded_path_for_surface(
            "engine/real.py", defaults=self._defaults_with_excludes()
        )

    def test_pattern_applies_through_the_whole_pipeline(self, env_tree):
        """A hilo bundle ranking the excluded files ships none of them."""
        bundle = "\n".join(
            [
                "## MAP",
                "engine/real.py →",
                "  - answer",
                "",
                "## DETAIL",
                "engine/real.py [provenance=ast_exact, score=1.00]",
                "def answer():",
                "    return 42",
                "",
                "internal/keys.py [provenance=ast_exact, score=0.80]",
                "KEY_MATERIAL = 'rot'",
                "",
                "vendor/lib/third_party.py [provenance=ast_exact, score=0.70]",
                "# vendor code",
                "",
                ".env [provenance=ast_exact, score=0.60]",
                "SUPER_SECRET=FAKE_KEY_NOT_A_SECRET",
            ]
        )
        blocks = resolution.parse_understand(
            output=bundle,
            bundle_rank=0,
            egress_exclude=("internal/keys.py", "vendor/*"),
            workdir=str(env_tree),
        )
        shipped = [block.file for block in blocks]
        assert shipped == ["engine/real.py"]
        joined = "\n".join(block.text for block in blocks)
        assert "KEY_MATERIAL" not in joined
        assert "SUPER_SECRET" not in joined

    def test_trace_drops_excluded_seeds(self, env_tree):
        """`hilo graph search` output naming a .env yields no seed for it."""

        def fake_runner(args, workdir):
            assert args[0] == "graph" and args[1] == "search"
            return (
                0,
                "\n".join(
                    [
                        "0.90  engine/real.py  [lexical]",
                        "  symbols: answer",
                        "0.80  internal/keys.py  [lexical]",
                        "0.10  .env  [lexical]",
                    ]
                ),
                "",
            )

        seeds = resolution.trace_question(
            "where is the key handling?",
            workdir=str(env_tree),
            runner=fake_runner,
            egress_exclude=("internal/keys.py", "vendor/*"),
        )
        assert [seed.file for seed in seeds] == ["engine/real.py"]

    def test_reads_never_open_an_excluded_file(self, env_tree):
        assert (
            resolution._read_block(
                "internal/keys.py",
                str(env_tree),
                bundle_rank=1,
                egress_exclude=("internal/keys.py",),
            )
            is None
        )
        block = resolution._read_block(
            "engine/real.py",
            str(env_tree),
            bundle_rank=1,
            egress_exclude=("internal/keys.py",),
        )
        assert block is not None and "answer" in block.text

    def test_empty_and_wrong_typed_excludes_are_ignored(self):
        assert not resolution.is_excluded_path_for_surface(
            "engine/real.py", egress_exclude=(), defaults=self._defaults_with_excludes()
        )
        d = GitReinsDefaults().overlay(_config_with({"egress_exclude": "internal"}))
        assert d.resolution_egress_exclude == ()


# ── gitreins init writes the resolution defaults ─────────────


class _Cli:
    """Load gitreins/cli.py under a private module name (doc-sync safety)."""

    def __init__(self):
        spec = importlib.util.spec_from_file_location(
            "_gitreins_cli_jevres006", REPO_ROOT / "gitreins" / "cli.py"
        )
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)


def _run_cli_inprocess(monkeypatch, repo: Path, *args: str):
    """Drive gitreins.cli.main() in-process with *repo* as the workdir.

    In-process (not a child interpreter) so the WORKTREE's package is what
    runs — a bare `python -m gitreins` resolves through sys.path and can
    silently execute the main checkout's installed copy instead.
    """
    import contextlib
    import io
    import sys

    cli = _Cli().module
    monkeypatch.delenv("GITREINS_OPENROUTER_KEY", raising=False)
    out, err = io.StringIO(), io.StringIO()
    code = 0
    cwd_backup = os.getcwd()
    argv_backup = sys.argv
    try:
        os.chdir(repo)
        sys.argv = ["gitreins", *args]
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                cli.main()
            except SystemExit as exc:
                code = exc.code or 0
    finally:
        os.chdir(cwd_backup)
        sys.argv = argv_backup
    return code, out.getvalue(), err.getvalue()


def _make_repo(tmp_path: Path) -> Path:
    import subprocess as sp

    repo = tmp_path / "repo"
    repo.mkdir()
    sp.run(["git", "init", "-q"], cwd=repo, check=True, capture_output=True)
    (repo / "main.py").write_text("print('hi')\n")
    return repo


class TestInitWritesResolutionDefaults:
    def test_fresh_init_writes_a_disabled_resolution_block(self, tmp_path, monkeypatch):
        repo = _make_repo(tmp_path)
        code, out, err = _run_cli_inprocess(monkeypatch, repo, "init")
        assert code == 0, err
        with open(repo / ".gitreins" / "config.yaml") as f:
            config = yaml.safe_load(f)
        block = config["resolution"]
        assert block["enabled"] == {
            "cli": False,
            "mcp": False,
            "predispatch": False,
            "judge_prescreen": False,
        }
        assert block["model"] == GitReinsDefaults().resolution_model
        assert block["tokens_max"] == GitReinsDefaults().resolution_tokens_max
        assert block["bands"] == {
            "resolved_at": GitReinsDefaults().resolution_resolved_at,
            "review_at": GitReinsDefaults().resolution_review_at,
        }
        assert block["egress_exclude"] == []

    def test_init_preserves_a_user_authored_resolution_block(self, tmp_path, monkeypatch):
        repo = _make_repo(tmp_path)
        (repo / ".gitreins").mkdir()
        with open(repo / ".gitreins" / "config.yaml", "w") as f:
            yaml.safe_dump(
                {
                    "resolution": {
                        "enabled": {"cli": True},
                        "egress_exclude": ["internal/keys.py"],
                    }
                },
                f,
            )
        code, out, err = _run_cli_inprocess(monkeypatch, repo, "init")
        assert code == 0, err
        with open(repo / ".gitreins" / "config.yaml") as f:
            config = yaml.safe_load(f)
        assert config["resolution"]["enabled"]["cli"] is True
        assert config["resolution"]["egress_exclude"] == ["internal/keys.py"]

    def test_disabled_init_output_abstains_via_the_real_cli(self, tmp_path, monkeypatch):
        """Acceptance 1, end to end: a fresh init's config disables the CLI."""
        import json as _json

        repo = _make_repo(tmp_path)
        code, _, err = _run_cli_inprocess(monkeypatch, repo, "init")
        assert code == 0, err
        code, out, err = _run_cli_inprocess(
            monkeypatch, repo, "resolve", "does anything resolve?", "--json"
        )
        assert code == 1
        verdict = _json.loads(out)
        assert verdict["verdict"] == "ABSTAIN"
        assert verdict["abstain_reason"] == "surface-disabled"
