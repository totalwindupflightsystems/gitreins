"""Tests for consuming externally-produced quality metric artifacts."""

import sys
import subprocess
from types import SimpleNamespace

import pytest
import yaml

from engine.config import QualityConfig
from engine.guard_manager import GuardManager
from engine.judge import JudgeResult, judge_result_to_dict
from engine.quality_metrics import (
    format_quality_snapshot,
    quality_snapshot_peek,
    read_quality_snapshot,
)


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


def _produce(tmp_path, metric_value=72.3, target=80, surfaces=None, class_floor=None):
    artifact = {
        "metrics": {"type_hint_pct": {"value": metric_value, "target": target, "stage": "stage-2"}},
        "produced_at": "test",
        "command": "repo-producer",
    }
    if surfaces is not None:
        artifact["surfaces"] = surfaces
    if class_floor is not None:
        artifact["class_floor"] = class_floor
    (tmp_path / "producer.py").write_text(
        "import json, pathlib\n"
        "p = pathlib.Path('.gitreins/count')\n"
        "p.write_text(str(int(p.read_text()) + 1) if p.exists() else '1')\n"
        f"pathlib.Path('.gitreins/quality.json').write_text(json.dumps({artifact!r}))\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "producer.py"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "baseline"], cwd=tmp_path, check=True)


def _stage_surface_change(tmp_path, path="src/COV-1/component.py"):
    target = tmp_path / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("changed\n", encoding="utf-8")
    subprocess.run(["git", "add", path], cwd=tmp_path, check=True)


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
    assert "type_hint_pct=72.3% (target 80%, block, stage stage-2, via repo-producer)" in (
        result.results[-1].output
    )
    assert result.extra["quality_snapshot"]["metrics"]["type_hint_pct"]["met_target"] is False
    assert (tmp_path / ".gitreins/count").read_text() == "1"


def test_warn_metric_does_not_fail_guard(tmp_path):
    _produce(tmp_path)
    quality = _config(tmp_path, per_metric_mode={"type_hint_pct": "warn"}).to_dict()
    result = _guard(tmp_path, quality)
    assert result.passed is True
    assert "type_hint_pct=72.3% (target 80%, warn, stage stage-2, via repo-producer)" in (
        result.summary
    )


def test_diff_scope_reports_staged_surface_and_warns_without_test_link(tmp_path):
    _produce(
        tmp_path,
        surfaces={"COV-1": {"test_links": 0, "classes": ["unit"]}},
        class_floor=["unit", "integration"],
    )
    _stage_surface_change(tmp_path)
    result = _guard(tmp_path, _config(tmp_path).to_dict())
    scope = result.extra["quality_snapshot"]["diff_scope"]
    assert scope == {
        "status": "available",
        "base": "HEAD",
        "diff_files": 1,
        "surfaces": {
            "COV-1": {
                "test_links": 0,
                "link_status": "none",
                "classes": ["unit"],
                "missing_classes": ["integration"],
            }
        },
    }
    assert "diff-scope: 1 changed files, 1 surfaces touched" in result.summary
    assert "WARNING: surface COV-1 has no test link" in result.summary
    assert result.passed is True


def test_diff_scope_without_surfaces_is_unavailable(tmp_path):
    _produce(tmp_path)
    _stage_surface_change(tmp_path)
    scope = _guard(tmp_path, _config(tmp_path).to_dict()).extra["quality_snapshot"]["diff_scope"]
    assert scope["status"] == "unavailable"
    assert scope["reason"] == "producer artifact has no surfaces model"
    assert scope["diff_files"] == 1
    assert scope["surfaces"] == {}


def test_docs_only_diff_has_no_surface_findings(tmp_path):
    _produce(tmp_path, surfaces={"COV-1": {"test_links": ["test"], "classes": ["unit"]}})
    _stage_surface_change(tmp_path, "docs/guide.md")
    result = _guard(tmp_path, _config(tmp_path).to_dict())
    scope = result.extra["quality_snapshot"]["diff_scope"]
    assert scope["status"] == "no_diff"
    assert scope["surfaces"] == {}
    assert "diff-scope: no artifact surfaces touched" in result.summary


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
    assert (
        "type_hint_pct=82% (target 80%, warn, stage stage-2, via repo-producer)" in result.summary
    )


# ── GR-143: one snapshot, every surface ──────────────────────────


@pytest.fixture(autouse=True)
def _fresh_quality_cache():
    """The snapshot cache is process-global; tests must not see each other's."""
    from engine.quality_metrics import _snapshot_cache

    _snapshot_cache.clear()
    yield
    _snapshot_cache.clear()


def test_surfaces_report_identical_numbers(tmp_path, monkeypatch, capsys):
    """THE parity cell: guard + judge + doctor + CLI + MCP, one producer run.

    A producer and the same enabled-quality config drive every surface over
    one tmp repo. Each surface's snapshot dict AND its rendered line must be
    identical to the guard's, and the producer must have run exactly once
    (the cache, not a second producer run, is what the later surfaces read).
    """
    import json as _json

    from gitreins import cli
    from gitreins_mcp.server import GitReinsMCPServer

    _produce(
        tmp_path,
        metric_value=82,
        target=80,
        surfaces={
            "COV-1": {"test_links": ["tests/test_component.py"], "classes": ["unit", "integration"]}
        },
        class_floor=["unit", "integration"],
    )
    _stage_surface_change(tmp_path)
    quality_cfg = _config(tmp_path).to_dict()
    (tmp_path / ".gitreins/config.yaml").write_text(
        yaml.safe_dump({"quality": quality_cfg}), encoding="utf-8"
    )

    # Surface 1 — guard.
    tier1 = _guard(tmp_path, quality_cfg)
    guard_snapshot = tier1.extra["quality_snapshot"]
    guard_line = format_quality_snapshot(guard_snapshot)

    # Surface 2 — judge verdict dict (what MCP judge.evaluate / judge.status
    # return and what the persisted verdict carries).
    result = JudgeResult(task_id="GR-143", passed=True, tier1=tier1)
    judge_payload = judge_result_to_dict("GR-143", str(tmp_path), result)

    # Surface 3 — doctor (prints the block) + Surface 4 — quality_snapshot_for
    # (the CLI's one computation point).
    monkeypatch.setattr(cli, "get_workdir", lambda: str(tmp_path))
    cli.cmd_doctor(SimpleNamespace(config=None, fix=False))
    doctor_out = capsys.readouterr().out
    cli_snapshot = cli.quality_snapshot_for(str(tmp_path))

    # Surface 5 — MCP quality.status (read-only peek at the run's snapshot).
    server = GitReinsMCPServer(str(tmp_path))
    response = server.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "quality.status", "arguments": {}},
        }
    )
    mcp_payload = _json.loads(response["result"]["content"][0]["text"])

    # The parity assertions: same dict, same line, everywhere.
    assert judge_payload["quality_snapshot"] == guard_snapshot
    assert cli_snapshot == guard_snapshot
    assert mcp_payload["quality_snapshot"] == guard_snapshot
    assert guard_line in doctor_out
    assert guard_line in result.summary
    # value + target + stage + producer command on every printed line.
    assert guard_line == (
        "quality: type_hint_pct=82% (target 80%, warn, stage stage-2, via repo-producer)\n"
        "diff-scope: 1 changed files, 1 surfaces touched\n"
        "  surface COV-1: test_links=linked, classes=integration,unit, missing_classes=none"
    )
    # One producer run for the whole run: count stays at 1.
    assert (tmp_path / ".gitreins/count").read_text() == "1"


def test_cached_snapshot_reused_across_surfaces_without_rerunning_producer(tmp_path):
    """The cache — not the producer — serves the second surface."""
    _produce(tmp_path)
    cfg = _config(tmp_path)
    first = read_quality_snapshot(str(tmp_path), cfg)
    assert (tmp_path / ".gitreins/count").read_text() == "1"
    # A second surface with a fresh config object gets the SAME dict...
    second = read_quality_snapshot(str(tmp_path), _config(tmp_path))
    assert second == first
    # ...without the producer running again.
    assert (tmp_path / ".gitreins/count").read_text() == "1"


def test_cached_snapshot_regrades_modes_from_calling_config(tmp_path):
    """Shared numbers, caller-owned policy: the guard's block grade is not
    copied into a warn-mode reader's snapshot."""
    _produce(tmp_path, metric_value=72.3, target=80)
    block_cfg = _config(tmp_path, per_metric_mode={"type_hint_pct": "block"})
    guard_view = read_quality_snapshot(str(tmp_path), block_cfg)
    assert guard_view["metrics"]["type_hint_pct"]["met_target"] is False

    warn_view = read_quality_snapshot(str(tmp_path), _config(tmp_path))
    assert warn_view["metrics"]["type_hint_pct"]["value"] == 72.3
    # The guard's block mode must not have leaked into the warn reader.
    assert warn_view["metrics"]["type_hint_pct"]["mode"] == "warn"
    assert warn_view["metrics"]["type_hint_pct"]["met_target"] is False


def test_snapshot_peek_reads_cache_without_running_producer(tmp_path):
    _produce(tmp_path)
    assert quality_snapshot_peek(str(tmp_path)) is None
    read_quality_snapshot(str(tmp_path), _config(tmp_path))
    peeked = quality_snapshot_peek(str(tmp_path))
    assert peeked is not None
    assert peeked["metrics"]["type_hint_pct"]["value"] == 72.3
    # Peek is read-only: the producer count did not move.
    assert (tmp_path / ".gitreins/count").read_text() == "1"


def test_mcp_quality_status_reports_not_computed_then_the_snapshot(tmp_workdir):
    """quality.status: read-only surface — not-computed before, the run's
    snapshot after guard.run, and never a producer run of its own."""
    import json as _json
    from pathlib import Path

    from gitreins_mcp.server import GitReinsMCPServer

    wd = Path(tmp_workdir)
    server = GitReinsMCPServer(tmp_workdir)
    quality_cfg = _config(wd).to_dict()
    (wd / ".gitreins/config.yaml").write_text(
        yaml.safe_dump({"quality": quality_cfg, "guards": {"secrets": True}}),
        encoding="utf-8",
    )
    _produce(wd, metric_value=91, target=80)

    def call_quality_status():
        response = server.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "quality.status", "arguments": {}},
            }
        )
        return _json.loads(response["result"]["content"][0]["text"])

    # Before any surface computed: read-only not-computed, no producer run.
    before = call_quality_status()
    assert before["status"] == "not-computed"
    assert "guard.run" in before["note"]
    assert not (wd / ".gitreins/count").exists()

    # guard.run computes and carries the snapshot in its response...
    response = server.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "guard.run", "arguments": {}},
        }
    )
    guard_payload = _json.loads(response["result"]["content"][0]["text"])
    guard_snapshot = guard_payload["quality_snapshot"]
    assert guard_snapshot["metrics"]["type_hint_pct"]["value"] == 91
    assert (wd / ".gitreins/count").read_text() == "1"

    # ...and quality.status now reports the SAME snapshot, producer untouched.
    after = call_quality_status()
    assert after["status"] == "available"
    assert after["quality_snapshot"] == guard_snapshot
    assert (wd / ".gitreins/count").read_text() == "1"


def test_guard_and_verdict_json_carry_the_same_snapshot(tmp_path, monkeypatch):
    """Machine-readable parity: tier1.extra and the PERSISTED verdict record
    hold the identical snapshot dict."""
    import engine.persist as persist_mod
    from engine.judge import Judge
    from engine.task_manager import TaskManager

    _produce(tmp_path, metric_value=83, target=80)
    quality_cfg = _config(tmp_path).to_dict()
    (tmp_path / ".gitreins/config.yaml").write_text(
        yaml.safe_dump({"quality": quality_cfg}), encoding="utf-8"
    )
    tier1 = _guard(tmp_path, quality_cfg)

    # --skip-tier2 legacy judge: the surface `gitreins judge --skip-tier2`
    # reports, persisted through the shared verdict path. Tier 2 is skipped
    # so no LLM is contacted; persistence is stubbed to dry-run and the
    # record builder is what is under test.
    monkeypatch.setattr(persist_mod.VerdictPersister, "persist", lambda *a, **k: "dry-run")
    task = TaskManager(str(tmp_path)).create("GR-143", "t", ["c"])
    judge = Judge(SimpleNamespace(api_key="k"), str(tmp_path))
    judge.guard_manager = GuardManager(
        str(tmp_path),
        config={
            "guards": {"secrets": False, "lint": False, "tests": False},
            "quality": quality_cfg,
        },
        persist_log=False,
    )
    result = judge.evaluate_task(task, skip_tier2=True)
    verdict_data = persist_mod.build_verdict_data(str(tmp_path), task, result)

    assert result.quality_snapshot == tier1.extra["quality_snapshot"]
    assert verdict_data["quality_snapshot"] == tier1.extra["quality_snapshot"]


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
    assert "type_hint_pct=82% (target 80%, warn, stage stage-2, via repo-producer)" in output


def test_format_includes_target_stage_and_producer_command(tmp_path):
    _produce(tmp_path, metric_value=66.5, target=70)
    snapshot = read_quality_snapshot(str(tmp_path), _config(tmp_path))
    line = format_quality_snapshot(snapshot)
    # Value, target, stage and the exact producer command, on one line.
    assert "type_hint_pct=66.5% (target 70%, warn, stage stage-2, via repo-producer)" in line


def test_format_without_producer_command_still_complete(tmp_path):
    """A bare artifact (no 'command' key) falls back to the configured
    command — the line still carries the producer."""
    (tmp_path / "producer.py").write_text(
        "import json, pathlib\n"
        "pathlib.Path('.gitreins/quality.json').write_text(json.dumps({"
        "'metrics': {'type_hint_pct': {'value': 50, 'target': 60, 'stage': 's1'}}}))\n",
        encoding="utf-8",
    )
    snapshot = read_quality_snapshot(str(tmp_path), _config(tmp_path))
    line = format_quality_snapshot(snapshot)
    assert "via" in line and sys.executable in line
    assert "stage s1" in line


def test_disabled_snapshot_formats_empty_and_surfaces_stay_none(tmp_path):
    """Disabled quality: every surface reports the historical None/no-block
    shape instead of a snapshot."""
    from gitreins import cli

    assert read_quality_snapshot(str(tmp_path), _config(tmp_path, enabled=False)) == {
        "status": "disabled",
        "metrics": {},
    }
    assert format_quality_snapshot({"status": "disabled", "metrics": {}}) == ""
    assert cli.quality_snapshot_for(str(tmp_path)) is None
