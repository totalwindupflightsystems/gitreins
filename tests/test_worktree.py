"""Tests for canonical board resolution across linked Git worktrees."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from engine.repo_paths import WorktreeResolutionError, board_file_path, resolve_worktree_paths
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
