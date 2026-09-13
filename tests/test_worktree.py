"""Tests for canonical board resolution across linked Git worktrees.

The second half of this file (WORKTREE-002) covers the task worktree
lifecycle: creation under ../<repo>-wt/<id>, the durable registry in the
main checkout's .gitreins/worktrees.json, reconciliation (running/guarding/
judging/merged/stale/orphan), and safe cleanup semantics.  Every test builds
its own real temporary git repository — the developer's repo and global git
config are never touched.
"""

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from engine.repo_paths import WorktreeResolutionError, board_file_path, resolve_worktree_paths
from engine.worktree_manager import (
    STALE_AFTER_SECONDS,
    WorktreeError,
    WorktreeManager,
    WorktreeValidationError,
    validate_task_id,
)
from gitreins.serve import load_jsonl


CLI_SCRIPT = Path(__file__).parents[1] / "gitreins" / "cli.py"


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def _make_repo(tmp_path: Path) -> tuple[Path, Path]:
    """Create a committed repo and a linked worktree with real Git metadata."""
    main = tmp_path / "main"
    linked = tmp_path / "linked"
    main.mkdir()
    _git(main, "init", "-q")
    _git(main, "config", "user.name", "GitReins Tests")
    _git(main, "config", "user.email", "gitreins-tests@example.invalid")
    (main / "base.txt").write_text("base\n", encoding="utf-8")
    _git(main, "add", "base.txt")
    _git(main, "commit", "-qm", "initial")
    _git(main, "worktree", "add", "-q", "-b", "feature", str(linked))
    return main, linked


def test_linked_worktree_resolves_main_board_and_board_writes(tmp_path: Path):
    main, linked = _make_repo(tmp_path)
    main_board = main / ".coding-hermes" / "board"
    local_board = linked / ".coding-hermes" / "board"
    main_board.mkdir(parents=True)
    local_board.mkdir(parents=True)
    (main_board / "tasks.jsonl").write_text('{"id":"main"}\n', encoding="utf-8")
    (local_board / "tasks.jsonl").write_text('{"id":"local"}\n', encoding="utf-8")

    paths = resolve_worktree_paths(linked)

    assert paths.invoking_worktree_root == linked.resolve()
    assert paths.git_common_dir == (main / ".git").resolve()
    assert paths.canonical_main_root == main.resolve()
    assert paths.canonical_board == main_board.resolve()
    assert paths.local_board == local_board.resolve()
    assert paths.local_board_exists is True
    assert load_jsonl(str(linked), "tasks.jsonl") == [{"id": "main"}]

    board_file_path(linked, "events.jsonl").write_text('{"from":"linked"}\n', encoding="utf-8")
    assert (main_board / "events.jsonl").read_text(encoding="utf-8") == '{"from":"linked"}\n'
    assert not (local_board / "events.jsonl").exists()


def test_resolution_handles_nested_invocation_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    main, linked = _make_repo(tmp_path)
    (main / ".coding-hermes" / "board").mkdir(parents=True)
    nested = linked / "nested" / "directory"
    nested.mkdir(parents=True)

    monkeypatch.chdir(nested)
    paths = resolve_worktree_paths()

    assert paths.invoking_worktree_root == linked.resolve()
    assert paths.canonical_main_root == main.resolve()


def test_single_checkout_resolution_is_unchanged(tmp_path: Path):
    main, _ = _make_repo(tmp_path)
    board = main / ".coding-hermes" / "board"
    board.mkdir(parents=True)

    paths = resolve_worktree_paths(main)

    assert paths.invoking_worktree_root == main.resolve()
    assert paths.canonical_main_root == main.resolve()
    assert paths.canonical_board == board.resolve()
    assert paths.local_board_exists is False


def test_resolution_fails_outside_git(tmp_path: Path):
    with pytest.raises(WorktreeResolutionError, match="not inside a Git repository"):
        resolve_worktree_paths(tmp_path)


def test_resolution_fails_when_canonical_board_is_missing(tmp_path: Path):
    main, _ = _make_repo(tmp_path)

    with pytest.raises(WorktreeResolutionError, match="canonical board directory does not exist"):
        resolve_worktree_paths(main)


def test_resolution_rejects_bare_repository(tmp_path: Path):
    bare = tmp_path / "bare.git"
    subprocess.run(["git", "init", "--bare", "-q", str(bare)], check=True)

    with pytest.raises(WorktreeResolutionError, match="bare Git repositories"):
        resolve_worktree_paths(bare)


def test_doctor_reports_shared_store_from_linked_worktree(tmp_path: Path):
    main, linked = _make_repo(tmp_path)
    (main / ".coding-hermes" / "board").mkdir(parents=True)
    (linked / ".coding-hermes" / "board").mkdir(parents=True)
    env = os.environ.copy()
    env["PYTHONPATH"] = str(CLI_SCRIPT.parents[1])

    result = subprocess.run(
        [sys.executable, str(CLI_SCRIPT), "worktree", "doctor"],
        cwd=linked,
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert f"Invoking worktree root: {linked.resolve()}" in result.stdout
    assert f"Git common dir: {(main / '.git').resolve()}" in result.stdout
    assert f"Canonical main checkout/root: {main.resolve()}" in result.stdout
    assert f"Canonical board path: {(main / '.coding-hermes' / 'board').resolve()}" in result.stdout
    assert "Ignored local worktree board copy: present" in result.stdout
    assert "Resolution: valid" in result.stdout


def test_doctor_fails_loudly_outside_git(tmp_path: Path):
    result = subprocess.run(
        [sys.executable, str(CLI_SCRIPT), "worktree", "doctor"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(CLI_SCRIPT.parents[1])},
        check=False,
    )

    assert result.returncode != 0
    assert "worktree doctor: invalid" in result.stderr
    assert "not inside a Git repository" in result.stderr


# ═══════════════════════════════════════════════════════════════════════
# WORKTREE-002 — task worktree lifecycle, registry, reconcile, cleanup
# ═══════════════════════════════════════════════════════════════════════


class FakeClock:
    """Controllable clock for deterministic stale/age assertions."""

    def __init__(self, start: float = 1_000_000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def wt_repo(tmp_path):
    """A real git repo with a canonical board, ready for worktree creation."""
    main = tmp_path / "main"
    main.mkdir()
    subprocess.run(["git", "init", "-q", str(main)], check=True)
    subprocess.run(["git", "-C", str(main), "config", "user.name", "GitReins Tests"], check=True)
    subprocess.run(
        ["git", "-C", str(main), "config", "user.email", "gitreins-tests@example.invalid"],
        check=True,
    )
    (main / "base.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(main), "add", "base.txt"], check=True)
    subprocess.run(["git", "-C", str(main), "commit", "-qm", "initial"], check=True)
    (main / ".coding-hermes" / "board").mkdir(parents=True)
    return main


def _commit_in(repo: Path, filename: str, message: str) -> None:
    (repo / filename).write_text(f"{filename}\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", filename], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", message], check=True)


def _branch_exists(main: Path, branch: str) -> bool:
    result = subprocess.run(
        ["git", "-C", str(main), "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


# ── validation ───────────────────────────────────────────────────────────


def test_validate_task_id_rejects_traversal_and_separators():
    for bad in ("../evil", "has/slash", "..", ".", "", "a\\b", "x" * 65):
        with pytest.raises(WorktreeValidationError):
            validate_task_id(bad)


def test_validate_task_id_accepts_reasonable_ids():
    for good in ("GR-GAP-061", "task_1", "A", "fix.auth-2"):
        assert validate_task_id(good) == good


# ── create / layout / branch / registry ──────────────────────────────────


def test_create_builds_layout_branch_and_registry(wt_repo):
    clock = FakeClock()
    manager = WorktreeManager(wt_repo, clock=clock)
    record, created = manager.create("GR-1", brief_path="/brief.md", tick="t-9")

    assert created is True
    expected = wt_repo.parent / "main-wt" / "GR-1"
    assert Path(record.path) == expected
    assert expected.is_dir() and (expected / ".git").is_file()
    assert record.branch == "gitreins/task/GR-1"
    assert record.brief_path == "/brief.md"
    assert record.tick == "t-9"
    assert record.state == "running"
    assert _branch_exists(wt_repo, record.branch)
    assert record.branch_point  # recorded for merged-vs-idle semantics


def test_create_is_idempotent_when_registry_and_git_agree(wt_repo):
    manager = WorktreeManager(wt_repo)
    first, created_first = manager.create("GR-2")
    second, created_second = manager.create("GR-2", brief_path="/new-brief.md")

    assert created_first is True and created_second is False
    assert second.path == first.path
    assert second.brief_path == "/new-brief.md"  # reuse refreshes the brief path


def test_registry_lives_in_main_checkout_and_survives_restart(wt_repo):
    manager = WorktreeManager(wt_repo)
    manager.create("GR-3")
    registry = wt_repo / ".gitreins" / "worktrees.json"
    assert registry.is_file()

    # A brand-new manager instance (fresh process equivalent) sees the record.
    revived = WorktreeManager(wt_repo)
    records = revived.list_records()
    assert [r.task_id for r in records] == ["GR-3"]


def test_registry_shared_from_linked_worktree(wt_repo, tmp_path):
    manager = WorktreeManager(wt_repo)
    manager.create("GR-4")
    linked = tmp_path / "linked"
    subprocess.run(
        ["git", "-C", str(wt_repo), "worktree", "add", "-q", "-b", "side", str(linked)],
        check=True,
    )

    from_linked = WorktreeManager(linked)
    records = from_linked.list_records()
    assert [r.task_id for r in records] == ["GR-4"]


def test_create_rejects_pre_existing_branch_outside_registry(wt_repo):
    subprocess.run(
        ["git", "-C", str(wt_repo), "branch", "gitreins/task/GR-5"],
        check=True,
    )
    manager = WorktreeManager(wt_repo)
    with pytest.raises(WorktreeError, match="registry and git disagree"):
        manager.create("GR-5")


def test_create_rejects_occupied_path_and_cross_task_collision(wt_repo):
    manager = WorktreeManager(wt_repo)
    manager.create("GR-6")
    occupied = wt_repo.parent / "main-wt" / "GR-7"
    occupied.mkdir(parents=True)
    (occupied / "sentinel.txt").write_text("not a worktree\n", encoding="utf-8")

    with pytest.raises(WorktreeError, match="not empty"):
        manager.create("GR-7")

    # A different task registered on GR-7's derived path blocks creation.
    import copy

    records = manager._load_registry()
    squatter = copy.deepcopy(records["GR-6"])
    squatter.path = str(wt_repo.parent / "main-wt" / "GR-7")
    records["GR-6"] = squatter
    manager._save_registry(records)

    with pytest.raises(WorktreeError, match="already registered to task 'GR-6'"):
        manager.create("GR-7")


def test_create_refuses_reuse_when_registry_and_git_disagree_on_branch(wt_repo):
    manager = WorktreeManager(wt_repo)
    manager.create("GR-8")
    # Simulate drift: someone switched the tree to a foreign branch.
    tree = wt_repo.parent / "main-wt" / "GR-8"
    subprocess.run(["git", "-C", str(tree), "checkout", "-q", "-b", "foreign"], check=True)

    with pytest.raises(WorktreeError, match="does not match git reality"):
        manager.create("GR-8")


# ── reconcile semantics ──────────────────────────────────────────────────


def test_reconcile_states_running_merged_orphan(wt_repo):
    clock = FakeClock()
    manager = WorktreeManager(wt_repo)

    manager.create("WT-RUN")
    manager.create("WT-IDLE")
    manager.create("WT-MERGED")
    manager.create("WT-ORPHAN")

    # WT-MERGED: real work merged into main.
    merged_tree = wt_repo.parent / "main-wt" / "WT-MERGED"
    _commit_in(merged_tree, "m.txt", "work")
    subprocess.run(
        ["git", "-C", str(wt_repo), "merge", "--ff-only", "-q", "gitreins/task/WT-MERGED"],
        check=True,
    )
    # WT-ORPHAN: tree deleted behind git's back.
    shutil.rmtree(wt_repo.parent / "main-wt" / "WT-ORPHAN")

    records = manager.reconcile_and_persist()
    assert records["WT-RUN"].state == "running"
    assert records["WT-IDLE"].state == "running"  # idle tree at branch point ≠ merged
    assert records["WT-MERGED"].state == "merged"
    assert records["WT-ORPHAN"].state == "orphan"
    assert records["WT-ORPHAN"].notes  # classified with an explanatory note


def test_reconcile_marks_stale_after_24h_without_heartbeat(wt_repo):
    clock = FakeClock()
    manager = WorktreeManager(wt_repo, clock=clock)
    manager.create("WT-STALE")
    clock.advance(STALE_AFTER_SECONDS + 60)

    records = manager.reconcile_and_persist()
    assert records["WT-STALE"].state == "stale"
    # A fresh heartbeat pulls it back to running.
    manager.heartbeat("WT-STALE")
    records = manager.reconcile_and_persist()
    assert records["WT-STALE"].state == "running"


def test_mark_phase_records_guarding_and_judging(wt_repo):
    manager = WorktreeManager(wt_repo)
    manager.create("WT-PHASE")
    manager.mark_phase("WT-PHASE", "guarding")
    assert manager._load_registry()["WT-PHASE"].state == "guarding"
    manager.mark_phase("WT-PHASE", "judging")
    assert manager._load_registry()["WT-PHASE"].state == "judging"

    with pytest.raises(ValueError):
        manager.mark_phase("WT-PHASE", "bogus")


def test_reconcile_survives_torn_registry_entries(wt_repo):
    manager = WorktreeManager(wt_repo)
    manager.create("WT-TORN")
    registry = wt_repo / ".gitreins" / "worktrees.json"
    data = json.loads(registry.read_text(encoding="utf-8"))
    data["worktrees"].append({"task_id": "BROKEN"})  # missing required fields
    registry.write_text(json.dumps(data), encoding="utf-8")

    records = manager.reconcile_and_persist()
    assert "WT-TORN" in records
    assert "BROKEN" not in records


# ── cleanup ──────────────────────────────────────────────────────────────


def test_clean_reaps_merged_immediately_but_keeps_live_work(wt_repo):
    clock = FakeClock()
    manager = WorktreeManager(wt_repo)
    manager.create("WT-LIVE")
    manager.create("WT-DONE")

    done_tree = wt_repo.parent / "main-wt" / "WT-DONE"
    _commit_in(done_tree, "d.txt", "done")
    subprocess.run(
        ["git", "-C", str(wt_repo), "merge", "--ff-only", "-q", "gitreins/task/WT-DONE"],
        check=True,
    )

    report = manager.clean()

    assert report["removed"] == ["WT-DONE"]
    assert not done_tree.exists()
    assert not _branch_exists(wt_repo, "gitreins/task/WT-DONE")
    assert ("WT-LIVE", "running") in report["kept"]
    assert (wt_repo.parent / "main-wt" / "WT-LIVE").exists()
    assert manager._load_registry().keys() == {"WT-LIVE"}


def test_clean_never_reaps_stale_or_orphan_without_confirmation(wt_repo):
    clock = FakeClock()
    manager = WorktreeManager(wt_repo, clock=clock)
    manager.create("WT-OLD")
    manager.create("WT-GONE")
    clock.advance(STALE_AFTER_SECONDS + 120)
    shutil.rmtree(wt_repo.parent / "main-wt" / "WT-GONE")

    # Unconfirmed: both survive, trees intact, registry intact.
    report = manager.clean()
    assert report["removed"] == []
    assert ("WT-OLD", "stale") in report["kept"]
    assert ("WT-GONE", "orphan") in report["kept"]
    assert (wt_repo.parent / "main-wt" / "WT-OLD").exists()

    # Confirmed: both reaped, merged-style branch hygiene applied.
    report = manager.clean(confirm_stale_orphan=True)
    assert sorted(report["removed"]) == ["WT-GONE", "WT-OLD"]
    assert manager._load_registry() == {}
    assert not (wt_repo.parent / "main-wt" / "WT-OLD").exists()
    assert not _branch_exists(wt_repo, "gitreins/task/WT-OLD")
    assert not _branch_exists(wt_repo, "gitreins/task/WT-GONE")


def test_confirmed_clean_keeps_unmerged_branch_of_stale_work(wt_repo):
    """A stale tree with real unmerged work loses the tree, never the branch."""
    clock = FakeClock()
    manager = WorktreeManager(wt_repo, clock=clock)
    manager.create("WT-WIP")
    wip_tree = wt_repo.parent / "main-wt" / "WT-WIP"
    _commit_in(wip_tree, "w.txt", "wip")
    clock.advance(STALE_AFTER_SECONDS + 120)

    report = manager.clean(confirm_stale_orphan=True)

    assert report["removed"] == ["WT-WIP"]
    assert not wip_tree.exists()
    assert _branch_exists(wt_repo, "gitreins/task/WT-WIP")  # branch preserved
    assert report["branches_deleted"] == []


# ── listing ──────────────────────────────────────────────────────────────


def test_list_records_sorted_and_truthful(wt_repo):
    clock = FakeClock()
    manager = WorktreeManager(wt_repo)
    manager.create("WT-B")
    clock.advance(10)
    manager.create("WT-A")

    records = manager.list_records()
    assert [r.task_id for r in records] == ["WT-B", "WT-A"]  # creation order


# ── doctor compatibility (unchanged semantics) ───────────────────────────


def test_doctor_still_reports_shared_store_after_lifecycle_wiring(wt_repo, tmp_path):
    linked = tmp_path / "linked"
    subprocess.run(
        ["git", "-C", str(wt_repo), "worktree", "add", "-q", "-b", "side", str(linked)],
        check=True,
    )
    (wt_repo / ".coding-hermes" / "board").mkdir(parents=True, exist_ok=True)
    (linked / ".coding-hermes" / "board").mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["PYTHONPATH"] = str(CLI_SCRIPT.parents[1])

    result = subprocess.run(
        [sys.executable, str(CLI_SCRIPT), "worktree", "doctor"],
        cwd=linked,
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "Resolution: valid" in result.stdout


# ── CLI surfaces ─────────────────────────────────────────────────────────


def _run_cli(*argv, cwd):
    env = os.environ.copy()
    env["PYTHONPATH"] = str(CLI_SCRIPT.parents[1])
    return subprocess.run(
        [sys.executable, str(CLI_SCRIPT), *argv],
        cwd=cwd,
        capture_output=True,
        text=True,
        env=env,
        check=False,
        timeout=60,
    )


def test_cli_task_worktree_creates_reports_and_reuses(wt_repo):
    result = _run_cli("task", "worktree", "CLI-1", cwd=wt_repo)
    assert result.returncode == 0, result.stderr
    tree = wt_repo.parent / "main-wt" / "CLI-1"
    assert f"✓ Created worktree {tree}" in result.stdout
    assert "(branch gitreins/task/CLI-1)" in result.stdout
    assert f"worker brief path: {tree / '.gitreins' / 'worker-brief.md'}" in result.stdout
    assert _branch_exists(wt_repo, "gitreins/task/CLI-1")
    registry = json.loads((wt_repo / ".gitreins" / "worktrees.json").read_text())
    assert registry["worktrees"][0]["brief_path"].endswith("worker-brief.md")

    reuse = _run_cli("task", "worktree", "CLI-1", cwd=wt_repo)
    assert reuse.returncode == 0, reuse.stderr
    assert "✓ Reused worktree" in reuse.stdout


def test_cli_task_worktree_rejects_bad_id(wt_repo):
    result = _run_cli("task", "worktree", "../escape", cwd=wt_repo)
    assert result.returncode == 1
    assert "task worktree: failed" in result.stderr
    assert "invalid task id" in result.stderr


def test_cli_worktree_list_shows_task_state_age(wt_repo):
    _run_cli("task", "worktree", "CLI-L", cwd=wt_repo)
    result = _run_cli("worktree", "list", cwd=wt_repo)
    assert result.returncode == 0, result.stderr
    assert "CLI-L" in result.stdout
    assert "running" in result.stdout
    assert "gitreins/task/CLI-L" in result.stdout
    assert re.search(r"AGE", result.stdout)
    assert re.search(r"\d+[smhd]", result.stdout)  # an age token is printed

    empty = _run_cli("worktree", "list", cwd=wt_repo)  # still one entry
    assert "CLI-L" in empty.stdout


def test_cli_worktree_list_empty(wt_repo):
    result = _run_cli("worktree", "list", cwd=wt_repo)
    assert result.returncode == 0, result.stderr
    assert "No worktrees registered." in result.stdout


def test_cli_worktree_clean_requires_confirmation_then_reaps(wt_repo):
    _run_cli("task", "worktree", "CLI-M", cwd=wt_repo)
    _run_cli("task", "worktree", "CLI-O", cwd=wt_repo)
    merged_tree = wt_repo.parent / "main-wt" / "CLI-M"
    gone_tree = wt_repo.parent / "main-wt" / "CLI-O"
    _commit_in(merged_tree, "c.txt", "cli")
    subprocess.run(
        ["git", "-C", str(wt_repo), "merge", "--ff-only", "-q", "gitreins/task/CLI-M"],
        check=True,
    )
    shutil.rmtree(gone_tree)

    refused = _run_cli("worktree", "clean", cwd=wt_repo)
    assert refused.returncode == 0, refused.stderr
    assert "Reaped 1 worktree(s): CLI-M" in refused.stdout
    assert "CLI-O [orphan]" in refused.stdout
    assert "--confirm-stale-orphan" in refused.stdout
    assert not merged_tree.exists()  # merged reaped immediately
    assert gone_tree.parent / "CLI-O" == gone_tree  # sanity

    confirmed = _run_cli("worktree", "clean", "--confirm-stale-orphan", cwd=wt_repo)
    assert confirmed.returncode == 0, confirmed.stderr
    assert "Reaped 1 worktree(s): CLI-O" in confirmed.stdout


def test_cli_worktree_help_lists_new_subcommands():
    result = _run_cli("worktree", "--help", cwd=Path.cwd())
    assert result.returncode == 0, result.stderr
    for token in ("doctor", "list", "clean"):
        assert token in result.stdout
