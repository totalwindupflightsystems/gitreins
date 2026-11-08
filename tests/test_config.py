"""Dedicated tests for unified configuration defaults and coercion helpers."""

import pytest

from engine.config import (
    GitReinsDefaults,
    _coerce_float,
    _coerce_seconds,
    _coerce_tokens,
    _fmt_seconds,
    _fmt_tokens,
    _pypi_url,
    _version_greater,
    load_raw_config,
    QualityConfig,
    load_defaults,
)
from pathlib import Path


# ── GitReinsDefaults — built-in values ───────────────────────


def test_defaults_model() -> None:
    d = GitReinsDefaults()
    assert d.model == "deepseek-v4-flash"


def test_defaults_max_iterations() -> None:
    d = GitReinsDefaults()
    assert d.max_iterations == 100.0


def test_defaults_review_severity() -> None:
    d = GitReinsDefaults()
    assert d.commit_audit_review_severity == "standard"


def test_defaults_scoring_threshold() -> None:
    d = GitReinsDefaults()
    assert d.commit_audit_review_score_threshold == 8.0
    assert d.commit_audit_review_score_offset == 1.0


def test_defaults_source_is_builtin() -> None:
    d = GitReinsDefaults()
    assert "built-in" in d._source


# ── overlay ──────────────────────────────────────────────────


def test_overlay_with_none_returns_self() -> None:
    d = GitReinsDefaults()
    result = d.overlay(None)
    assert result is d


def test_overlay_with_empty_dict_returns_same_defaults() -> None:
    d = GitReinsDefaults()
    result = d.overlay({})
    assert result.model == d.model
    assert result.max_iterations == d.max_iterations


def test_overlay_changes_model() -> None:
    d = GitReinsDefaults()
    result = d.overlay({"defaults": {"model": "kimi-for-coding"}})
    assert result.model == "kimi-for-coding"


def test_overlay_changes_max_iterations() -> None:
    d = GitReinsDefaults()
    result = d.overlay({"defaults": {"max_iterations": 50}})
    assert result.max_iterations == 50.0


def test_overlay_respects_time_string() -> None:
    d = GitReinsDefaults()
    result = d.overlay({"defaults": {"max_time": "10m"}})
    assert result.max_seconds == 600.0


def test_overlay_respects_token_string() -> None:
    d = GitReinsDefaults()
    result = d.overlay({"defaults": {"max_input_tokens": "1M"}})
    assert result.max_input_tokens == 1_000_000


def test_overlay_sets_source_to_config_yaml() -> None:
    d = GitReinsDefaults()
    result = d.overlay({"defaults": {"model": "test"}})
    assert "config.yaml" in result._source


def test_overlay_nested_commit_audit() -> None:
    d = GitReinsDefaults()
    result = d.overlay(
        {
            "defaults": {
                "commit_audit": {
                    "mode": "block",
                    "review_checks": {"style": True},
                }
            }
        }
    )
    assert result.commit_audit_mode == "block"
    assert result.commit_audit_review_checks_style is True
    # unchanged defaults remain
    assert result.commit_audit_review_checks_bugs is True


# ── to_config_dict ───────────────────────────────────────────


def test_to_config_dict_includes_model() -> None:
    d = GitReinsDefaults()
    result = d.to_config_dict()
    assert result["model"] == "deepseek-v4-flash"


def test_to_config_dict_includes_commit_audit_nested() -> None:
    d = GitReinsDefaults()
    result = d.to_config_dict()
    assert "commit_audit" in result
    assert result["commit_audit"]["mode"] == "warn"
    assert result["commit_audit"]["review_checks"]["bugs"] is True


def test_to_config_dict_formats_max_time_as_string() -> None:
    d = GitReinsDefaults()
    d.max_seconds = 600.0
    result = d.to_config_dict()
    assert result["max_time"] == "10m"


def test_to_config_dict_format_none_for_unlimited() -> None:
    d = GitReinsDefaults()
    d.max_seconds = -1.0
    result = d.to_config_dict()
    assert result["max_time"] is None


# ── _coerce_float ────────────────────────────────────────────


def test_coerce_float_from_int() -> None:
    assert _coerce_float(100) == 100.0


def test_coerce_float_from_string() -> None:
    assert _coerce_float("50") == 50.0


def test_coerce_float_from_invalid_string() -> None:
    assert _coerce_float("invalid") == -1.0


# ── _coerce_seconds ──────────────────────────────────────────


@pytest.mark.parametrize(
    ("val", "expected"),
    [
        (30, 30.0),
        ("30s", 30.0),
        ("5m", 300.0),
        ("2h", 7200.0),
        ("1.5m", 90.0),
        ("10min", 600.0),
    ],
)
def test_coerce_seconds_valid(val: object, expected: object) -> None:
    assert _coerce_seconds(val) == expected


def test_coerce_seconds_invalid_returns_negative() -> None:
    assert _coerce_seconds("nope") == -1.0


# ── _coerce_tokens ───────────────────────────────────────────


@pytest.mark.parametrize(
    ("val", "expected"),
    [
        (500, 500),
        ("200k", 200_000),
        ("1M", 1_000_000),
        ("0.5M", 500_000),
        ("100", 100),
    ],
)
def test_coerce_tokens_valid(val: object, expected: object) -> None:
    assert _coerce_tokens(val) == expected


def test_coerce_tokens_bad_string() -> None:
    assert _coerce_tokens("xyz") == -1


# ── _fmt_seconds ─────────────────────────────────────────────


def test_fmt_seconds_under_60() -> None:
    assert _fmt_seconds(30) == "30s"


def test_fmt_seconds_exact_minutes() -> None:
    assert _fmt_seconds(120) == "2m"


def test_fmt_seconds_minutes_and_seconds() -> None:
    assert _fmt_seconds(150) == "2m30s"


def test_fmt_seconds_hours() -> None:
    assert _fmt_seconds(3600) == "1h"
    assert _fmt_seconds(7200) == "2h"


# ── _fmt_tokens ──────────────────────────────────────────────


def test_fmt_tokens_millions() -> None:
    assert _fmt_tokens(1_000_000) == "1.0M"


def test_fmt_tokens_thousands() -> None:
    assert _fmt_tokens(200_000) == "200k"


def test_fmt_tokens_small() -> None:
    assert _fmt_tokens(500) == "500"


# ── _version_greater ─────────────────────────────────────────


def test_version_greater_true() -> None:
    assert _version_greater("0.11.0", "0.10.2") is True


def test_version_greater_false() -> None:
    assert _version_greater("0.8.0", "0.10.0") is False


def test_version_greater_equal() -> None:
    assert _version_greater("0.10.2", "0.10.2") is False


# ── _pypi_url ────────────────────────────────────────────────


def test_pypi_url_returns_project_link() -> None:
    url = _pypi_url()
    assert "pypi.org" in url
    assert "gitreins" in url


# ── load_raw_config ──────────────────────────────────────────


def test_load_raw_config_returns_empty_for_none_workdir() -> None:
    assert load_raw_config(None) == {}


def test_load_raw_config_returns_empty_for_nonexistent(tmp_path: Path) -> None:
    assert load_raw_config(str(tmp_path)) == {}


def test_quality_config_round_trip(tmp_path: Path) -> None:
    import yaml

    config_dir = tmp_path / ".gitreins"
    config_dir.mkdir()
    expected = QualityConfig(
        enabled=True,
        command="python measure.py",
        artifact_path=".gitreins/quality.json",
        targets_source="quality/ladder.json",
        per_metric_mode={"type_hint_pct": "block", "coverage_pct": "warn"},
        timeout=42,
    )
    path = config_dir / "config.yaml"
    path.write_text(yaml.safe_dump({"quality": expected.to_dict()}), encoding="utf-8")
    loaded = load_defaults(str(tmp_path)).quality
    path.write_text(yaml.safe_dump({"quality": loaded.to_dict()}), encoding="utf-8")
    assert load_defaults(str(tmp_path)).quality == expected


# ── Comment-preserving config rewrite (GR-194) ────────────────


def _write_commented_config(tmp_path: Path, text: str) -> tuple[Path, Path]:
    """Write a commented config.yaml into a scratch .gitreins dir."""
    gitreins_dir = tmp_path / ".gitreins"
    gitreins_dir.mkdir()
    config_path = gitreins_dir / "config.yaml"
    config_path.write_text(text, encoding="utf-8")
    return gitreins_dir, config_path


def test_comment_rewrite_adding_block_keeps_every_comment(tmp_path: Path) -> None:
    """A rewrite that adds a missing block leaves untouched lines byte-identical."""
    import yaml

    from engine.config import GitReinsDefaults
    from gitreins.cli import _write_init_config

    original = (
        "# operator-maintained — do not bulk-reformat\n"
        "guards:\n"
        "  test_command: pytest -q  # keep the fast suite, not the full battery\n"
        "  allow_skips: false  # a DEGRADED pass is not a pass\n"
        "\n"
        "# JEV rationale: every surface stays off until calibration lands\n"
        "# (JEVRES-006) — the gate makes a real third-party egress.\n"
    )
    gitreins_dir, config_path = _write_commented_config(tmp_path, original)
    existing = yaml.safe_load(original)
    existing["resolution"] = GitReinsDefaults().to_config_dict()["resolution"]

    changed: list[str] = []
    _write_init_config(str(gitreins_dir), str(config_path), existing, changed)

    new_text = config_path.read_text(encoding="utf-8")
    # Untouched lines byte-identical: the merge only APPENDS the new block.
    assert new_text.startswith(original)
    assert "resolution:" in new_text
    assert yaml.safe_load(new_text) == existing
    for marker in (
        "# operator-maintained — do not bulk-reformat",
        "# keep the fast suite, not the full battery",
        "# a DEGRADED pass is not a pass",
        "# JEV rationale: every surface stays off until calibration lands",
    ):
        assert marker in new_text
    # Backup behaviour unchanged: _safe_overwrite recorded one.
    assert any(c.startswith("backup:") for c in changed)
    assert (gitreins_dir / "config.yaml.bak").read_text(encoding="utf-8") == original

    # Idempotent second run: identical bytes -> no write, no backup churn (DF-022).
    changed_second: list[str] = []
    _write_init_config(str(gitreins_dir), str(config_path), existing, changed_second)
    assert config_path.read_text(encoding="utf-8") == new_text
    assert changed_second == []


def test_comment_rewrite_patches_leaf_keeping_inline_comment(tmp_path: Path) -> None:
    """A replaced leaf value keeps its line's trailing rationale comment verbatim."""
    import yaml

    from gitreins.cli import _write_init_config

    original = (
        "guards:\n"
        "  test_command: python3 -m pytest -x --tb=short  # install default\n"
        "  allow_skips: false  # fail loud on skips\n"
    )
    gitreins_dir, config_path = _write_commented_config(tmp_path, original)
    existing = yaml.safe_load(original)
    existing["guards"]["test_command"] = "pytest -q"  # the upgrade path's exact change

    _write_init_config(str(gitreins_dir), str(config_path), existing, [])

    new_text = config_path.read_text(encoding="utf-8")
    # Value patched, comment (and its two-space gap) byte-identical:
    assert "  test_command: pytest -q  # install default\n" in new_text
    # The untouched sibling line is byte-identical:
    assert "  allow_skips: false  # fail loud on skips\n" in new_text
    assert yaml.safe_load(new_text) == existing


def test_comment_merge_refusal_leaves_file_byte_identical(tmp_path: Path) -> None:
    """A block that changes value while carrying anchors is refused, file untouched."""
    import yaml

    from gitreins.cli import _write_init_config

    original = (
        "# shared anchor for the model defaults\n"
        "defaults: &base\n"
        "  model: deepseek-v4-flash\n"
        "guards:\n"
        "  test_command: pytest -q  # rationale that must survive any refusal\n"
    )
    gitreins_dir, config_path = _write_commented_config(tmp_path, original)
    before = config_path.read_bytes()
    existing = yaml.safe_load(original)
    existing["defaults"]["model"] = "other-model"  # mutates the anchored block

    with pytest.raises(SystemExit) as excinfo:
        _write_init_config(str(gitreins_dir), str(config_path), existing, [])

    assert excinfo.value.code == 1
    assert config_path.read_bytes() == before  # byte-identical: never a stripped write
    assert not (gitreins_dir / "config.yaml.bak").exists()


def test_comment_merge_refuses_setting_removal_in_additive_mode() -> None:
    """An additive upgrade that drops a user's key refuses instead of stripping."""
    import yaml

    from engine.config import ConfigMergeError, merge_config_text

    original = (
        "guards:\n"
        "  test_command: pytest -q  # keep the fast suite\n"
        "  allow_skips: false  # a DEGRADED pass is not a pass\n"
    )
    merged = {"guards": {"test_command": "pytest -q"}}  # allow_skips removed
    with pytest.raises(ConfigMergeError) as excinfo:
        merge_config_text(original, merged)
    assert "allow_skips" in str(excinfo.value)
    assert "never strips" in str(excinfo.value)

    # With an explicit section rebuild (init --reset semantics) the block is
    # re-rendered loudly — values land, the warning names the section.
    warned: list[str] = []
    text = merge_config_text(
        original, merged, on_full_render=warned.append, allow_section_rebuild=True
    )
    assert yaml.safe_load(text) == merged
    assert warned == ["guards"]
