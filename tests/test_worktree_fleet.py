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


def test_fleet_rerun_over_failed_lane_refuses_and_the_hint_resolves_it(fleet_repo: Path):
    """DF-GITREINS-POC-50 AC3: a failed lane's stale tree is never silently reused.

    The re-run must fail LOUD (the old behaviour reused the tree at the OLD
    HEAD, so the next lane ran against a pre-feature tree), and the message
    must name the command that actually clears the lane.
    """
    code = (sys.executable, "-c", "import sys; print('lane boom'); sys.exit(7)")
    first = WorktreeFleet(fleet_repo).run([FleetLane("FLEET-RERUN", code)])
    assert first["lanes"][0]["state"] == "failed"

    with pytest.raises(WorktreeError) as excinfo:
        WorktreeFleet(fleet_repo).run([FleetLane("FLEET-RERUN", code)])

    message = str(excinfo.value)
    assert "FAILED worktree" in message
    assert "gitreins worktree clean" in message

    # Following the hint works end to end: plain clean reaps the failed lane,
    # and the re-run then builds a fresh tree instead of reusing the stale one.
    manager = WorktreeManager(fleet_repo)
    assert manager.clean()["removed"] == ["FLEET-RERUN"]
    rerun = WorktreeFleet(fleet_repo, manager=manager).run([FleetLane("FLEET-RERUN", code)])
    assert rerun["lanes"][0]["state"] == "failed"
    assert Path(rerun["lanes"][0]["worktree"]).is_dir()


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


# ── DF-GITREINS-POC-47 — `worktree fleet --merge` on a stock consumer install ──

#: Runtime files an ordinary run leaves in the canonical main checkout (the
#: disposable verifier registry + lock, the task store's flock sidecar, and the
#: worktree registry + its lock).  None of them is user work.
RUNTIME_ARTIFACTS_IN_MAIN = (
    ".gitreins/worktrees.json",
    ".gitreins/worktrees.lock",
    ".gitreins/disposable.json",
    ".gitreins/disposable.lock",
    ".gitreins/tasks.yaml.lock",
)

#: A lane that commits its one file and then leaves the regenerated venv the
#: configured guard (`uv run pytest`) produces inside the tree.  The tree is
#: otherwise clean — but `.venv` is untracked, and a consumer whose .gitignore
#: predates the venv cannot commit it.
LANE_WITH_REGENERATED_VENV = (
    "from pathlib import Path; import subprocess;"
    "Path('.venv').mkdir(exist_ok=True);"
    "Path('.venv', 'python').write_text('');"
    "Path('lane.txt').write_text('lane\\n');"
    "subprocess.run(['git', 'add', 'lane.txt'], check=True);"
    "subprocess.run(['git', 'commit', '-qm', 'lane work'], check=True)"
)


def _write_runtime_artifacts_in_main(repo: Path) -> None:
    for name in RUNTIME_ARTIFACTS_IN_MAIN:
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")


def test_fleet_merge_passes_when_only_runtime_artifacts_are_present(fleet_repo: Path):
    """The whole fleet was unmergeable on a stock install (DF-GITREINS-POC-47).

    Nothing in this setup is user work: canonical main carries only the runtime
    files an ordinary run writes, and the lane tree carries the regenerated
    venv.  Every one of them used to read as dirt, so `worktree fleet --merge`
    refused each lane with "canonical main has uncommitted changes" /
    "task worktree has uncommitted changes" — permanently, on a fresh consumer
    repo that had not yet ignored them.
    """
    _write_runtime_artifacts_in_main(fleet_repo)
    fleet = WorktreeFleet(fleet_repo)

    report = fleet.run(
        [FleetLane("MERGE-RUNTIME", (sys.executable, "-c", LANE_WITH_REGENERATED_VENV))],
        merge=True,
        force_merge=True,
        merge_actor="fleet-test",
    )

    assert report["merge_errors"] == {}, report["merge_errors"]
    assert report["merge_order"] == ["MERGE-RUNTIME"]
    assert (fleet_repo / "lane.txt").read_text(encoding="utf-8") == "lane\n"
    assert WorktreeManager(fleet_repo).list_records() == []


def test_fleet_merge_still_refuses_real_dirt_beside_runtime_artifacts(fleet_repo: Path):
    """The exemption is narrow: one stray edit still stops the merge."""
    _write_runtime_artifacts_in_main(fleet_repo)
    code = LANE_WITH_REGENERATED_VENV + ";Path('stray.txt').write_text('uncommitted')"
    fleet = WorktreeFleet(fleet_repo)

    report = fleet.run(
        [FleetLane("MERGE-STRAY", (sys.executable, "-c", code))],
        merge=True,
        force_merge=True,
        merge_actor="fleet-test",
    )

    assert report["merge_order"] == []
    assert "uncommitted changes" in report["merge_errors"]["MERGE-STRAY"]
    assert not (fleet_repo / "lane.txt").exists()
    assert Path(fleet_repo.parent / "main-wt" / "MERGE-STRAY").is_dir()


# ── DF-GITREINS-POC-48 — the judge phase and the merge gate ──────────────
#
# The row's three repros, each as an executable claim:
#   (a) a lane's judge phase could not name the lane's task at all —
#       `.gitreins/tasks.yaml` is per-checkout and untracked, so a fresh lane
#       tree has no store and `gitreins judge <id>` answered "Task not found";
#   (b) an ephemeral judge verdict could never reach the merge gate (nothing is
#       persisted);
#   (c) a judge-failed lane reported `error=None` and no merge error, so the
#       refusal reason was dropped.


#: A lane phase argv that runs *code* in the lane tree.  Engine imports need the
#: repo root on PYTHONPATH (the tests that use them set it).
def _lane_phase(code: str) -> tuple[str, ...]:
    return (sys.executable, "-c", code)


#: Stand-in for a lane's judge phase: write the merge-gate verdict document the
#: way the real CLI writes it — `engine.persist.build_verdict_data` (the shared
#: payload builder) into `engine.worktree_manager.write_disk_verdict` (the same
#: writer, and the same path constant, the gate reads).
JUDGE_PHASE_WRITES_THE_GATE_VERDICT = """
import sys
from pathlib import Path

src, task_id = sys.argv[1], sys.argv[2]
sys.path.insert(0, src)

from engine.persist import build_verdict_data
from engine.worktree_manager import write_disk_verdict

tree = Path.cwd()


class Task:
    id = task_id
    title = "Serve GET /status"
    criteria = ["GET /status returns 200"]


class Result:
    passed = True
    summary = "lane judged PASS"
    verdict = None
    pipeline_result = None


path = write_disk_verdict(tree, build_verdict_data(str(tree), Task(), Result()))
print("merge-gate verdict:", path)
"""


def test_fleet_seeds_the_lane_task_for_the_judge_phase(
    fleet_repo: Path, monkeypatch: pytest.MonkeyPatch
):
    """AC-1 (a): a lane's judge phase can name the lane's own task.

    The README's manifest pattern (`["gitreins", "judge", "API-1"]` in the same
    worktree the command ran in) is unreachable without this: the task store is
    per-checkout and gitignored, so the lane starts empty.  The fleet copies the
    task from canonical main before the judge phase runs — verbatim, so nothing
    is invented — and leaves the main store untouched.
    """
    from engine.task_manager import TaskManager

    monkeypatch.setenv("PYTHONPATH", str(CLI_SCRIPT.parents[1]))
    TaskManager(str(fleet_repo)).create(
        "SEED-1", "Serve GET /status", ["GET /status returns 200", "A test covers the endpoint"]
    )
    main_store = (fleet_repo / ".gitreins" / "tasks.yaml").read_bytes()

    judge = _lane_phase(
        "from engine.task_manager import TaskManager; import sys; "
        "task = TaskManager('.').get('SEED-1'); "
        "sys.exit(0 if task and task.title == 'Serve GET /status' "
        "and task.criteria == ['GET /status returns 200', 'A test covers the endpoint'] else 3)"
    )
    report = WorktreeFleet(fleet_repo).run([FleetLane("SEED-1", _lane_phase("pass"), judge=judge)])

    lane = report["lanes"][0]
    assert lane["state"] == "completed", lane
    seeded = TaskManager(lane["worktree"]).get("SEED-1")
    assert seeded is not None, "the judge phase ran against a lane with no task store"
    assert seeded.criteria == ["GET /status returns 200", "A test covers the endpoint"]
    assert (fleet_repo / ".gitreins" / "tasks.yaml").read_bytes() == main_store
    board = (fleet_repo / ".coding-hermes" / "board" / "events.jsonl").read_text()
    assert "worktree_lane_task_seeded" in board


def test_fleet_reports_why_a_failed_judge_phase_blocks_the_merge(
    fleet_repo: Path, monkeypatch: pytest.MonkeyPatch
):
    """AC-1 (c): a judge-failed lane carries WHY, in the lane result itself.

    The row's repro 3: state=failed / passed=false with `error=None` and no
    merge error, so a tick report could not distinguish "the judge failed, so no
    verdict exists and --merge will refuse this lane" from a merge refusal.
    """
    monkeypatch.setenv("PYTHONPATH", str(CLI_SCRIPT.parents[1]))
    judge = _lane_phase(
        "import sys; "
        "print('Stage tier2: FAIL'); "
        "print('  FAIL GET /status returns 200: endpoint missing'); "
        "print('Overall: FAIL'); "
        "sys.exit(1)"
    )
    report = WorktreeFleet(fleet_repo).run(
        [FleetLane("JUDGE-FAIL", _lane_phase("pass"), judge=judge)], merge=True
    )

    lane = report["lanes"][0]
    assert lane["state"] == "failed" and lane["passed"] is False
    error = lane["error"]
    assert "judging phase failed" in error
    assert "no PASS verdict" in error
    assert "endpoint missing" in error, error  # the criterion the judge printed
    assert report["merge_order"] == []
    assert report["merge_errors"]["JUDGE-FAIL"].startswith("not merged: judging phase failed")

    record = WorktreeManager(fleet_repo).list_records()[0]
    assert record.error == error
    assert record.lane_result["error"] == error


def test_fleet_judge_phase_naming_an_unknown_task_says_where_tasks_live(
    fleet_repo: Path, monkeypatch: pytest.MonkeyPatch
):
    """AC-1 (c), the (a)-shaped failure: no task anywhere names the fix."""
    monkeypatch.setenv("PYTHONPATH", str(CLI_SCRIPT.parents[1]))
    judge = _lane_phase("import sys; print('Task not found: GHOST-1'); sys.exit(1)")

    report = WorktreeFleet(fleet_repo).run([FleetLane("GHOST-1", _lane_phase("pass"), judge=judge)])

    error = report["lanes"][0]["error"]
    assert "Task not found" in error
    assert "per-checkout and untracked" in error
    assert "canonical checkout" in error


def test_merge_gate_accepts_the_verdict_a_lane_wrote_in_its_own_tree(
    fleet_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """AC-2: a verdict produced in the lane tree satisfies the `--merge` gate.

    The end-to-end shape the row calls unreachable: the task lives in canonical
    main, the fleet seeds it into the lane, the lane's judge phase writes the
    merge-gate verdict document in the lane tree, and `--merge` (WITHOUT
    `--force`) applies the lane.  Two runtime artifacts an ordinary lane now
    leaves behind — the seeded task store and the verdict document — must both
    read as harness state rather than as uncommitted work, or the gate would
    refuse the very lane they describe.
    """
    from engine.task_manager import TaskManager

    monkeypatch.setenv("PYTHONPATH", str(CLI_SCRIPT.parents[1]))
    TaskManager(str(fleet_repo)).create("GATE-1", "Serve GET /status", ["GET /status returns 200"])
    judge_script = tmp_path / "judge_lane.py"
    judge_script.write_text(JUDGE_PHASE_WRITES_THE_GATE_VERDICT, encoding="utf-8")
    lane_command = _lane_phase(
        "from pathlib import Path; import subprocess; "
        "Path('status.txt').write_text('ok'); "
        "subprocess.run(['git', 'add', 'status.txt'], check=True); "
        "subprocess.run(['git', 'commit', '-qm', 'GATE-1 status endpoint'], check=True)"
    )

    report = WorktreeFleet(fleet_repo).run(
        [
            FleetLane(
                "GATE-1",
                lane_command,
                judge=(
                    sys.executable,
                    str(judge_script),
                    str(CLI_SCRIPT.parents[1]),
                    "GATE-1",
                ),
            )
        ],
        merge=True,
    )

    assert report["merge_errors"] == {}, report["merge_errors"]
    assert report["merge_order"] == ["GATE-1"]
    assert (fleet_repo / "status.txt").read_text(encoding="utf-8") == "ok"
    assert WorktreeManager(fleet_repo).list_records() == []


def test_fleet_lane_can_judge_ephemerally_and_still_merge(
    fleet_repo: Path, monkeypatch: pytest.MonkeyPatch
):
    """AC-2, the ephemeral route: `--persist-verdict` makes an ephemeral PASS usable.

    Repro 2 from the row: `judge --ephemeral` writes nothing (EVID-003) and the
    merge gate accepts nothing without a verdict, so the two were irreconcilable.
    Here the lane's judge phase IS the real CLI in ephemeral mode with
    `--persist-verdict`, and the lane merges: no history entry, no task store
    (there is no task at all), just the one document the gate reads.
    """
    source_root = CLI_SCRIPT.parents[1]
    monkeypatch.setenv("PYTHONPATH", str(source_root))
    monkeypatch.setenv(
        "GITREINS_MOCK_LLM_RESPONSE",
        json.dumps(
            {
                "content": json.dumps(
                    {
                        "verdict": "COMPLETE",
                        "items": [
                            {
                                "criterion": "GET /status returns 200",
                                "status": "PASS",
                                "detail": "app.status() returns ok",
                            }
                        ],
                        "summary": "criterion satisfied",
                    }
                )
            }
        ),
    )
    # A Python tree, so Tier 1 plans lint AND tests rather than degrading to
    # secrets-only (a degraded PASS is refused by the gate on purpose), with the
    # toolchain this test actually has: lint off, a test command that needs no
    # project setup.
    (fleet_repo / "app.py").write_text("def status():\n    return 'ok'\n", encoding="utf-8")
    config = fleet_repo / ".gitreins" / "config.yaml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        f'guards:\n  lint: false\n  test_command: "{sys.executable} -c pass"\n'
        "defaults:\n  check_for_updates: false\n",
        encoding="utf-8",
    )
    _git(fleet_repo, "add", "app.py", ".gitreins/config.yaml")
    _git(fleet_repo, "commit", "-qm", "python tree + fleet config")

    lane_command = _lane_phase(
        "from pathlib import Path; import subprocess; "
        "Path('status.txt').write_text('ok'); "
        "subprocess.run(['git', 'add', 'status.txt'], check=True); "
        "subprocess.run(['git', 'commit', '-qm', 'API-1 status endpoint'], check=True)"
    )
    judge = (
        sys.executable,
        str(CLI_SCRIPT),
        "judge",
        "API-1",
        "--ephemeral",
        "--title",
        "Serve GET /status",
        "--criterion",
        "GET /status returns 200",
        "--persist-verdict",
    )

    report = WorktreeFleet(fleet_repo).run(
        [FleetLane("API-1", lane_command, judge=judge)], merge=True
    )

    assert report["merge_errors"] == {}, report["merge_errors"]
    assert report["merge_order"] == ["API-1"]
    assert (fleet_repo / "status.txt").read_text(encoding="utf-8") == "ok"
    # The ephemeral route never opened a task store, in main or in the lane.
    assert not (fleet_repo / ".gitreins" / "tasks.yaml").exists()
    assert WorktreeManager(fleet_repo).list_records() == []


def test_merge_gate_reads_the_disk_verdict_for_the_commit_it_grades(fleet_repo: Path):
    """AC-2's two-sided control: the disk document is read, and scoped.

    One-sided, "the gate accepted the document" could equally mean "the gate
    never looked" — a history verdict or no verdict at all would answer the
    same.  So the same file is tried twice: for the commit the branch actually
    points at (the merge must proceed on THAT evidence alone) and for an older
    commit (the merge must refuse).
    """
    from engine.worktree_manager import write_disk_verdict

    manager = WorktreeManager(fleet_repo)
    record, _created = manager.create("GATE-DISK")
    tree = Path(record.path)
    (tree / "lane.txt").write_text("lane\n", encoding="utf-8")
    _git(tree, "add", "lane.txt")
    _git(tree, "commit", "-qm", "lane work")
    older_commit = _git(tree, "rev-parse", "HEAD").stdout.strip()
    _git(tree, "commit", "-q", "--allow-empty", "-m", "tip moves on")
    tip = _git(tree, "rev-parse", "HEAD").stdout.strip()

    def _verdict(commit: str) -> dict:
        return {
            "task_id": record.task_id,
            "passed": True,
            "worktree": str(tree.resolve()),
            "branch": record.branch,
            "commit": commit,
        }

    write_disk_verdict(tree, _verdict(older_commit))
    with pytest.raises(WorktreeError, match="no PASS verdict"):
        manager.merge(record.task_id)

    write_disk_verdict(tree, _verdict(tip))
    merged = manager.merge(record.task_id)
    assert merged["source_commit"] == tip
    assert _git(fleet_repo, "rev-parse", "HEAD").stdout.strip() == tip
