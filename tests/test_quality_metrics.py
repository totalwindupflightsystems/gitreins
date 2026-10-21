"""Tests for consuming externally-produced quality metric artifacts."""

import json
import sys
import subprocess
from types import SimpleNamespace

import pytest
import yaml

from engine.judge import JudgeResult, judge_result_to_dict
from engine.quality_metrics import (
    format_quality_snapshot,
    quality_blocks,
    quality_snapshot_peek,
    read_quality_snapshot,
)
from engine.config import QualityConfig
from engine.guard_manager import GuardManager
from collections.abc import Iterator
from pathlib import Path


def _config(tmp_path: Path, **overrides: object) -> object:
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


def _produce(
    tmp_path: Path,
    metric_value: object = 72.3,
    target: float = 80,
    surfaces: list[str] | None = None,
    class_floor: int | None = None,
) -> None:
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


def _stage_surface_change(tmp_path: Path, path: str = "src/COV-1/component.py") -> None:
    target = tmp_path / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("changed\n", encoding="utf-8")
    subprocess.run(["git", "add", path], cwd=tmp_path, check=True)


def test_reads_valid_artifact_and_runs_producer_once(tmp_path: Path) -> None:
    _produce(tmp_path)
    snapshot = read_quality_snapshot(str(tmp_path), _config(tmp_path))
    assert snapshot["status"] == "available"
    assert snapshot["metrics"]["type_hint_pct"]["value"] == 72.3
    assert snapshot["metrics"]["type_hint_pct"]["stage"] == "stage-2"
    assert (tmp_path / ".gitreins/count").read_text() == "1"


def test_missing_artifact_is_unavailable_not_zero(tmp_path: Path) -> None:
    (tmp_path / "producer.py").write_text("pass\n", encoding="utf-8")
    snapshot = read_quality_snapshot(str(tmp_path), _config(tmp_path))
    assert snapshot["status"] == "unavailable"
    assert "artifact not found" in snapshot["reason"]
    assert snapshot["metrics"] == {}


def test_malformed_artifact_is_unavailable(tmp_path: Path) -> None:
    (tmp_path / ".gitreins").mkdir()
    (tmp_path / ".gitreins/quality.json").write_text("{bad", encoding="utf-8")
    config = _config(tmp_path, command=f'{sys.executable} -c "pass"')
    snapshot = read_quality_snapshot(str(tmp_path), config)
    assert snapshot["status"] == "unavailable"
    assert "artifact unreadable" in snapshot["reason"]


def test_nonzero_command_is_unavailable(tmp_path: Path) -> None:
    snapshot = read_quality_snapshot(str(tmp_path), _config(tmp_path, command="exit 7"))
    assert snapshot["status"] == "unavailable"
    assert "command failed" in snapshot["reason"]


def test_timeout_is_unavailable(tmp_path: Path) -> None:
    config = _config(tmp_path, command="sleep 2", timeout=1)
    snapshot = read_quality_snapshot(str(tmp_path), config)
    assert snapshot["status"] == "unavailable"
    assert "timed out" in snapshot["reason"]


def _guard(tmp_path: Path, quality: object = None) -> object:
    config = {"guards": {"secrets": False, "lint": False, "tests": False}}
    if quality is not None:
        config["quality"] = quality
    return GuardManager(str(tmp_path), config=config, persist_log=False).run_all()


def test_guard_surfaces_enabled_quality_and_blocks_opt_in_miss(tmp_path: Path) -> None:
    _produce(tmp_path)
    quality = _config(tmp_path, per_metric_mode={"type_hint_pct": "block"}).to_dict()
    result = _guard(tmp_path, quality)
    assert result.passed is False
    assert "type_hint_pct=72.3% (target 80%, block, stage stage-2, via repo-producer)" in (
        result.results[-1].output
    )
    assert result.extra["quality_snapshot"]["metrics"]["type_hint_pct"]["met_target"] is False
    assert (tmp_path / ".gitreins/count").read_text() == "1"


def test_warn_metric_does_not_fail_guard(tmp_path: Path) -> None:
    _produce(tmp_path)
    quality = _config(tmp_path, per_metric_mode={"type_hint_pct": "warn"}).to_dict()
    result = _guard(tmp_path, quality)
    assert result.passed is True
    assert "type_hint_pct=72.3% (target 80%, warn, stage stage-2, via repo-producer)" in (
        result.summary
    )


def test_diff_scope_reports_staged_surface_and_warns_without_test_link(tmp_path: Path) -> None:
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


def test_diff_scope_without_surfaces_is_unavailable(tmp_path: Path) -> None:
    _produce(tmp_path)
    _stage_surface_change(tmp_path)
    scope = _guard(tmp_path, _config(tmp_path).to_dict()).extra["quality_snapshot"]["diff_scope"]
    assert scope["status"] == "unavailable"
    assert scope["reason"] == "producer artifact has no surfaces model"
    assert scope["diff_files"] == 1
    assert scope["surfaces"] == {}


def test_docs_only_diff_has_no_surface_findings(tmp_path: Path) -> None:
    _produce(tmp_path, surfaces={"COV-1": {"test_links": ["test"], "classes": ["unit"]}})
    _stage_surface_change(tmp_path, "docs/guide.md")
    result = _guard(tmp_path, _config(tmp_path).to_dict())
    scope = result.extra["quality_snapshot"]["diff_scope"]
    assert scope["status"] == "no_diff"
    assert scope["surfaces"] == {}
    assert "diff-scope: no artifact surfaces touched" in result.summary


def test_diff_scope_excludes_harness_state_from_change_set(tmp_path: Path) -> None:
    """DF-GITREINS-POC-97: harness state never counts as a user change.

    The change set behind ``diff_files`` is filtered directly: everything
    under ``.gitreins/**`` plus the producer's own artifact path is excluded,
    whatever the producer rewrites on a given run.
    """
    from engine.quality_metrics import _diff_scope

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "keep.txt").write_text("baseline\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "baseline"], cwd=tmp_path, check=True)
    for path in (
        ".gitreins/quality.json",
        ".gitreins/logs/guard.log",
        "src/COV-1/app.py",
    ):
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    artifact = {
        "metrics": {"type_hint_pct": {"value": 82, "target": 80}},
        "surfaces": {"COV-1": {"test_links": ["tests/test_cov.py"], "classes": ["unit"]}},
    }
    scope = _diff_scope(str(tmp_path), artifact, artifact_path=".gitreins/quality.json")
    # Three dirty paths, two of them harness state (.gitreins/quality.json —
    # which is ALSO the artifact path — and .gitreins/logs/guard.log): the
    # change set must count only the one real source file.
    assert scope["diff_files"] == 1
    assert scope["status"] == "available"
    assert "COV-1" in scope["surfaces"]


def test_diff_scope_ignores_dirty_quality_artifact(tmp_path: Path) -> None:
    """Only the quality artifact is dirty (no user changes) → ``no_diff`` / 0."""
    from engine.quality_metrics import _snapshot_cache

    def fresh_guard(quality: object) -> object:
        # Each gitreins invocation is a fresh process; the snapshot cache is
        # process-local, so a new simulated invocation must not reuse the
        # previous one's diff-scope numbers.
        _snapshot_cache.clear()
        return _guard(tmp_path, quality)

    _produce(
        tmp_path,
        surfaces={"COV-1": {"test_links": ["tests/test_cov.py"], "classes": ["unit"]}},
    )
    # First run: the producer writes .gitreins/quality.json + count files.
    result = fresh_guard(_config(tmp_path).to_dict())
    assert result.extra["quality_snapshot"]["diff_scope"]["diff_files"] == 0
    # The user stages the artifact (committing everything) — still not a
    # user change: the producer owns that file.
    subprocess.run(
        ["git", "add", ".gitreins/quality.json", ".gitreins/count"],
        cwd=tmp_path,
        check=True,
    )
    result = fresh_guard(_config(tmp_path).to_dict())
    scope = result.extra["quality_snapshot"]["diff_scope"]
    assert scope == {"status": "no_diff", "base": "HEAD", "diff_files": 0, "surfaces": {}}
    assert "diff-scope: no artifact surfaces touched" in result.summary


def test_diff_scope_counts_real_change_besides_rewritten_artifact(
    tmp_path: Path,
) -> None:
    """A timestamped producer rewriting the artifact must not inflate the count.

    One real staged change + a producer that stamps ``produced_at`` (so
    .gitreins/quality.json is rewritten on every run) reports exactly
    ``1 changed files`` — not 2.
    """
    from engine.quality_metrics import _snapshot_cache

    def fresh_guard(quality: object) -> object:
        # Fresh-process semantics: clear the run-local snapshot cache so this
        # invocation re-runs the producer instead of reusing cached numbers.
        _snapshot_cache.clear()
        return _guard(tmp_path, quality)

    _produce(
        tmp_path,
        surfaces={"COV-1": {"test_links": ["tests/test_cov.py"], "classes": ["unit"]}},
        class_floor=["unit"],
    )
    (tmp_path / "producer.py").write_text(
        "import json, pathlib, datetime\n"
        "artifact = {\n"
        "    'metrics': {\n"
        "        'type_hint_pct': {'value': 82, 'target': 80, 'stage': 'stage-2'}\n"
        "    },\n"
        "    'produced_at': datetime.datetime.now().isoformat(),\n"
        "    'command': 'repo-producer',\n"
        "    'surfaces': {'COV-1': {'test_links': ['tests/test_cov.py'],"
        " 'classes': ['unit']}},\n"
        "    'class_floor': ['unit'],\n"
        "}\n"
        "pathlib.Path('.gitreins').mkdir(exist_ok=True)\n"
        "pathlib.Path('.gitreins/quality.json').write_text(json.dumps(artifact))\n",
        encoding="utf-8",
    )
    # Run 1: the timestamped producer rewrites the artifact; commit everything
    # (artifact included) so the tree starts clean.
    fresh_guard(_config(tmp_path).to_dict())
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "commit artifact"], cwd=tmp_path, check=True)
    # Run 2 proves the tree-dirty premise: the producer just rewrote the
    # tracked artifact again, so git status shows it as modified.
    fresh_guard(_config(tmp_path).to_dict())
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=tmp_path, check=True, capture_output=True
    )
    assert b".gitreins/quality.json" in status.stdout
    # One real staged change: the diff-scope count must stay at 1.
    _stage_surface_change(tmp_path)
    result = fresh_guard(_config(tmp_path).to_dict())
    scope = result.extra["quality_snapshot"]["diff_scope"]
    assert scope["status"] == "available"
    assert scope["diff_files"] == 1
    assert "COV-1" in scope["surfaces"]
    assert "diff-scope: 1 changed files, 1 surfaces touched" in result.summary


def test_disabled_quality_produces_no_guard_output(tmp_path: Path) -> None:
    result = _guard(tmp_path, {"enabled": False})
    assert "quality" not in result.summary.lower()
    assert "quality_snapshot" not in result.extra


def test_quality_targets_are_metric_targets_not_stage_labels(tmp_path: Path) -> None:
    _produce(tmp_path, metric_value=82, target=80)
    quality = _config(tmp_path, per_metric_mode={"type_hint_pct": "block"}).to_dict()
    result = _guard(tmp_path, quality)
    assert result.passed is True
    assert result.extra["quality_snapshot"]["metrics"]["type_hint_pct"]["stage"] == "stage-2"


def test_judge_result_carries_quality_snapshot(tmp_path: Path) -> None:
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
def _fresh_quality_cache() -> Iterator:
    """The snapshot cache is process-global; tests must not see each other's."""
    from engine.quality_metrics import _snapshot_cache

    _snapshot_cache.clear()
    yield
    _snapshot_cache.clear()


def test_surfaces_report_identical_numbers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
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


def test_cached_snapshot_reused_across_surfaces_without_rerunning_producer(tmp_path: Path) -> None:
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


def test_cached_snapshot_regrades_modes_from_calling_config(tmp_path: Path) -> None:
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


def test_snapshot_peek_reads_cache_without_running_producer(tmp_path: Path) -> None:
    _produce(tmp_path)
    assert quality_snapshot_peek(str(tmp_path)) is None
    read_quality_snapshot(str(tmp_path), _config(tmp_path))
    peeked = quality_snapshot_peek(str(tmp_path))
    assert peeked is not None
    assert peeked["metrics"]["type_hint_pct"]["value"] == 72.3
    # Peek is read-only: the producer count did not move.
    assert (tmp_path / ".gitreins/count").read_text() == "1"


def test_mcp_quality_status_reports_not_computed_then_the_snapshot(tmp_workdir: str) -> object:
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

    def call_quality_status() -> object:
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


def test_guard_and_verdict_json_carry_the_same_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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


def test_doctor_shows_enabled_quality_metrics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
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


def test_format_includes_target_stage_and_producer_command(tmp_path: Path) -> None:
    _produce(tmp_path, metric_value=66.5, target=70)
    snapshot = read_quality_snapshot(str(tmp_path), _config(tmp_path))
    line = format_quality_snapshot(snapshot)
    # Value, target, stage and the exact producer command, on one line.
    assert "type_hint_pct=66.5% (target 70%, warn, stage stage-2, via repo-producer)" in line


def test_format_without_producer_command_still_complete(tmp_path: Path) -> None:
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


def test_disabled_snapshot_formats_empty_and_surfaces_stay_none(tmp_path: Path) -> None:
    """Disabled quality: every surface reports the historical None/no-block
    shape instead of a snapshot."""
    from gitreins import cli

    assert read_quality_snapshot(str(tmp_path), _config(tmp_path, enabled=False)) == {
        "status": "disabled",
        "metrics": {},
    }
    assert format_quality_snapshot({"status": "disabled", "metrics": {}}) == ""
    assert cli.quality_snapshot_for(str(tmp_path)) is None


# ── DF-GITREINS-POC-95: the declared ladder target is authoritative ────


def _write_ladder(tmp_path: Path, ladder: dict | str | None) -> None:
    """Write quality/ladder.json (dict, raw string, or None to skip)."""
    (tmp_path / "quality").mkdir(exist_ok=True)
    if isinstance(ladder, str):
        (tmp_path / "quality" / "ladder.json").write_text(ladder, encoding="utf-8")
    elif ladder is not None:
        (tmp_path / "quality" / "ladder.json").write_text(json.dumps(ladder), encoding="utf-8")


def _write_artifact_direct(tmp_path: Path, value: float, target: float | None) -> None:
    """Write the artifact without a producer round-trip and commit a clean tree."""
    (tmp_path / ".gitreins").mkdir(exist_ok=True)
    metric: dict[str, object] = {"value": value, "stage": "current"}
    if target is not None:
        metric["target"] = target
    artifact = {
        "metrics": {"type_hint_pct": metric},
        "produced_at": "local",
        "command": "repo-producer",
    }
    (tmp_path / ".gitreins" / "quality.json").write_text(json.dumps(artifact), encoding="utf-8")
    (tmp_path / "producer.py").write_text("pass\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "baseline"], cwd=tmp_path, check=True)


def _block_config(tmp_path: Path, **overrides: object) -> object:
    return _config(tmp_path, per_metric_mode={"type_hint_pct": "block"}, **overrides)


def test_lowered_artifact_target_cannot_pass_block_gate(tmp_path: Path) -> None:
    """AC-1: ladder 80 + artifact target 10 + block mode must BLOCK.

    Pre-fix the artifact's own target was the only grade input, so the
    repository could pass its block gate by lowering it (ladder 80, artifact
    target 10 → 'target 10%', guard exit 0)."""
    _write_ladder(tmp_path, {"type_hint_pct": 80})
    _write_artifact_direct(tmp_path, 72.3, target=10)
    snapshot = read_quality_snapshot(str(tmp_path), _block_config(tmp_path))
    row = snapshot["metrics"]["type_hint_pct"]
    assert row["target"] == 80
    assert row["ladder_target"] == 80
    assert row["artifact_target"] == 10
    assert row["target_source"] == "ladder+artifact"
    assert row["met_target"] is False
    assert quality_blocks(snapshot) is True


def test_missing_artifact_target_blocks_from_ladder(tmp_path: Path) -> None:
    """AC-2: no artifact target + ladder 80 + block mode must BLOCK.

    Pre-fix the row rendered 'target n/a' and a block gate could never
    fire."""
    _write_ladder(tmp_path, {"type_hint_pct": 80})
    _write_artifact_direct(tmp_path, 72.3, target=None)
    snapshot = read_quality_snapshot(str(tmp_path), _block_config(tmp_path))
    row = snapshot["metrics"]["type_hint_pct"]
    assert row["target"] == 80
    assert row["target_source"] == "ladder"
    assert row["met_target"] is False
    assert quality_blocks(snapshot) is True


def test_agreeing_ladder_and_artifact_render_byte_identically(tmp_path: Path) -> None:
    """AC-3: agreement (both 80, warn) renders exactly the pre-fix line.

    The pre-fix string for this fixture shape was captured before the change:
    'quality: type_hint_pct=72.3% (target 80%, warn, stage current, via
    repo-producer)' + the diff-scope unavailable line."""
    _write_ladder(tmp_path, {"type_hint_pct": 80})
    _write_artifact_direct(tmp_path, 72.3, target=80)
    cfg = _config(tmp_path, per_metric_mode={"type_hint_pct": "warn"})
    line = format_quality_snapshot(read_quality_snapshot(str(tmp_path), cfg))
    assert line == (
        "quality: type_hint_pct=72.3% (target 80%, warn, stage current, via repo-producer)\n"
        "diff-scope: unavailable (producer artifact has no surfaces model)"
    )


def test_target_disagreement_names_the_stricter_number(tmp_path: Path) -> None:
    """Ladder vs artifact disagreement: effective target wins and the line
    names which number beat which — in both directions."""
    _write_ladder(tmp_path, {"type_hint_pct": 80})
    _write_artifact_direct(tmp_path, 72.3, target=10)
    line = format_quality_snapshot(read_quality_snapshot(str(tmp_path), _block_config(tmp_path)))
    assert (
        "type_hint_pct=72.3% "
        "(target 80% (ladder beats artifact target 10%), block, stage current, "
        "via repo-producer)"
    ) in line

    from engine.quality_metrics import _snapshot_cache

    _snapshot_cache.clear()
    _write_artifact_direct(tmp_path, 72.3, target=90)
    line = format_quality_snapshot(read_quality_snapshot(str(tmp_path), _block_config(tmp_path)))
    assert (
        "type_hint_pct=72.3% "
        "(target 90% (artifact beats ladder target 80%), block, stage current, "
        "via repo-producer)"
    ) in line


def test_missing_ladder_file_degrades_with_reason(tmp_path: Path) -> None:
    """AC-4: a configured ladder that cannot be read is fail-soft, never
    silent — the artifact target stays authoritative and the snapshot carries
    the reason instead of looking identical to 'no ladder'."""
    _write_ladder(tmp_path, None)
    _write_artifact_direct(tmp_path, 72.3, target=10)
    snapshot = read_quality_snapshot(str(tmp_path), _block_config(tmp_path))
    row = snapshot["metrics"]["type_hint_pct"]
    assert row["target"] == 10  # artifact target still used
    assert row["target_source"] == "ladder unavailable: file not found"
    assert "ladder_target" not in row
    assert "artifact_target" not in row
    assert quality_blocks(snapshot) is False  # 72.3 >= 10 under the only target left


def test_non_mapping_ladder_degrades_with_reason(tmp_path: Path) -> None:
    """A ladder that is not a JSON object must not raise and must be named."""
    _write_ladder(tmp_path, "[80]")
    _write_artifact_direct(tmp_path, 72.3, target=10)
    snapshot = read_quality_snapshot(str(tmp_path), _block_config(tmp_path))
    row = snapshot["metrics"]["type_hint_pct"]
    assert row["target"] == 10
    assert row["target_source"].startswith("ladder unavailable: expected a JSON object")


def test_no_targets_source_keeps_artifact_only_row(tmp_path: Path) -> None:
    """No ladder configured: rows stay artifact-only, no provenance noise —
    the historical behaviour byte-for-byte at the dict level too."""
    _write_ladder(tmp_path, None)
    _write_artifact_direct(tmp_path, 72.3, target=10)
    snapshot = read_quality_snapshot(str(tmp_path), _block_config(tmp_path, targets_source=""))
    row = snapshot["metrics"]["type_hint_pct"]
    assert row == {
        "value": 72.3,
        "target": 10,
        "target_source": "artifact",
        "stage": "current",
        "mode": "block",
        "met_target": True,
    }
    assert snapshot["targets_source"] is None


def test_ladder_not_naming_metric_leaves_artifact_target(tmp_path: Path) -> None:
    """A ladder that omits the metric must not invent a target."""
    _write_ladder(tmp_path, {"other_pct": 80})
    _write_artifact_direct(tmp_path, 72.3, target=10)
    snapshot = read_quality_snapshot(str(tmp_path), _block_config(tmp_path))
    row = snapshot["metrics"]["type_hint_pct"]
    assert row["target"] == 10
    assert row["target_source"] == "artifact"
    assert quality_blocks(snapshot) is False


def test_cached_reread_keeps_effective_target_provenance(tmp_path: Path) -> None:
    """Semantics 3: _apply_modes re-grades the CACHED effective target — the
    ladder keeps its authority for every later surface's re-read."""

    _write_ladder(tmp_path, {"type_hint_pct": 80})
    _write_artifact_direct(tmp_path, 72.3, target=10)
    first = read_quality_snapshot(str(tmp_path), _block_config(tmp_path))
    assert first["metrics"]["type_hint_pct"]["met_target"] is False

    # A later surface with no per-metric config reads the cache and still
    # grades against the EFFECTIVE (stricter) 80, with provenance intact.
    second = read_quality_snapshot(str(tmp_path), _config(tmp_path))
    row = second["metrics"]["type_hint_pct"]
    assert row["target"] == 80
    assert row["target_source"] == "ladder+artifact"
    assert row["ladder_target"] == 80
    assert row["artifact_target"] == 10
    assert row["met_target"] is False
    assert row["mode"] == "warn"
    assert quality_blocks(second) is False  # warn mode never blocks
    # And the producer/reader did not conflate the two views.
    assert first["metrics"]["type_hint_pct"]["mode"] == "block"


# ── PERF-1: the quality producer is off the commit-blocking path ──


def test_async_run_launches_producer_without_waiting(tmp_path: Path) -> None:
    """async_run starts the producer detached and returns immediately.

    A 2s producer must not delay the read: the call returns in well under 2s
    with the honest "not yet written" unavailable reason, while the producer
    keeps running in the background.
    """
    import time

    (tmp_path / ".gitreins").mkdir(exist_ok=True)
    (tmp_path / "producer.py").write_text(
        "import json, pathlib, time\n"
        "time.sleep(2)\n"
        "pathlib.Path('.gitreins/quality.json').write_text(json.dumps({"
        "'metrics': {'type_hint_pct': {'value': 72.3, 'target': 80, 'stage': 's'}}}))\n",
        encoding="utf-8",
    )
    config = _config(tmp_path)
    start = time.monotonic()
    snapshot = read_quality_snapshot(str(tmp_path), config, async_run=True)
    elapsed = time.monotonic() - start
    assert elapsed < 2.0
    assert snapshot["status"] == "unavailable"
    assert "producer started in background" in snapshot["reason"]


def test_async_run_reads_last_completed_artifact(tmp_path: Path) -> None:
    """With a prior artifact on disk, async_run returns those numbers while a
    fresh producer runs in the background — no re-wait, but real numbers."""
    from engine.quality_metrics import _snapshot_cache

    _produce(tmp_path, metric_value=82, target=80)
    config = _config(tmp_path)
    # Produce once synchronously so a completed artifact exists on disk.
    first = read_quality_snapshot(str(tmp_path), config)
    assert first["metrics"]["type_hint_pct"]["value"] == 82
    # Simulate a fresh invocation (cache is process-local), then read async:
    # the completed artifact is served, the new producer runs in background.
    _snapshot_cache.clear()
    snapshot = read_quality_snapshot(str(tmp_path), config, async_run=True)
    assert snapshot["status"] == "available"
    assert snapshot["metrics"]["type_hint_pct"]["value"] == 82


def test_guard_quality_async_does_not_wait_on_slow_producer(tmp_path: Path) -> None:
    """GuardManager(quality_async=True) launches the producer in the background:
    a 2s producer must not delay the guard run (the commit path)."""
    import time

    (tmp_path / ".gitreins").mkdir(exist_ok=True)
    (tmp_path / "producer.py").write_text(
        "import json, pathlib, time\n"
        "time.sleep(2)\n"
        "pathlib.Path('.gitreins/quality.json').write_text(json.dumps({"
        "'metrics': {'type_hint_pct': {'value': 90, 'target': 80, 'stage': 's'}}}))\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    quality = _config(tmp_path).to_dict()
    gm = GuardManager(
        str(tmp_path),
        config={"guards": {"secrets": False, "lint": False, "tests": False}, "quality": quality},
        persist_log=False,
        quality_async=True,
    )
    start = time.monotonic()
    result = gm.run_all()
    elapsed = time.monotonic() - start
    assert elapsed < 2.0
    # The producer launched in the background: no artifact yet → unavailable,
    # which never blocks the commit.
    assert result.extra["quality_snapshot"]["status"] == "unavailable"


def test_quality_ensure_blocks_on_block_mode_miss(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The pre-push surface runs the producer to completion and exits 1 on an
    opt-in block-mode miss — the enforcement that moved off the commit path."""
    from gitreins import cli

    _produce(tmp_path, metric_value=72.3, target=80)
    quality = _config(tmp_path, per_metric_mode={"type_hint_pct": "block"}).to_dict()
    (tmp_path / ".gitreins/config.yaml").write_text(
        yaml.safe_dump({"quality": quality}), encoding="utf-8"
    )
    monkeypatch.setattr(cli, "get_workdir", lambda: str(tmp_path))
    with pytest.raises(SystemExit) as exc_info:
        cli.cmd_quality_ensure(SimpleNamespace())
    assert exc_info.value.code == 1
    out = capsys.readouterr().out
    assert "type_hint_pct=72.3%" in out


def test_quality_ensure_passes_without_block_mode_miss(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """No block-mode miss: quality-ensure reports the numbers and exits 0."""
    from gitreins import cli

    _produce(tmp_path, metric_value=82, target=80)
    quality = _config(tmp_path, per_metric_mode={"type_hint_pct": "warn"}).to_dict()
    (tmp_path / ".gitreins/config.yaml").write_text(
        yaml.safe_dump({"quality": quality}), encoding="utf-8"
    )
    monkeypatch.setattr(cli, "get_workdir", lambda: str(tmp_path))
    cli.cmd_quality_ensure(SimpleNamespace())  # must not raise
    out = capsys.readouterr().out
    assert "type_hint_pct=82%" in out
