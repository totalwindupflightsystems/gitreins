"""Hermetic tests for disposable verification worktrees."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from engine.config import load_defaults
from engine.worktree_disposable import DisposableWorktreeManager
from engine.worktree_manager import WorktreeError

CLI_SCRIPT = Path(__file__).parents[1] / "gitreins" / "cli.py"


def _git(repo: Path, *args: str, check: bool = True):
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=check
    )


@pytest.fixture
def disposable_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "main"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "GitReins Disposable Tests")
    _git(repo, "config", "user.email", "gitreins-disposable@example.invalid")
    (repo / "base.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "base.txt")
    _git(repo, "commit", "-qm", "initial")
    (repo / ".coding-hermes" / "board").mkdir(parents=True)
    return repo


def _run_cli(repo: Path, *args: str):
    return subprocess.run(
        [os.fspath(os.sys.executable), os.fspath(CLI_SCRIPT), *args],
        cwd=repo,
        env={**os.environ, "PYTHONPATH": str(CLI_SCRIPT.parents[1])},
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


def _disposable_dir(repo: Path) -> Path:
    return repo.parent / "main-wt" / ".disposable"


def test_fresh_pass_and_failure_reap_trees_and_write_evidence(disposable_repo, tmp_path):
    before = len(_git(disposable_repo, "worktree", "list").stdout.splitlines())
    evidence = tmp_path / "fresh.json"
    passed = _run_cli(
        disposable_repo,
        "worktree",
        "fresh",
        "--cmd",
        "printf hello",
        "--json",
        str(evidence),
    )
    assert passed.returncode == 0, passed.stderr
    record = json.loads(evidence.read_text(encoding="utf-8"))
    assert record["exit_code"] == 0
    assert record["output"] == "hello"
    assert not Path(record["tree"]).exists()

    failed = _run_cli(disposable_repo, "worktree", "fresh", "--cmd", "exit 7")
    assert failed.returncode == 7
    assert len(_git(disposable_repo, "worktree", "list").stdout.splitlines()) == before
    assert not _disposable_dir(disposable_repo).exists() or not any(
        _disposable_dir(disposable_repo).iterdir()
    )


def test_fresh_timeout_reaps_tree(disposable_repo):
    result = DisposableWorktreeManager(disposable_repo).run("sleep 1", timeout=0.01)
    assert result["exit_code"] == -1
    assert not Path(result["tree"]).exists()


def test_fresh_keep_is_reaped_by_worktree_clean(disposable_repo):
    kept = _run_cli(disposable_repo, "worktree", "fresh", "--cmd", "exit 3", "--keep")
    assert kept.returncode == 3
    assert _disposable_dir(disposable_repo).is_dir()
    assert any(_disposable_dir(disposable_repo).iterdir())

    cleaned = _run_cli(disposable_repo, "worktree", "clean")
    assert cleaned.returncode == 0, cleaned.stderr
    assert "disposable run" in cleaned.stdout
    assert not any(_disposable_dir(disposable_repo).iterdir())


def test_repro_reports_deterministic_failures_and_reaps(disposable_repo, tmp_path):
    evidence = tmp_path / "repro.json"
    result = _run_cli(
        disposable_repo,
        "worktree",
        "repro",
        "--cmd",
        "test -f definitely-missing-marker",
        "-k",
        "3",
        "--concurrency",
        "2",
        "--json",
        str(evidence),
    )
    assert result.returncode == 1
    report = json.loads(evidence.read_text(encoding="utf-8"))
    assert report["passes"] == 0
    assert report["failures"] == 3
    assert report["pass_rate"] == 0.0
    assert len(report["runs"]) == 3
    assert all(run["exit_code"] != 0 and not run["kept"] for run in report["runs"])
    assert not any(_disposable_dir(disposable_repo).iterdir())


def test_repro_all_pass_has_three_runs(disposable_repo):
    result = _run_cli(
        disposable_repo,
        "worktree",
        "repro",
        "--cmd",
        "test -f base.txt",
        "-k",
        "3",
    )
    assert result.returncode == 0, result.stderr
    assert "repro: 3/3 passed (pass rate 1.00)" in result.stdout


def test_repro_keep_failures_keeps_only_failed_tree(disposable_repo):
    verifier = DisposableWorktreeManager(disposable_repo)
    report = verifier.repro("test -f no-such-file", 2, keep_failures=True)
    assert report["failures"] == 2
    assert all(run["kept"] for run in report["runs"])
    assert len(verifier._load()) == 2
    verifier.reap()
    assert verifier._load() == []


def test_disk_ceiling_reaps_oldest_disposable_first(disposable_repo):
    verifier = DisposableWorktreeManager(disposable_repo)
    first = verifier.create("first", run_id="run-first", keep=True)
    (Path(first.path) / "payload.bin").write_bytes(b"a" * 600_000)
    second = verifier.create("second", run_id="run-second", keep=True)
    (Path(second.path) / "payload.bin").write_bytes(b"b" * 600_000)

    verifier.manager.worktree_disk_ceiling_mb = 1
    third = verifier.create("third", run_id="run-third", keep=True)
    assert not Path(first.path).exists()
    assert Path(second.path).exists()
    assert Path(third.path).exists()
    verifier.reap()
    assert verifier._load() == []


def test_ceiling_reap_during_create_leaves_no_ghost_registry_entry(disposable_repo):
    """WORKTREE-007: after a ceiling-driven reap the registry lists live trees only.

    `create` used to capture the registry BEFORE `enforce_disk_ceiling` ran, so
    when the ceiling reaped run-first to make room, the new record was appended
    to the stale list and the reaped run came back as a ghost entry: registered,
    counted by accounting / `--keep` reporting, but not on disk.
    """
    verifier = DisposableWorktreeManager(disposable_repo)
    first = verifier.create("first", run_id="run-first", keep=True)
    (Path(first.path) / "payload.bin").write_bytes(b"a" * 600_000)
    second = verifier.create("second", run_id="run-second", keep=True)
    (Path(second.path) / "payload.bin").write_bytes(b"b" * 600_000)

    verifier.manager.worktree_disk_ceiling_mb = 1
    third = verifier.create("third", run_id="run-third", keep=True)

    assert not Path(first.path).exists()
    assert Path(second.path).exists() and Path(third.path).exists()

    records = verifier._load()
    registered = sorted(record.run_id for record in records)
    on_disk = sorted(
        entry.name for entry in _disposable_dir(disposable_repo).iterdir() if entry.is_dir()
    )
    # The registry is the truth about what exists: no ghost, nothing missing.
    assert registered == on_disk == ["run-second", "run-third"]
    assert all(Path(record.path).exists() for record in records)


def test_disk_ceiling_failure_has_no_new_tree(disposable_repo):
    verifier = DisposableWorktreeManager(disposable_repo)
    verifier.manager.worktree_disk_ceiling_mb = 1
    (disposable_repo / "large.bin").write_bytes(b"x" * (2 * 1024 * 1024))
    with pytest.raises(WorktreeError, match="disk ceiling 1 MB"):
        verifier.create("too-large", run_id="run-too-large")
    assert not (_disposable_dir(disposable_repo) / "run-too-large").exists()
    assert verifier._load() == []


def test_disk_ceiling_config_is_loaded_and_validated(disposable_repo):
    config = disposable_repo / ".gitreins" / "config.yaml"
    config.parent.mkdir(parents=True)
    config.write_text("worktree_fleet:\n  disk_ceiling_mb: 12\n", encoding="utf-8")
    assert load_defaults(str(disposable_repo)).worktree_disk_ceiling_mb == 12
    config.write_text("worktree_fleet:\n  disk_ceiling_mb: true\n", encoding="utf-8")
    with pytest.raises(ValueError, match="disk_ceiling_mb"):
        load_defaults(str(disposable_repo))


def test_dogfood_skip_judge_runs_four_steps_and_reaps(disposable_repo, tmp_path):
    evidence = tmp_path / "dogfood.json"
    result = _run_cli(
        disposable_repo,
        "worktree",
        "dogfood",
        "--skip-judge",
        "--test-command",
        "true",
        "--json",
        str(evidence),
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(evidence.read_text(encoding="utf-8"))
    assert [step["name"] for step in report["steps"]] == ["init", "task", "guard", "judge"]
    assert report["judge"]["status"] == "skipped"
    assert not Path(report["tree"]).exists()
    assert not any(_disposable_dir(disposable_repo).iterdir())


def test_dogfood_keep_retains_tree(disposable_repo):
    result = _run_cli(
        disposable_repo,
        "worktree",
        "dogfood",
        "--skip-judge",
        "--test-command",
        "true",
        "--keep",
    )
    assert result.returncode == 0, result.stderr
    verifier = DisposableWorktreeManager(disposable_repo)
    assert verifier._load()
    verifier.reap()
    assert verifier._load() == []


# ── INT-CI-11: the reap is idempotent and serialized ─────────────────────────
#
# CI-only failure on a board-only commit: `worktree repro -k 3` exited 2 with
# `could not reap disposable worktree .../.disposable/run-6ef7c5c0...: fatal:
# Invalid path '<repo>/.git/worktrees/run-c05c99c2...': No such file or
# directory` (the manager reported an infrastructure failure, assert 2 == 0)
# and then passed on a rerun.  A parallel repro farm reaps `k` trees at once,
# so one run's `git worktree prune` can delete another run's admin metadata
# (<git-common-dir>/worktrees/<run-id>, or even the whole worktrees/ dir via
# delete_worktrees_dir_if_empty) while that run is resolving its own admin
# path — git then aborts even though the tree is already gone.


def test_reap_tolerates_admin_metadata_dropped_by_a_concurrent_reap(disposable_repo):
    """A tree git no longer tracks is already reaped — reap it, do not raise."""
    verifier = DisposableWorktreeManager(disposable_repo)
    record = verifier.create("race", run_id="run-c05c99c2a1b2")
    tree = Path(record.path)
    admin = disposable_repo / ".git" / "worktrees" / record.run_id
    assert admin.is_dir()

    # What the concurrent reap does: the admin metadata disappears while the
    # worktree directory is still on disk.
    shutil.rmtree(admin)
    assert tree.is_dir()
    assert record.run_id not in _git(disposable_repo, "worktree", "list").stdout

    assert verifier.reap() == [record.run_id]
    assert not tree.exists()
    assert verifier._load() == []
    assert not (_disposable_dir(disposable_repo) / record.run_id).exists()


def test_reap_still_fails_loudly_while_git_tracks_the_tree(disposable_repo, monkeypatch):
    """The tolerance is not a blanket ignore: a registered tree that cannot be
    reaped still raises, and nothing is silently forgotten."""
    import engine.worktree_disposable as disposable_mod

    verifier = DisposableWorktreeManager(disposable_repo)
    record = verifier.create("stuck", run_id="run-stillregistered")
    tree = Path(record.path)

    real_git = disposable_mod._git

    def failing_git(workdir, *args, check=True):
        if args[:2] == ("worktree", "remove"):
            return subprocess.CompletedProcess(
                list(args),
                1,
                "",
                "fatal: Invalid path "
                f"'{disposable_repo}/.git/worktrees/{record.run_id}': "
                "No such file or directory",
            )
        return real_git(workdir, *args, check=check)

    monkeypatch.setattr(disposable_mod, "_git", failing_git)
    with pytest.raises(WorktreeError, match="could not reap disposable worktree"):
        verifier.reap()
    assert tree.is_dir()
    assert [item.run_id for item in verifier._load()] == [record.run_id]


def test_parallel_repro_reaps_are_serialized(disposable_repo, monkeypatch):
    """k concurrent reaps never run git metadata mutations at the same time."""
    import engine.worktree_disposable as disposable_mod

    real_remove = disposable_mod._remove_disposable_tree
    live = {"now": 0, "max": 0}

    def spy(main_root, path):
        live["now"] += 1
        live["max"] = max(live["max"], live["now"])
        try:
            return real_remove(main_root, path)
        finally:
            live["now"] -= 1

    monkeypatch.setattr(disposable_mod, "_remove_disposable_tree", spy)
    report = DisposableWorktreeManager(disposable_repo).repro("test -f base.txt", 4, concurrency=4)
    assert report["passes"] == 4
    assert report["failures"] == 0
    assert live["max"] == 1, f"reaps were not serialized: {live}"
    assert not any(_disposable_dir(disposable_repo).iterdir())
