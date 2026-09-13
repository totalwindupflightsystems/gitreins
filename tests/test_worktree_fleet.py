"""Hermetic tests for the bounded parallel worktree fleet."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from engine.config import load_defaults
from engine.worktree_fleet import FleetLane, FleetValidationError, WorktreeFleet
from engine.worktree_manager import WorktreeError, WorktreeManager

CLI_SCRIPT = Path(__file__).parents[1] / "gitreins" / "cli.py"


def _git(repo: Path, *args: str, check: bool = True):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=check,
    )


@pytest.fixture
def fleet_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "main"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "GitReins Fleet Tests")
    _git(repo, "config", "user.email", "gitreins-fleet@example.invalid")
    (repo / "base.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "base.txt")
    _git(repo, "commit", "-qm", "initial")
    (repo / ".coding-hermes" / "board").mkdir(parents=True)
    return repo


def _command(events: Path, output_name: str, delay: float = 0.15):
    code = (
        "import os,sys,time; "
        "events,out,delay=sys.argv[1],sys.argv[2],float(sys.argv[3]); "
        "open(events,'a').write('start,'+os.path.basename(os.getcwd())+'\\n'); "
        "time.sleep(delay); "
        "open(events,'a').write('end,'+os.path.basename(os.getcwd())+'\\n'); "
        "open(out,'w').write(os.getcwd())"
    )
    return (sys.executable, "-c", code, str(events), output_name, str(delay))


def test_fleet_is_bounded_isolated_and_registry_truthful(fleet_repo: Path, tmp_path: Path):
    events = tmp_path / "events.log"
    source_venv = fleet_repo / "shared-env"
    source_venv.mkdir()
    config = fleet_repo / ".gitreins" / "config.yaml"
    config.parent.mkdir(exist_ok=True)
    config.write_text(
        "worktree_fleet:\n  max_concurrent_worktrees: 2\n  venv:\n    source: shared-env\n    name: .fleet-venv\n",
        encoding="utf-8",
    )

    fleet = WorktreeFleet(fleet_repo)
    lanes = [
        FleetLane("FLEET-3", _command(events, "three.txt"), priority=30),
        FleetLane("FLEET-1", _command(events, "one.txt"), priority=10),
        FleetLane("FLEET-2", _command(events, "two.txt"), priority=20),
    ]
    report = fleet.run(lanes, tick="tick-1")

    assert report["cap"] == 2
    assert [lane["task_id"] for lane in report["lanes"]] == ["FLEET-1", "FLEET-2", "FLEET-3"]
    assert all(lane["state"] == "completed" and lane["passed"] for lane in report["lanes"])
    records = {record.task_id: record for record in fleet.manager.list_records()}
    assert set(records) == {"FLEET-1", "FLEET-2", "FLEET-3"}
    assert all(record.state == "completed" for record in records.values())
    assert all(record.lane_result and record.lane_result["passed"] for record in records.values())
    assert all((Path(record.path) / ".fleet-venv").is_symlink() for record in records.values())
    assert all(
        (Path(records[task_id].path) / filename).read_text(encoding="utf-8")
        == str(Path(records[task_id].path))
        for task_id, filename in (
            ("FLEET-1", "one.txt"),
            ("FLEET-2", "two.txt"),
            ("FLEET-3", "three.txt"),
        )
    )

    lines = events.read_text(encoding="utf-8").splitlines()
    assert sum(line.startswith("start,") for line in lines) == 3
    assert sum(line.startswith("end,") for line in lines) == 3
    first_end = next(index for index, line in enumerate(lines) if line.startswith("end,"))
    assert any(line.startswith("start,") for line in lines[:first_end])
    assert len({line.split(",", 1)[1] for line in lines if "," in line}) == 3

    registry = json.loads((fleet_repo / ".gitreins" / "worktrees.json").read_text(encoding="utf-8"))
    assert {entry["task_id"] for entry in registry["worktrees"]} == set(records)
    board_events = (fleet_repo / ".coding-hermes" / "board" / "events.jsonl").read_text()
    assert board_events.count("worktree_lane_completed") == 3


def test_fleet_failure_retains_tree_and_evidence(fleet_repo: Path, tmp_path: Path):
    code = (sys.executable, "-c", "import sys; print('known failure'); sys.exit(7)")
    report = WorktreeFleet(fleet_repo).run([FleetLane("FLEET-FAIL", code)])

    lane = report["lanes"][0]
    assert lane["state"] == "failed"
    assert lane["exit_code"] == 7
    assert "known failure" in lane["output"]
    record = WorktreeManager(fleet_repo).list_records()[0]
    assert record.state == "failed"
    assert record.exit_code == 7
    assert Path(record.path).is_dir()
    assert _git(fleet_repo, "rev-parse", "--verify", record.branch).returncode == 0


def test_fleet_runs_guard_and_judge_phases_without_shell(fleet_repo: Path):
    phases = (sys.executable, "-c", "print('phase')")
    report = WorktreeFleet(fleet_repo).run(
        [FleetLane("FLEET-PHASE", phases, guard=phases, judge=phases)]
    )

    lane = report["lanes"][0]
    assert lane["state"] == "completed"
    assert [stage["phase"] for stage in lane["stages"]] == ["running", "guarding", "judging"]
    record = WorktreeManager(fleet_repo).list_records()[0]
    assert record.lane_phase == "completed"
    assert [stage["phase"] for stage in record.lane_result["stages"]] == [
        "running",
        "guarding",
        "judging",
    ]


def test_fleet_merges_in_priority_order_with_serialized_mutations(fleet_repo: Path):
    config = fleet_repo / ".gitreins" / "config.yaml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text("history:\n  storage: filesystem\n", encoding="utf-8")
    _git(fleet_repo, "add", ".gitreins/config.yaml")
    _git(fleet_repo, "commit", "-qm", "fleet config")
    commands = []
    for task_id, filename in (("MERGE-2", "two.txt"), ("MERGE-1", "one.txt")):
        code = (
            "from pathlib import Path; import subprocess,sys; "
            f"Path('{filename}').write_text('{task_id}\\n'); "
            f"subprocess.run(['git','add','{filename}'],check=True); "
            f"subprocess.run(['git','commit','-qm','{task_id}'])"
        )
        commands.append(
            FleetLane(
                task_id, (sys.executable, "-c", code), priority=2 if task_id.endswith("2") else 1
            )
        )

    manager = WorktreeManager(fleet_repo)
    fleet = WorktreeFleet(fleet_repo, manager=manager)
    report = fleet.run(
        commands,
        merge=True,
        force_merge=True,
        merge_actor="fleet-test",
    )

    assert report["merge_order"] == ["MERGE-1", "MERGE-2"], report["merge_errors"]
    assert report["merge_errors"] == {}
    assert (fleet_repo / "one.txt").read_text(encoding="utf-8") == "MERGE-1\n"
    assert (fleet_repo / "two.txt").read_text(encoding="utf-8") == "MERGE-2\n"
    assert WorktreeManager(fleet_repo).list_records() == []
    events = (fleet_repo / ".coding-hermes" / "board" / "events.jsonl").read_text().splitlines()
    merge_events = [json.loads(line) for line in events if "worktree_merged" in line]
    assert [event["task_id"] for event in merge_events] == ["MERGE-1", "MERGE-2"]


def test_venv_collision_is_refused_without_creating_tree(fleet_repo: Path):
    source = fleet_repo / "shared-env"
    source.mkdir()
    destination = fleet_repo.parent / "main-wt" / "COLLIDE"
    destination.mkdir(parents=True)
    (destination / ".venv").write_text("real file", encoding="utf-8")
    manager = WorktreeManager(fleet_repo, venv_source="shared-env")

    with pytest.raises(WorktreeError, match="not empty"):
        manager.create("COLLIDE")
    registry = json.loads((fleet_repo / ".gitreins" / "worktrees.json").read_text(encoding="utf-8"))
    assert registry["worktrees"] == []
    assert not _git(
        fleet_repo, "show-ref", "--verify", "refs/heads/gitreins/task/COLLIDE", check=False
    ).stdout


def test_missing_venv_source_is_optional(fleet_repo: Path):
    manager = WorktreeManager(fleet_repo, venv_source="does-not-exist")
    record, _ = manager.create("NO-VENV")
    assert not (Path(record.path) / ".venv").exists()


def test_fleet_config_cap_override_and_validation(fleet_repo: Path):
    config = fleet_repo / ".gitreins" / "config.yaml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text("worktree_fleet:\n  max_concurrent_worktrees: 3\n", encoding="utf-8")
    assert load_defaults(str(fleet_repo)).max_concurrent_worktrees == 3
    assert WorktreeFleet(fleet_repo).max_concurrent_worktrees == 3

    for value in (0, -1, True, 1.5):
        config.write_text(
            f"worktree_fleet:\n  max_concurrent_worktrees: {value!r}\n", encoding="utf-8"
        )
        with pytest.raises(ValueError, match="max_concurrent_worktrees"):
            load_defaults(str(fleet_repo))


def test_fleet_cli_loads_manifest_and_reports_cap(fleet_repo: Path, tmp_path: Path):
    manifest = tmp_path / "lanes.json"
    manifest.write_text(
        json.dumps(
            {"lanes": [{"task_id": "CLI-FLEET", "command": [sys.executable, "-c", "print('cli')"]}]}
        ),
        encoding="utf-8",
    )
    result = subprocess.run(
        [sys.executable, str(CLI_SCRIPT), "worktree", "fleet", str(manifest)],
        cwd=fleet_repo,
        env={**os.environ, "PYTHONPATH": str(CLI_SCRIPT.parents[1])},
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["cap"] == 2
    assert report["lanes"][0]["state"] == "completed"
    listed = subprocess.run(
        [sys.executable, str(CLI_SCRIPT), "worktree", "list"],
        cwd=fleet_repo,
        env={**os.environ, "PYTHONPATH": str(CLI_SCRIPT.parents[1])},
        capture_output=True,
        text=True,
        check=False,
    )
    assert "Fleet cap: 2" in listed.stdout
    assert "CLI-FLEET" in listed.stdout and "completed" in listed.stdout


def test_empty_fleet_is_a_loud_noop(fleet_repo: Path):
    with pytest.raises(FleetValidationError, match="at least one lane"):
        WorktreeFleet(fleet_repo).run([])
    assert not (fleet_repo / ".gitreins" / "worktrees.json").exists()
