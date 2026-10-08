"""Quality config round-trip coverage."""

import yaml

from engine.config import QualityConfig, load_defaults
from pathlib import Path


def test_quality_config_load_save_load_round_trip(tmp_path: Path) -> None:
    config_dir = tmp_path / ".gitreins"
    config_dir.mkdir()
    source = {
        "enabled": True,
        "command": "python measure.py",
        "artifact_path": ".gitreins/quality.json",
        "targets_source": "quality/ladder.json",
        "per_metric_mode": {"type_hint_pct": "block", "coverage_pct": "warn"},
        "timeout": 42,
    }
    config_path = config_dir / "config.yaml"
    config_path.write_text(yaml.safe_dump({"quality": source}), encoding="utf-8")
    first = load_defaults(str(tmp_path)).quality
    assert first == QualityConfig.from_dict(source)
    config_path.write_text(yaml.safe_dump({"quality": first.to_dict()}), encoding="utf-8")
    second = load_defaults(str(tmp_path)).quality
    assert second == first


def test_quality_defaults_are_disabled_and_warn_by_default() -> None:
    cfg = QualityConfig.from_dict({})
    assert cfg.enabled is False
    assert cfg.timeout == 300
    assert cfg.per_metric_mode == {}
