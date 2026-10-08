"""Hermetic tests for disposable verification worktrees."""

from __future__ import annotations

import argparse
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
    # DF-GITREINS-POC-73: the human line must not present the skipped judge as
    # a failure ("3/4 steps passed") — skipped steps are named, not counted as
    # not-passed.
    assert "judge skipped" in result.stdout
    assert "--skip-judge" in result.stdout
    assert "3/4 steps passed" not in result.stdout


def test_dogfood_human_summary_separates_skipped_from_passed(disposable_repo, tmp_path):
    """DF-GITREINS-POC-73 — the summary is also an API.

    A CI consumer keying on the old ``3/4 steps passed`` line graded a
    deterministic judge skip as a failure. The renderer now reports the
    executed count separately from the skipped remainder and suffixes each
    skipped step with its recorded reason, mirroring the honest JSON.
    """
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

    passed = sum(step["status"] == "passed" for step in report["steps"])
    failed = sum(step["status"] == "failed" for step in report["steps"])
    skipped = [step for step in report["steps"] if step["status"] == "skipped"]
    # Contract: counts that separate skipped from failed, plus the skip reason.
    assert "dogfood: 3 passed, 0 failed, 1 skipped" in result.stdout
    for step in skipped:
        assert f"{step['name']} skipped ({step['reason']})" in result.stdout
    # The old misleading rendering is gone, and a clean skip stays exit 0.
    assert "3/4 steps passed" not in result.stdout
    assert passed == 3 and failed == 0


def test_format_dogfood_summary_renders_each_status_honestly():
    """Unit contract for the pure renderer: passed / failed / skipped each get
    their own count, skipped steps carry their reason, and no rendering can
    re-introduce the misleading ``N/M steps passed`` shape."""
    from gitreins import cli as cli_mod

    # Skip case: judge skipped with its reason — must not read as a failure.
    skipped_report = {
        "steps": [
            {"name": "init", "status": "passed", "reason": ""},
            {"name": "task", "status": "passed", "reason": ""},
            {"name": "guard", "status": "passed", "reason": ""},
            {"name": "judge", "status": "skipped", "reason": "--skip-judge"},
        ],
        "judge": {"status": "skipped", "reason": "--skip-judge"},
        "exit_code": 0,
    }
    summary = cli_mod._format_dogfood_summary(skipped_report)
    assert summary == ("dogfood: 3 passed, 0 failed, 1 skipped (judge skipped (--skip-judge))")
    assert "3/4" not in summary

    # Full-execution success: identical shape, zero skipped, no skip suffix.
    passed_report = {
        "steps": [
            {"name": "init", "status": "passed", "reason": ""},
            {"name": "task", "status": "passed", "reason": ""},
            {"name": "guard", "status": "passed", "reason": ""},
            {"name": "judge", "status": "passed", "reason": ""},
        ],
        "judge": {"status": "passed", "reason": ""},
        "exit_code": 0,
    }
    assert (
        cli_mod._format_dogfood_summary(passed_report) == "dogfood: 4 passed, 0 failed, 0 skipped"
    )

    # Real failure stays visibly failed and exit-code-truthful.
    failed_report = {
        "steps": [
            {"name": "init", "status": "passed", "reason": ""},
            {"name": "task", "status": "failed", "reason": ""},
            {"name": "judge", "status": "skipped", "reason": "previous dogfood step failed"},
        ],
        "judge": {"status": "skipped", "reason": "previous dogfood step failed"},
        "exit_code": 1,
    }
    failed_summary = cli_mod._format_dogfood_summary(failed_report)
    assert "1 failed" in failed_summary
    assert "judge skipped (previous dogfood step failed)" in failed_summary
    assert "skipped (previous dogfood step failed)" in failed_summary


def test_dogfood_failure_keeps_failed_count_visible(disposable_repo, monkeypatch, capsys):
    """DF-GITREINS-POC-73 control — a genuinely failed step stays failed.

    Drives the real command function in-process with only the guard CLI
    invocation stubbed to exit 3 (in a disposable tree every lane that could
    fail naturally is diff-scoped against a clean HEAD and skips instead).
    The run must exit 1, count the guard failure separately, and name both
    the failure and the judge skip with its recorded reason — the honest-skip
    rendering must not soften real failures. (--skip-judge's own reason wins
    over the failure reason by the documented branch order.)
    """
    import engine.worktree_disposable as disposable_mod
    from gitreins import cli as cli_mod

    real_run = disposable_mod.subprocess.run

    def failing_guard_run(cmd, **kwargs):
        if cmd[-1] == "guard":
            return subprocess.CompletedProcess(cmd, 3, "", "guard exploded")
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(disposable_mod.subprocess, "run", failing_guard_run)
    monkeypatch.setattr(cli_mod, "get_workdir", lambda: str(disposable_repo))

    args = argparse.Namespace(
        keep=False, skip_judge=True, test_command="true", timeout=None, json_path=None
    )
    with pytest.raises(SystemExit) as excinfo:
        cli_mod.cmd_worktree_dogfood(args)
    assert excinfo.value.code == 1
    out = capsys.readouterr().out
    assert "1 failed" in out
    assert "guard failed (exit 3)" in out
    assert "judge skipped (--skip-judge)" in out
    assert "0 failed" not in out


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


# ── DF-GITREINS-POC-27: a plain checkout has no fleet board ──────────────────
#
# `.coding-hermes/board/` is a Hermes fleet scheduler artifact: `gitreins
# install`/`init` never create it, and the disposable batteries never read it.
# Every test below runs in a repo with no `.coding-hermes/` anywhere — they all
# used to die in WorktreeResolutionError before the board became optional.


@pytest.fixture
def plain_repo(tmp_path: Path) -> Path:
    """A git repo with `install` + `init` applied and no `.coding-hermes/`."""
    repo = tmp_path / "plain"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "GitReins Disposable Tests")
    _git(repo, "config", "user.email", "gitreins-disposable@example.invalid")
    (repo / "base.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "base.txt")
    _git(repo, "commit", "-qm", "initial")

    for command in ("install", "init"):
        applied = _run_cli(repo, command)
        assert applied.returncode == 0, applied.stderr
    assert not (repo / ".coding-hermes").exists()
    return repo


def _qa_rows(repo: Path) -> list[dict]:
    ledger = repo / ".gitreins" / "qa-ledger.jsonl"
    return [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines() if line]


def test_fresh_runs_in_a_plain_repo_without_a_board(plain_repo: Path):
    """`worktree fresh` exits 0 and records QA with no board to resolve."""
    result = _run_cli(plain_repo, "worktree", "fresh", "--cmd", "echo hello")

    assert result.returncode == 0, result.stderr
    assert "hello" in result.stdout
    rows = _qa_rows(plain_repo)
    assert [row["kind"] for row in rows] == ["fresh"]
    assert rows[0]["verdict"] == "PASS"
    assert not (plain_repo / ".coding-hermes").exists()


def test_repro_runs_in_a_plain_repo_without_a_board(plain_repo: Path):
    result = _run_cli(plain_repo, "worktree", "repro", "--cmd", "true", "-k", "2")

    assert result.returncode == 0, result.stderr
    assert "repro: 2/2 passed" in result.stdout
    assert [row["kind"] for row in _qa_rows(plain_repo)] == ["repro"]
    assert not (plain_repo / ".coding-hermes").exists()


def test_dogfood_runs_in_a_plain_repo_without_a_board(plain_repo: Path):
    result = _run_cli(plain_repo, "worktree", "dogfood", "--skip-judge", "--test-command", "true")

    assert result.returncode == 0, result.stderr
    assert "judge skipped" in result.stdout
    assert [row["kind"] for row in _qa_rows(plain_repo)] == ["dogfood"]
    assert not (plain_repo / ".coding-hermes").exists()


def test_disposable_manager_api_works_without_a_board(plain_repo: Path):
    """The manager API itself (not just the CLI) runs with no board."""
    verifier = DisposableWorktreeManager(plain_repo)

    run = verifier.run("echo hello")
    assert run["exit_code"] == 0
    assert "hello" in run["output"]

    report = verifier.dogfood(skip_judge=True, test_command="true")
    assert report["exit_code"] == 0
    assert [step["name"] for step in report["steps"]] == ["init", "task", "guard", "judge"]

    verifier.reap()
    assert verifier._load() == []
    assert not (plain_repo / ".coding-hermes").exists()


# ── DF-GITREINS-POC-71: child environment must not inherit the session ──────


def _write_tool_stub(bin_dir: Path, name: str, body: str) -> None:
    """Drop a stand-in shell tool into a toolchain bin dir."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    stub = bin_dir / name
    stub.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    stub.chmod(0o755)


def test_fresh_child_resolves_tree_toolchain_over_session_path(disposable_repo: Path, monkeypatch):
    """DF-GITREINS-POC-71 — `worktree fresh` runs the TREE's toolchain.

    The disposable run's child inherits the consumer session's PATH, so a
    deliberately flaky command kept via --keep-failures re-ran the session's
    own pytest (a different interpreter/checkout) inside the kept tree and
    passed — the captured failure was not reproducible in the artifact the
    flag exists to preserve. Dogfood run 15, 2026-09-26. Mirrored here: the
    tree carries a pytest that self-identifies and exits 0, while the
    session PATH leads with a pytest that exits 42. The child must run the
    tree's pytest (PATH resolves the tree's linked venv bin first).
    """
    _write_tool_stub(disposable_repo / ".venv" / "bin", "pytest", 'echo "venv-pytest $@"\nexit 0')
    foreign_bin = disposable_repo.parent / "foreign-bin"
    _write_tool_stub(foreign_bin, "pytest", "echo session-pytest\nexit 42")
    _write_tool_stub(foreign_bin, "python", "exit 42")
    monkeypatch.setenv("PATH", f"{foreign_bin}:{os.environ['PATH']}")

    manager = DisposableWorktreeManager(disposable_repo)
    result = manager.run("pytest test_flaky.py -q")

    assert result["exit_code"] == 0, result["output"]
    assert "venv-pytest" in result["output"]
    assert "session-pytest" not in result["output"]


def test_dogfood_steps_boot_the_harness_cli_under_hostile_session_env(
    disposable_repo: Path, monkeypatch
):
    """DF-GITREINS-POC-71 — dogfood steps boot the harness CLI deterministically.

    Reproduces the verified live failure: the parent interpreter is foreign
    to the harness checkout (an agent runtime's python first on PATH) and the
    child dies with ``ModuleNotFoundError: No module named 'engine'`` — the
    dogfood init step exits 1 and the whole run reports init failed. The step
    runner now pins PYTHONPATH to the harness root (replacing any session
    value) and prefers the tree's own venv interpreter, so a hostile session
    PATH/PYTHONPATH cannot change what the child imports or runs.
    """
    hostile_site = disposable_repo.parent / "hostile-site"
    (hostile_site / "engine").mkdir(parents=True)
    (hostile_site / "engine" / "__init__.py").write_text(
        "raise RuntimeError('session PYTHONPATH shadowed the harness engine package')\n",
        encoding="utf-8",
    )
    foreign_bin = disposable_repo.parent / "foreign-bin"
    _write_tool_stub(foreign_bin, "python3", "exit 42")
    monkeypatch.setenv("PYTHONPATH", str(hostile_site))
    monkeypatch.setenv("PATH", f"{foreign_bin}:{os.environ['PATH']}")

    manager = DisposableWorktreeManager(disposable_repo)
    report = manager.dogfood(skip_judge=True, test_command="true")

    assert report["exit_code"] == 0, [
        (step["name"], step["status"], step["output"]) for step in report["steps"]
    ]
    assert [step["name"] for step in report["steps"]] == ["init", "task", "guard", "judge"]
    statuses = {step["name"]: step["status"] for step in report["steps"]}
    # --skip-judge keeps the judge step recorded as skipped (documented
    # contract); the harness-CLI steps must all PASS under the hostile env.
    assert statuses == {
        "init": "passed",
        "task": "passed",
        "guard": "passed",
        "judge": "skipped",
    }
    # The hostile value belongs to the parent session; the child's pin must
    # not leak back into it.
    assert str(hostile_site) in (os.environ.get("PYTHONPATH") or "")


def test_child_env_helpers_pin_the_tree_toolchain_and_harness_root(tmp_path: Path):
    """The env-pinning contract lives in named helpers, not ad-hoc literals.

    Both builders must point PATH at the disposable tree's linked venv bin
    (falling back to the harness checkout's source venv when the link is
    missing) and the CLI builder must set PYTHONPATH to the harness root —
    the session value is replaced, not prepended, so a hostile PYTHONPATH
    cannot shadow the engine package.
    """
    from engine import worktree_disposable as disposable_mod
    from engine.worktree_manager import WorktreeManager

    _git(tmp_path, "init", "-q")
    manager = WorktreeManager(tmp_path)
    tree = tmp_path / "main-wt" / ".disposable" / "run-x"
    venv_bin = tree / manager.venv_name / "bin"
    venv_bin.mkdir(parents=True)

    run_env = disposable_mod._child_command_env(tree, manager)
    assert run_env["PATH"].split(os.pathsep)[0] == str(venv_bin)

    cli_env = disposable_mod._child_cli_env(tree, manager)
    assert cli_env["PYTHONPATH"] == str(Path(disposable_mod.__file__).resolve().parents[1])
    assert cli_env["PATH"].split(os.pathsep)[0] == str(venv_bin)
