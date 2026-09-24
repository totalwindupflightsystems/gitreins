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


def _plain_repo(tmp_path: Path) -> Path:
    """A committed repo with no ``.coding-hermes/`` anywhere (DF-GITREINS-POC-27)."""
    repo = tmp_path / "plain"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "GitReins Tests")
    _git(repo, "config", "user.email", "gitreins-tests@example.invalid")
    (repo / "base.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "base.txt")
    _git(repo, "commit", "-qm", "initial")
    return repo


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


def test_resolution_without_a_canonical_board_is_not_an_error(tmp_path: Path):
    """DF-GITREINS-POC-27: an absent fleet board resolves; it is not an error.

    ``.coding-hermes/board/`` is created by the Hermes fleet scheduler, not by
    ``gitreins install``/``init``, so a plain checkout must still resolve — with
    ``board_exists`` False so the consumers that use the board skip it instead
    of the whole invocation failing.
    """
    main = _plain_repo(tmp_path)

    paths = resolve_worktree_paths(main)

    assert paths.invoking_worktree_root == main.resolve()
    assert paths.canonical_main_root == main.resolve()
    assert paths.canonical_board == (main / ".coding-hermes" / "board").resolve()
    assert paths.board_exists is False
    assert not (main / ".coding-hermes").exists()


def test_append_board_event_skips_silently_without_a_board_and_creates_nothing(tmp_path: Path):
    """A merge/lane event with no fleet board is skipped, never a mkdir."""
    repo = _plain_repo(tmp_path)
    manager = WorktreeManager(repo)

    event = manager._append_board_event({"event_type": "worktree_merged", "task_id": "NB-1"})

    assert event is None
    assert not (repo / ".coding-hermes").exists()


def test_merge_without_a_board_still_fast_forwards(tmp_path: Path):
    """The merge itself is not gated on fleet bookkeeping existing."""
    repo = _plain_repo(tmp_path)
    manager = WorktreeManager(repo)
    record, tree = _make_task_commit(manager, "MERGE-NOBOARD")

    result = manager.merge(record.task_id, force=True, actor="no-board-actor")

    assert result["mode"] == "fast-forward"
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == result["destination_commit"]
    assert not tree.exists()
    assert manager._load_registry() == {}
    assert not (repo / ".coding-hermes").exists()


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


def _switch_tree_branch(tree: Path, branch: str) -> None:
    """Move a worktree onto another branch (registry/git metadata drift)."""
    subprocess.run(["git", "-C", str(tree), "checkout", "-q", "-b", branch], check=True)


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


# ── DF-GITREINS-POC-50 — failed lanes are reapable, never silently reusable ──


def test_clean_reaps_failed_lane_tree_and_branch(wt_repo):
    """A failed lane is terminal, so plain clean reaps it like a merged one."""
    manager = WorktreeManager(wt_repo)
    record, _created = manager.create("WT-FAIL")
    manager.mark_lane("WT-FAIL", "failed", exit_code=7, error="lane boom")

    report = manager.clean()

    assert report["removed"] == ["WT-FAIL"]
    assert report["kept"] == []
    assert report["kept_reasons"] == {}
    assert "gitreins/task/WT-FAIL" in report["branches_deleted"]
    assert not Path(record.path).exists()
    assert not _branch_exists(wt_repo, "gitreins/task/WT-FAIL")
    assert manager._load_registry() == {}


def test_clean_reaps_failed_tree_but_keeps_its_unmerged_branch(wt_repo):
    """Removal never destroys committed work the branch still carries."""
    manager = WorktreeManager(wt_repo)
    record, _created = manager.create("WT-FAIL-WIP")
    _commit_in(Path(record.path), "wip.txt", "failed lane work")
    manager.mark_lane("WT-FAIL-WIP", "failed", exit_code=1)

    report = manager.clean()

    assert report["removed"] == ["WT-FAIL-WIP"]
    assert not Path(record.path).exists()
    assert report["branches_deleted"] == []  # unmerged branch survives `-d`
    assert _branch_exists(wt_repo, "gitreins/task/WT-FAIL-WIP")
    assert manager._load_registry() == {}


def test_clean_keeps_failed_tree_holding_uncommitted_files_and_says_why(wt_repo):
    """The uncommitted-file doctrine: reap the tree, never its unread dirt."""
    manager = WorktreeManager(wt_repo)
    record, _created = manager.create("WT-FAIL-DIRTY")
    manager.mark_lane("WT-FAIL-DIRTY", "failed", exit_code=1)
    tree = Path(record.path)
    (tree / "scratch.txt").write_text("uncommitted\n", encoding="utf-8")

    report = manager.clean()

    assert report["removed"] == []
    assert report["kept"] == [("WT-FAIL-DIRTY", "failed")]
    assert tree.is_dir()
    assert _branch_exists(wt_repo, "gitreins/task/WT-FAIL-DIRTY")
    reason = report["kept_reasons"]["WT-FAIL-DIRTY"]
    assert "uncommitted" in reason
    assert "gitreins worktree clean" in reason
    assert (
        report["kept_reasons"]["WT-FAIL-DIRTY"] in manager._load_registry()["WT-FAIL-DIRTY"].notes
    )

    # Committing the scratch file clears the block: the same run then reaps it.
    _commit_in(tree, "scratch.txt", "commit the evidence")
    assert manager.clean()["removed"] == ["WT-FAIL-DIRTY"]


def test_create_refuses_reuse_of_failed_lane_and_names_the_fix(wt_repo):
    """A failed lane's tree sits at a stale HEAD — never reuse it silently."""
    manager = WorktreeManager(wt_repo)
    manager.create("WT-RERUN")
    manager.mark_lane("WT-RERUN", "failed", exit_code=9, error="guard failed")

    with pytest.raises(WorktreeError) as excinfo:
        manager.create("WT-RERUN")

    message = str(excinfo.value)
    assert "FAILED worktree" in message
    assert "gitreins worktree clean" in message
    assert "--confirm-stale-orphan" not in message  # plain clean is the fix here
    assert Path(wt_repo.parent / "main-wt" / "WT-RERUN").is_dir()  # nothing destroyed

    # Following the hint actually resolves it, and the re-run then succeeds.
    assert manager.clean()["removed"] == ["WT-RERUN"]
    _record, created = manager.create("WT-RERUN")
    assert created is True


def test_failed_lane_aged_past_stale_is_refused_with_the_confirm_hint(wt_repo):
    """Reconcile rewrites an old failure to `stale`; lane_phase keeps the memory.

    The hint must still be the command that resolves it: plain clean protects
    stale entries, so the refusal names `--confirm-stale-orphan`.
    """
    clock = FakeClock()
    manager = WorktreeManager(wt_repo, clock=clock)
    record, _created = manager.create("WT-AGED")
    _commit_in(Path(record.path), "aged.txt", "failed lane work")
    manager.mark_lane("WT-AGED", "failed", exit_code=9)
    clock.advance(STALE_AFTER_SECONDS + 120)

    with pytest.raises(WorktreeError) as excinfo:
        manager.create("WT-AGED")

    message = str(excinfo.value)
    assert "FAILED worktree" in message
    assert "gitreins worktree clean --confirm-stale-orphan" in message

    unconfirmed = manager.clean()
    assert unconfirmed["removed"] == []
    assert ("WT-AGED", "stale") in unconfirmed["kept"]  # plain clean cannot fix it
    assert manager.clean(confirm_stale_orphan=True)["removed"] == ["WT-AGED"]


def test_reconcile_hint_names_the_confirm_flag_for_an_orphaned_failed_lane(wt_repo):
    """DF-GITREINS-POC-50 AC4: the hint fits the problem class (orphan → flag)."""
    manager = WorktreeManager(wt_repo)
    record, _created = manager.create("WT-ORPHANED")
    manager.mark_lane("WT-ORPHANED", "failed", exit_code=9)
    shutil.rmtree(record.path)

    with pytest.raises(WorktreeError) as excinfo:
        manager.create("WT-ORPHANED")

    message = str(excinfo.value)
    assert "does not match git reality" in message
    assert "gitreins worktree clean --confirm-stale-orphan" in message

    # The named command is the one that works: plain clean keeps the orphan.
    assert manager.clean()["kept"] == [("WT-ORPHANED", "orphan")]
    assert manager.clean(confirm_stale_orphan=True)["removed"] == ["WT-ORPHANED"]


def test_reconcile_hint_escalates_when_clean_cannot_reap_the_entry(wt_repo):
    """A live entry on the wrong branch is not reapable by clean, and says so.

    The old hint pointed every mismatch at plain `clean`, the command that
    cannot resolve this one — the consumer re-ran it and stayed stuck.
    """
    manager = WorktreeManager(wt_repo)
    record, _created = manager.create("WT-DRIFTED")
    _switch_tree_branch(Path(record.path), "side-drift")

    with pytest.raises(WorktreeError) as excinfo:
        manager.create("WT-DRIFTED")

    message = str(excinfo.value)
    assert "does not match git reality" in message
    assert "git worktree remove --force" in message

    # Honest: plain clean really does keep it (it is not stale, not failed).
    assert manager.clean()["kept"] == [("WT-DRIFTED", "running")]


def test_failed_lane_aged_past_stale_but_merged_is_reaped_by_plain_clean(wt_repo):
    """When the branch reached HEAD, plain clean IS the fix and the hint says so."""
    clock = FakeClock()
    manager = WorktreeManager(wt_repo, clock=clock)
    record, _created = manager.create("WT-AGED-MERGED")
    _commit_in(Path(record.path), "merged.txt", "lane work")
    subprocess.run(
        [
            "git",
            "-C",
            str(wt_repo),
            "merge",
            "--ff-only",
            "-q",
            "gitreins/task/WT-AGED-MERGED",
        ],
        check=True,
    )
    manager.mark_lane("WT-AGED-MERGED", "failed", exit_code=9)
    clock.advance(STALE_AFTER_SECONDS + 120)

    with pytest.raises(WorktreeError) as excinfo:
        manager.create("WT-AGED-MERGED")

    message = str(excinfo.value)
    assert "FAILED worktree" in message
    assert "--confirm-stale-orphan" not in message
    assert manager.clean()["removed"] == ["WT-AGED-MERGED"]


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


def test_cli_worktree_clean_reaps_failed_lane_without_flags(wt_repo):
    """DF-GITREINS-POC-50 AC1: plain clean must reap a failed lane."""
    _run_cli("task", "worktree", "CLI-F", cwd=wt_repo)
    WorktreeManager(wt_repo).mark_lane("CLI-F", "failed", exit_code=3, error="lane boom")

    result = _run_cli("worktree", "clean", cwd=wt_repo)

    assert result.returncode == 0, result.stderr
    assert "Reaped 1 worktree(s): CLI-F" in result.stdout
    assert "Kept" not in result.stdout
    assert not (wt_repo.parent / "main-wt" / "CLI-F").exists()
    assert not _branch_exists(wt_repo, "gitreins/task/CLI-F")


def test_cli_worktree_clean_reports_why_a_failed_tree_was_kept(wt_repo):
    """A kept failed tree is never a mystery: the reason is printed."""
    _run_cli("task", "worktree", "CLI-FD", cwd=wt_repo)
    WorktreeManager(wt_repo).mark_lane("CLI-FD", "failed", exit_code=3)
    tree = wt_repo.parent / "main-wt" / "CLI-FD"
    (tree / "scratch.txt").write_text("uncommitted\n", encoding="utf-8")

    result = _run_cli("worktree", "clean", cwd=wt_repo)

    assert result.returncode == 0, result.stderr
    assert "Nothing to reap." in result.stdout
    assert "CLI-FD [failed]" in result.stdout
    assert "uncommitted" in result.stdout
    assert tree.is_dir()


def test_cli_worktree_help_lists_new_subcommands():
    result = _run_cli("worktree", "--help", cwd=Path.cwd())
    assert result.returncode == 0, result.stderr
    for token in ("doctor", "list", "clean", "merge"):
        assert token in result.stdout


# ── WORKTREE-004 — judge-gated merge-back ────────────────────────────────


def _write_merge_config(repo: Path) -> None:
    config = repo / ".gitreins" / "config.yaml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text("history:\n  storage: filesystem\n", encoding="utf-8")
    _git(repo, "add", ".gitreins/config.yaml")
    _git(repo, "commit", "-qm", "test merge config")


def _write_merge_verdict(
    repo: Path, task_id: str, record, passed: bool, commit: str, stages: dict | None = None
) -> Path:
    # Judge persistence is rooted at the producing task checkout.  Keep the
    # repo argument for callers that also use it to prepare config, but never
    # use canonical main as the verdict artifact location.
    entry = (
        Path(record.path) / ".gitreins" / "history" / "2026-01-01" / ("pass" if passed else "fail")
    )
    entry.mkdir(parents=True, exist_ok=True)
    verdict = {
        "task_id": task_id,
        "passed": passed,
        "worktree": str(Path(record.path).resolve()),
        "branch": record.branch,
        "commit": commit,
    }
    if stages is not None:
        verdict["stages"] = stages
    path = entry / "verdict.json"
    path.write_text(json.dumps(verdict), encoding="utf-8")
    return path


def _make_task_commit(manager: WorktreeManager, task_id: str, filename: str = "task.txt"):
    record, _ = manager.create(task_id)
    tree = Path(record.path)
    _commit_in(tree, filename, f"{task_id} work")
    return record, tree


def test_merge_refuses_without_exact_pass_verdict_and_keeps_worktree(wt_repo):
    manager = WorktreeManager(wt_repo)
    record, tree = _make_task_commit(manager, "MERGE-NO-VERDICT")
    before = _git(wt_repo, "rev-parse", "HEAD").stdout.strip()

    with pytest.raises(WorktreeError, match="verdict"):
        manager.merge(record.task_id)

    assert _git(wt_repo, "rev-parse", "HEAD").stdout.strip() == before
    assert tree.exists() and _branch_exists(wt_repo, record.branch)


def test_merge_fail_verdict_is_hold(wt_repo):
    _write_merge_config(wt_repo)
    manager = WorktreeManager(wt_repo)
    record, tree = _make_task_commit(manager, "MERGE-FAIL")
    source = _git(tree, "rev-parse", "HEAD").stdout.strip()
    _write_merge_verdict(wt_repo, record.task_id, record, False, source)
    before = _git(wt_repo, "rev-parse", "HEAD").stdout.strip()

    with pytest.raises(WorktreeError, match="HOLD"):
        manager.merge(record.task_id)

    assert _git(wt_repo, "rev-parse", "HEAD").stdout.strip() == before
    assert tree.exists() and _branch_exists(wt_repo, record.branch)


def test_merge_pass_verdict_with_tier1_skips_is_hold(wt_repo):
    """TRUST-001: a PASS whose Tier 1 carries skips cannot merge back.

    verdict.json records ``stages.tier1.skipped_steps`` when a substantive gate
    did no work (nothing staged, linter absent). The merge-back is the last
    consumer of that verdict, so it must refuse — the gate that would have
    caught a defect never ran.
    """
    _write_merge_config(wt_repo)
    manager = WorktreeManager(wt_repo)
    record, tree = _make_task_commit(manager, "MERGE-SKIP")
    source = _git(tree, "rev-parse", "HEAD").stdout.strip()
    _write_merge_verdict(
        wt_repo,
        record.task_id,
        record,
        True,
        source,
        stages={
            "tier1": {
                "id": "tier1",
                "passed": True,
                "coverage": "secrets",
                "degraded": True,
                "skipped_steps": ["lint"],
                "degradation_reason": "skipped at runtime — lint: no linter on PATH",
            }
        },
    )
    before = _git(wt_repo, "rev-parse", "HEAD").stdout.strip()

    with pytest.raises(WorktreeError, match="HOLD.*skipped"):
        manager.merge(record.task_id)

    assert _git(wt_repo, "rev-parse", "HEAD").stdout.strip() == before
    assert tree.exists() and _branch_exists(wt_repo, record.branch)


def test_merge_pass_fast_forwards_logs_event_and_reaps(wt_repo):
    _write_merge_config(wt_repo)
    manager = WorktreeManager(wt_repo)
    record, tree = _make_task_commit(manager, "MERGE-FAST")
    source = _git(tree, "rev-parse", "HEAD").stdout.strip()

    # Production judge persistence runs in the task checkout.  Keep unrelated
    # local history in canonical main to prove it cannot shadow the task
    # verdict, then persist the matching PASS under the registered tree.
    from engine.persist import VerdictPersister

    main_persister = VerdictPersister(str(wt_repo))
    main_persister.persist(
        "UNRELATED-MAIN",
        {"passed": True, "worktree": str(wt_repo.resolve()), "branch": "main", "commit": source},
    )
    task_persister = VerdictPersister(str(tree))
    task_persister.persist(
        record.task_id,
        {
            "passed": True,
            "worktree": str(tree.resolve()),
            "branch": record.branch,
            "commit": source,
        },
    )
    local_entries = task_persister.list_verdicts(task_id=record.task_id)
    assert len(local_entries) == 1
    local_artifact = (
        Path(task_persister.history_dir)
        / local_entries[0]["_date"]
        / local_entries[0]["_hash"]
        / "verdict.json"
    )
    assert local_artifact.is_file()

    result = manager.merge(record.task_id)

    assert result["mode"] == "fast-forward"
    assert _git(wt_repo, "rev-parse", "HEAD").stdout.strip() == source
    assert not tree.exists()
    assert not _branch_exists(wt_repo, record.branch)
    assert manager._load_registry() == {}
    events = [
        json.loads(line)
        for line in (wt_repo / ".coding-hermes" / "board" / "events.jsonl").read_text().splitlines()
    ]
    event = events[-1]
    assert event["event_type"] == "worktree_merged"
    assert event["task_id"] == record.task_id
    assert event["source_commit"] == source
    assert event["destination_commit"] == source


def test_merge_main_moved_rebases_reruns_guard_and_requires_fresh_judge(wt_repo):
    _write_merge_config(wt_repo)
    manager = WorktreeManager(wt_repo)
    record, tree = _make_task_commit(manager, "MERGE-REBASE")
    old_source = _git(tree, "rev-parse", "HEAD").stdout.strip()
    _write_merge_verdict(wt_repo, record.task_id, record, True, old_source)
    _commit_in(wt_repo, "main.txt", "main moved")
    main_before = _git(wt_repo, "rev-parse", "HEAD").stdout.strip()
    calls = []

    def guard(path):
        calls.append(("guard", _git(path, "rev-parse", "HEAD").stdout.strip()))
        return True

    def judge(path, task_id):
        calls.append(("judge", _git(path, "rev-parse", "HEAD").stdout.strip()))
        rebased = _git(path, "rev-parse", "HEAD").stdout.strip()
        _write_merge_verdict(wt_repo, task_id, record, True, rebased)

    result = manager.merge(record.task_id, guard_runner=guard, judge_runner=judge)

    rebased = calls[0][1]
    assert result["mode"] == "rebased-fast-forward"
    assert rebased != old_source
    assert calls[0][0] == "guard" and calls[1][0] == "judge"
    assert _git(wt_repo, "rev-parse", "HEAD").stdout.strip() == rebased
    assert main_before != rebased
    assert not tree.exists() and manager._load_registry() == {}


def test_merge_rebase_red_guard_holds_rebased_worktree(wt_repo):
    _write_merge_config(wt_repo)
    manager = WorktreeManager(wt_repo)
    record, tree = _make_task_commit(manager, "MERGE-GUARD-RED")
    old_source = _git(tree, "rev-parse", "HEAD").stdout.strip()
    _write_merge_verdict(wt_repo, record.task_id, record, True, old_source)
    _commit_in(wt_repo, "main.txt", "main moved")
    main_before = _git(wt_repo, "rev-parse", "HEAD").stdout.strip()

    with pytest.raises(WorktreeError, match="guard"):
        manager.merge(record.task_id, guard_runner=lambda _path: False)

    assert _git(wt_repo, "rev-parse", "HEAD").stdout.strip() == main_before
    assert tree.exists() and _branch_exists(wt_repo, record.branch)
    assert _git(tree, "rev-parse", "HEAD").stdout.strip() != old_source


def test_merge_rebase_without_fresh_pass_holds_rebased_worktree(wt_repo):
    _write_merge_config(wt_repo)
    manager = WorktreeManager(wt_repo)
    record, tree = _make_task_commit(manager, "MERGE-FRESH-FAIL")
    old_source = _git(tree, "rev-parse", "HEAD").stdout.strip()
    _write_merge_verdict(wt_repo, record.task_id, record, True, old_source)
    _commit_in(wt_repo, "main.txt", "main moved")
    main_before = _git(wt_repo, "rev-parse", "HEAD").stdout.strip()

    with pytest.raises(WorktreeError, match="fresh PASS"):
        manager.merge(
            record.task_id,
            guard_runner=lambda _path: True,
            judge_runner=lambda _path, _task_id: {"passed": False},
        )

    assert _git(wt_repo, "rev-parse", "HEAD").stdout.strip() == main_before
    assert tree.exists() and _branch_exists(wt_repo, record.branch)


def test_merge_rebase_conflict_holds_main_and_worktree(wt_repo):
    _write_merge_config(wt_repo)
    manager = WorktreeManager(wt_repo)
    record, tree = _make_task_commit(manager, "MERGE-CONFLICT", "same.txt")
    (tree / "same.txt").write_text("task\n", encoding="utf-8")
    _git(tree, "add", "same.txt")
    _git(tree, "commit", "-qm", "task conflict")
    (wt_repo / "same.txt").write_text("main\n", encoding="utf-8")
    _git(wt_repo, "add", "same.txt")
    _git(wt_repo, "commit", "-qm", "main conflict")
    source_before = _git(tree, "rev-parse", "HEAD").stdout.strip()
    main_before = _git(wt_repo, "rev-parse", "HEAD").stdout.strip()
    _write_merge_verdict(wt_repo, record.task_id, record, True, source_before)

    with pytest.raises(WorktreeError, match="rebase"):
        manager.merge(record.task_id)

    assert _git(wt_repo, "rev-parse", "HEAD").stdout.strip() == main_before
    assert _git(tree, "rev-parse", "HEAD").stdout.strip() == source_before
    assert tree.exists() and _branch_exists(wt_repo, record.branch)


def test_merge_force_requires_actor_and_logs_override(wt_repo):
    manager = WorktreeManager(wt_repo)
    record, tree = _make_task_commit(manager, "MERGE-FORCE")
    source = _git(tree, "rev-parse", "HEAD").stdout.strip()

    with pytest.raises(WorktreeError, match="actor"):
        manager.merge(record.task_id, force=True)
    assert tree.exists()

    result = manager.merge(record.task_id, force=True, actor="test-actor", reason="emergency")
    assert result["mode"] == "fast-forward"
    events = [
        json.loads(line)
        for line in (wt_repo / ".coding-hermes" / "board" / "events.jsonl").read_text().splitlines()
    ]
    override = next(event for event in events if event["event_type"] == "worktree_merge_override")
    assert override["actor"] == "test-actor"
    assert override["source_branch"] == record.branch
    assert override["source_commit"] == source
    assert override["destination_commit"] == source
    assert override["reason"] == "emergency"


def test_merge_rejects_unknown_and_unsafe_task_ids(wt_repo):
    manager = WorktreeManager(wt_repo)
    with pytest.raises(WorktreeError, match="not registered"):
        manager.merge("UNKNOWN")
    with pytest.raises(WorktreeValidationError):
        manager.merge("../unsafe")

    record, _tree = _make_task_commit(manager, "MERGE-REGISTRY-SAFE")
    records = manager._load_registry()
    records[record.task_id].branch = "refs/heads/attacker"
    manager._save_registry(records)
    with pytest.raises(WorktreeError, match="unsafe"):
        manager.merge(record.task_id, force=True, actor="test-actor")


def test_cli_worktree_merge_force_is_wired_and_requires_actor(wt_repo):
    created = _run_cli("task", "worktree", "CLI-MERGE", cwd=wt_repo)
    assert created.returncode == 0, created.stderr
    tree = wt_repo.parent / "main-wt" / "CLI-MERGE"
    _commit_in(tree, "cli.txt", "cli merge work")

    refused = _run_cli("worktree", "merge", "CLI-MERGE", "--force", cwd=wt_repo)
    assert refused.returncode != 0
    assert "actor" in (refused.stdout + refused.stderr)
    assert tree.exists()

    merged = _run_cli(
        "worktree",
        "merge",
        "CLI-MERGE",
        "--force",
        "--actor",
        "cli-test",
        cwd=wt_repo,
    )
    assert merged.returncode == 0, merged.stdout + merged.stderr
    assert "Merged CLI-MERGE" in merged.stdout
    assert not tree.exists()


def test_cli_worktree_merge_without_verdict_prints_reference(wt_repo):
    created = _run_cli("task", "worktree", "CLI-NO-VERDICT", cwd=wt_repo)
    assert created.returncode == 0, created.stderr
    tree = wt_repo.parent / "main-wt" / "CLI-NO-VERDICT"
    _commit_in(tree, "cli.txt", "cli merge work")

    refused = _run_cli("worktree", "merge", "CLI-NO-VERDICT", cwd=wt_repo)

    assert refused.returncode != 0
    assert ".gitreins/history" in refused.stderr
    assert tree.exists()


# ── DF-GITREINS-POC-47 — the clean-tree gate vs GitReins' own runtime files ──


#: Every runtime file an ordinary GitReins run leaves in the canonical main
#: checkout.  ``worktrees.json``/``worktrees.lock`` come from this module's
#: registry, ``disposable.json``/``disposable.lock`` from the disposable
#: verifier (``engine/worktree_disposable.py``: ``DISPOSABLE_FILE`` /
#: ``DISPOSABLE_LOCK``), and ``tasks.yaml.lock`` from the task store's flock
#: sidecar (``engine/task_manager.py``).
RUNTIME_ARTIFACTS_IN_MAIN = (
    ".gitreins/worktrees.json",
    ".gitreins/worktrees.lock",
    ".gitreins/disposable.json",
    ".gitreins/disposable.lock",
    ".gitreins/tasks.yaml.lock",
)


def _write_runtime_artifacts(repo: Path, names=RUNTIME_ARTIFACTS_IN_MAIN) -> None:
    """Drop the runtime files an ordinary GitReins run leaves behind.

    Never clobbers an existing file: ``worktrees.json`` is the live registry,
    and overwriting it would erase real state instead of simulating dirt.
    """
    for name in names:
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_text("", encoding="utf-8")


def _porcelain_status(repo: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain", "--untracked-files=all"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def _linked_venv_manager(wt_repo: Path, task_id: str = "VENV-TREE"):
    """A manager whose create() symlinks a shared venv into the task tree.

    This is the production shape: ``_link_venv`` points the tree's configured
    venv name at an existing checkout, so an untracked ``.venv`` in a task
    worktree is harness-created, not leftover work.
    """
    (wt_repo / "shared-env").mkdir(exist_ok=True)
    manager = WorktreeManager(wt_repo, venv_source="shared-env")
    record, _ = manager.create(task_id)
    return manager, Path(record.path)


def test_is_clean_exempts_every_runtime_file_gitreins_writes_in_main(wt_repo: Path):
    """DF-GITREINS-POC-47: harness runtime files are never uncommitted work.

    A stock consumer install generates only the install-time gitignore entries,
    and ``_is_clean``'s exemption set was hand-kept alongside them; both missed
    the disposable verifier's registry + lock and the task store's lock.  The
    only dirt an ordinary run left behind therefore read as user work and the
    fleet merge refused with "canonical main has uncommitted changes".
    """
    manager = WorktreeManager(wt_repo)
    assert manager._is_clean(wt_repo) is True

    _write_runtime_artifacts(wt_repo)

    status = _porcelain_status(wt_repo)
    for name in RUNTIME_ARTIFACTS_IN_MAIN:
        assert name in status, f"premise: {name} must reach `git status` as dirt"

    assert manager._is_clean(wt_repo) is True


def test_is_clean_exempts_the_linked_venv_inside_a_task_worktree(wt_repo: Path):
    """The tree gate must survive the venv ``_link_venv`` puts there by design."""
    manager, tree = _linked_venv_manager(wt_repo)

    assert (tree / ".venv").is_symlink(), "premise: create() links the shared venv"
    assert manager._is_clean(tree) is True

    # The configured guard (`uv run pytest`) regenerates the interpreter inside
    # the tree, replacing the symlink with a real directory, and refreshes the
    # uv lockfile next to it.  A consumer whose .gitignore predates the venv
    # cannot commit either one.
    (tree / ".venv").unlink()
    (tree / ".venv" / "bin").mkdir(parents=True)
    (tree / ".venv" / "bin" / "python").write_text("", encoding="utf-8")
    (tree / "uv.lock").write_text("", encoding="utf-8")
    (tree / ".uv.lock").write_text("", encoding="utf-8")
    _write_runtime_artifacts(tree, names=(".gitreins/disposable.json",))

    assert ".venv/bin/python" in _porcelain_status(tree)
    assert manager._is_clean(tree) is True


def test_is_clean_still_counts_an_untracked_venv_in_canonical_main(wt_repo: Path):
    """The venv exemption is scoped to task trees — main stays honest.

    In canonical main an untracked .venv is the consumer's own uncommitted
    state (their repo never gitignored it); only the harness's own symlink
    inside a task worktree is exempt.
    """
    (wt_repo / ".venv" / "bin").mkdir(parents=True)
    (wt_repo / ".venv" / "bin" / "python").write_text("", encoding="utf-8")
    (wt_repo / "uv.lock").write_text("", encoding="utf-8")

    assert WorktreeManager(wt_repo)._is_clean(wt_repo) is False


def test_is_clean_still_refuses_real_work_in_canonical_main(wt_repo: Path):
    """Regression guard against over-exemption: user work is still dirt."""
    manager = WorktreeManager(wt_repo)
    _write_runtime_artifacts(wt_repo)
    assert manager._is_clean(wt_repo) is True

    (wt_repo / "foo.txt").write_text("work in progress\n", encoding="utf-8")
    assert manager._is_clean(wt_repo) is False

    (wt_repo / "foo.txt").unlink()
    (wt_repo / "base.txt").write_text("edited\n", encoding="utf-8")
    assert manager._is_clean(wt_repo) is False

    # A runtime-looking path OUTSIDE the harness store is not exempt either.
    (wt_repo / "base.txt").write_text("base\n", encoding="utf-8")
    (wt_repo / "notes.txt.lock").write_text("", encoding="utf-8")
    assert manager._is_clean(wt_repo) is False


def test_is_clean_still_refuses_real_work_inside_a_task_worktree(wt_repo: Path):
    manager, tree = _linked_venv_manager(wt_repo, "DIRTY-TREE")
    assert manager._is_clean(tree) is True

    (tree / "foo.txt").write_text("work in progress\n", encoding="utf-8")
    assert manager._is_clean(tree) is False

    (tree / "foo.txt").unlink()
    (tree / "base.txt").write_text("edited in tree\n", encoding="utf-8")
    assert manager._is_clean(tree) is False

    # User files that merely sit inside .gitreins/ are still real work.
    (tree / "base.txt").write_text("base\n", encoding="utf-8")
    (tree / ".gitreins").mkdir(parents=True, exist_ok=True)
    (tree / ".gitreins" / "notes.md").write_text("mine\n", encoding="utf-8")
    assert manager._is_clean(tree) is False


def test_merge_proceeds_when_only_runtime_artifacts_are_present(wt_repo: Path):
    """End-to-end: the merge-back no longer refuses on harness bookkeeping."""
    manager = WorktreeManager(wt_repo)
    record, tree = _make_task_commit(manager, "MERGE-RUNTIME")
    _write_runtime_artifacts(wt_repo)

    result = manager.merge(record.task_id, force=True, actor="runtime-test")

    assert result["mode"] == "fast-forward"
    assert (wt_repo / "task.txt").read_text(encoding="utf-8") == "task.txt\n"
    assert not tree.exists()
