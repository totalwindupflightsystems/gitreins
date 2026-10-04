"""Tests for consuming externally-produced quality metric artifacts."""

import sys
from types import SimpleNamespace

import yaml

from engine.config import QualityConfig
from engine.guard_manager import GuardManager
from engine.judge import JudgeResult, judge_result_to_dict
from engine.quality_metrics import read_quality_snapshot


def _config(tmp_path, **overrides):
    values = {
        "enabled": True,
        "command": f"{sys.executable} producer.py",
        "artifact_path": ".gitreins/quality.json",
        "targets_source": "quality/ladder.json",
        "per_metric_mode": {},
        "timeout": 2,
    }
    values.update(overrides)
    (tmp_path / ".gitreins").mkdir(exist_ok=True)
    return QualityConfig.from_dict(values)


def _produce(tmp_path, metric_value=72.3, target=80):
    (tmp_path / "producer.py").write_text(
        "import json, pathlib\n"
        "p = pathlib.Path('.gitreins/count')\n"
        "p.write_text(str(int(p.read_text()) + 1) if p.exists() else '1')\n"
        f"pathlib.Path('.gitreins/quality.json').write_text(json.dumps({{"
        f"'metrics': {{'type_hint_pct': {{'value': {metric_value}, 'target': {target}, 'stage': 'stage-2'}}}},"
        "'produced_at': 'test', 'command': 'repo-producer'}))\n",
        encoding="utf-8",
    )


def test_reads_valid_artifact_and_runs_producer_once(tmp_path):
    _produce(tmp_path)
    snapshot = read_quality_snapshot(str(tmp_path), _config(tmp_path))
    assert snapshot["status"] == "available"
    assert snapshot["metrics"]["type_hint_pct"]["value"] == 72.3
    assert snapshot["metrics"]["type_hint_pct"]["stage"] == "stage-2"
    assert (tmp_path / ".gitreins/count").read_text() == "1"


def test_missing_artifact_is_unavailable_not_zero(tmp_path):
    (tmp_path / "producer.py").write_text("pass\n", encoding="utf-8")
    snapshot = read_quality_snapshot(str(tmp_path), _config(tmp_path))
    assert snapshot["status"] == "unavailable"
    assert "artifact not found" in snapshot["reason"]
    assert snapshot["metrics"] == {}


def test_malformed_artifact_is_unavailable(tmp_path):
    (tmp_path / ".gitreins").mkdir()
    (tmp_path / ".gitreins/quality.json").write_text("{bad", encoding="utf-8")
    config = _config(tmp_path, command=f'{sys.executable} -c "pass"')
    snapshot = read_quality_snapshot(str(tmp_path), config)
    assert snapshot["status"] == "unavailable"
    assert "artifact unreadable" in snapshot["reason"]


def test_nonzero_command_is_unavailable(tmp_path):
    snapshot = read_quality_snapshot(str(tmp_path), _config(tmp_path, command="exit 7"))
    assert snapshot["status"] == "unavailable"
    assert "command failed" in snapshot["reason"]


def test_timeout_is_unavailable(tmp_path):
    config = _config(tmp_path, command="sleep 2", timeout=1)
    snapshot = read_quality_snapshot(str(tmp_path), config)
    assert snapshot["status"] == "unavailable"
    assert "timed out" in snapshot["reason"]


def _guard(tmp_path, quality=None):
    config = {"guards": {"secrets": False, "lint": False, "tests": False}}
    if quality is not None:
        config["quality"] = quality
    return GuardManager(str(tmp_path), config=config, persist_log=False).run_all()


def test_guard_surfaces_enabled_quality_and_blocks_opt_in_miss(tmp_path):
    _produce(tmp_path)
    quality = _config(tmp_path, per_metric_mode={"type_hint_pct": "block"}).to_dict()
    result = _guard(tmp_path, quality)
    assert result.passed is False
    assert "type_hint_pct=72.3% (target 80%, block)" in result.summary
    assert result.extra["quality_snapshot"]["metrics"]["type_hint_pct"]["met_target"] is False
    assert (tmp_path / ".gitreins/count").read_text() == "1"


def test_warn_metric_does_not_fail_guard(tmp_path):
    _produce(tmp_path)
    quality = _config(tmp_path, per_metric_mode={"type_hint_pct": "warn"}).to_dict()
    result = _guard(tmp_path, quality)
    assert result.passed is True
    assert "type_hint_pct=72.3% (target 80%, warn)" in result.summary


def test_disabled_quality_produces_no_guard_output(tmp_path):
    result = _guard(tmp_path, {"enabled": False})
    assert "quality" not in result.summary.lower()
    assert "quality_snapshot" not in result.extra


def test_quality_targets_are_metric_targets_not_stage_labels(tmp_path):
    _produce(tmp_path, metric_value=82, target=80)
    quality = _config(tmp_path, per_metric_mode={"type_hint_pct": "block"}).to_dict()
    result = _guard(tmp_path, quality)
    assert result.passed is True
    assert result.extra["quality_snapshot"]["metrics"]["type_hint_pct"]["stage"] == "stage-2"


def test_judge_result_carries_quality_snapshot(tmp_path):
    _produce(tmp_path, metric_value=82, target=80)
    quality = _config(tmp_path).to_dict()
    tier1 = _guard(tmp_path, quality)
    result = JudgeResult(task_id="GR-142", passed=True, tier1=tier1)
    payload = judge_result_to_dict("GR-142", str(tmp_path), result)
    assert payload["quality_snapshot"]["metrics"]["type_hint_pct"]["value"] == 82
    assert "type_hint_pct=82% (target 80%, warn)" in result.summary


def test_doctor_shows_enabled_quality_metrics(tmp_path, monkeypatch, capsys):
    from gitreins import cli

    producer = _config(tmp_path).to_dict()
    (tmp_path / ".gitreins/config.yaml").write_text(
        yaml.safe_dump({"quality": producer}), encoding="utf-8"
    )
    _produce(tmp_path, metric_value=82, target=80)
    monkeypatch.setattr(cli, "get_workdir", lambda: str(tmp_path))
    cli.cmd_doctor(SimpleNamespace(config=None, fix=False))
    output = capsys.readouterr().out
    assert "Quality metrics:" in output
    assert "type_hint_pct=82% (target 80%, warn)" in output
