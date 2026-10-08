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
