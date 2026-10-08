"""
DF-GITREINS-POC-70: `gitreins commit` must refuse BEFORE the guard stage when
nothing is staged.

With an empty index the guard lanes all skip, the green "Tier 1 PASSED"
banner prints anyway, and `git commit` then fails with "nothing to commit" —
a fresh user reads a green gate, a red exit, and the actual cause sandwiched
in between. With unstaged-only edits the behavior is identical and nothing
hints that `git add` is the missing step.

These tests pin the prescribed fix: cmd_commit pre-checks HEAD's index before
any guard runs and refuses with targeted guidance (same early-refusal shape
as the DF-GITREINS-POC-66 in-progress check), while a normal staged commit
keeps byte-identical guard/banner behavior.
"""

import argparse
import subprocess
from pathlib import Path

import pytest

from gitreins.cli import cmd_commit


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def _init_repo(tmp_path: Path) -> Path:
    """Real repo with one base commit that includes .gitreins/config.yaml.

    The config is COMMITTED (not left untracked like test_cli_commit_in_progress's
    helper): the clean-tree refusal case needs `git status --porcelain` to be
    empty, and an untracked config would report as dirty and flip the case.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "GitReins Tests")
    _git(repo, "config", "user.email", "tests@gitreins.local")
    (repo / "base.txt").write_text("base\n")
    cfg = repo / ".gitreins" / "config.yaml"
    cfg.parent.mkdir()
    cfg.write_text("guards:\n  test_command: echo ok\n  allow_skips: true\n")
    _git(repo, "add", "base.txt", ".gitreins/config.yaml")
    _git(repo, "commit", "-q", "-m", "base")
    return repo


def _commit_args(**overrides) -> argparse.Namespace:
    defaults = dict(message="poc-70 commit", skip_tier2=True, allow_in_progress=False)
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


class _StubTier1:
    passed = True
    summary = "stub tier 1"


def _make_guard_stub():
    """Fresh GuardManager stub per test; `constructed` proves guard (non-)entry.

    A per-test class (not a module-level singleton) keeps the construction
    flag from leaking between tests.
    """

    class _GuardManagerStub:
        constructed = False

        def __init__(self, workdir, config=None):
            type(self).constructed = True

        def run_all(self):
            return _StubTier1()

    return _GuardManagerStub


def _assert_no_commit_created(repo: Path, head_before: str, count_before: str) -> None:
    assert _git(repo, "rev-parse", "HEAD") == head_before
    # No commit object reachable: rev-list count is the object-level witness.
    assert _git(repo, "rev-list", "--count", "HEAD") == count_before


def test_commit_refuses_when_index_empty_and_tree_clean(tmp_path, monkeypatch, capsys):
    """AC 1: clean tree + empty index — guidance, exit 1, no guards, no commit."""
    repo = _init_repo(tmp_path)
    head_before = _git(repo, "rev-parse", "HEAD")
    count_before = _git(repo, "rev-list", "--count", "HEAD")
    monkeypatch.chdir(repo)
    guard_stub = _make_guard_stub()
    monkeypatch.setattr("engine.guard_manager.GuardManager", guard_stub)

    with pytest.raises(SystemExit) as excinfo:
        cmd_commit(_commit_args())

    assert excinfo.value.code == 1
    captured = capsys.readouterr()
    assert "Nothing to commit" in captured.err
    assert "git add" in captured.err
    # Short-circuit proof: the green banner never printed and guards never ran.
    assert "Tier 1" not in captured.out
    assert "Tier 1" not in captured.err
    assert guard_stub.constructed is False
    _assert_no_commit_created(repo, head_before, count_before)


def test_commit_refuses_when_only_unstaged_edits(tmp_path, monkeypatch, capsys):
    """AC 2: unstaged-only edits — the hint names `git add`, guards not run."""
    repo = _init_repo(tmp_path)
    (repo / "base.txt").write_text("modified but never staged\n")
    head_before = _git(repo, "rev-parse", "HEAD")
    count_before = _git(repo, "rev-list", "--count", "HEAD")
    monkeypatch.chdir(repo)
    guard_stub = _make_guard_stub()
    monkeypatch.setattr("engine.guard_manager.GuardManager", guard_stub)

    with pytest.raises(SystemExit) as excinfo:
        cmd_commit(_commit_args())

    assert excinfo.value.code == 1
    captured = capsys.readouterr()
    assert "Nothing staged" in captured.err
    assert "git add" in captured.err
    assert "Tier 1" not in captured.out
    assert "Tier 1" not in captured.err
    assert guard_stub.constructed is False
    _assert_no_commit_created(repo, head_before, count_before)
    # The unstaged edit must still be sitting in the working tree, untouched.
    status = _git(repo, "status", "--porcelain")
    assert "M base.txt" in status
    assert (repo / "base.txt").read_text() == "modified but never staged\n"


def test_commit_refuses_when_only_untracked_files(tmp_path, monkeypatch, capsys):
    """AC 2 (untracked arm): untracked files alone must also get the git add hint."""
    repo = _init_repo(tmp_path)
    (repo / "untracked.txt").write_text("never added\n")
    head_before = _git(repo, "rev-parse", "HEAD")
    count_before = _git(repo, "rev-list", "--count", "HEAD")
    monkeypatch.chdir(repo)
    guard_stub = _make_guard_stub()
    monkeypatch.setattr("engine.guard_manager.GuardManager", guard_stub)

    with pytest.raises(SystemExit) as excinfo:
        cmd_commit(_commit_args())

    assert excinfo.value.code == 1
    captured = capsys.readouterr()
    assert "Nothing staged" in captured.err
    assert "git add" in captured.err
    assert "Tier 1" not in captured.out
    assert "Tier 1" not in captured.err
    assert guard_stub.constructed is False
    _assert_no_commit_created(repo, head_before, count_before)
    assert "??" in _git(repo, "status", "--porcelain")


def test_commit_with_staged_changes_unchanged_behavior(tmp_path, monkeypatch, capsys):
    """AC 3 control: a staged change keeps today's path — guards run, banner prints."""
    repo = _init_repo(tmp_path)
    (repo / "staged.txt").write_text("staged\n")
    _git(repo, "add", "staged.txt")
    head_before = _git(repo, "rev-parse", "HEAD")
    monkeypatch.chdir(repo)
    guard_stub = _make_guard_stub()
    monkeypatch.setattr("engine.guard_manager.GuardManager", guard_stub)

    cmd_commit(_commit_args())

    captured = capsys.readouterr()
    assert guard_stub.constructed is True  # guards really ran on this path
    assert "Tier 1 PASSED" in captured.out
    assert "Nothing staged" not in captured.err
    assert "Nothing to commit" not in captured.err
    head_after = _git(repo, "rev-parse", "HEAD")
    assert head_after != head_before
    assert "staged.txt" in _git(repo, "show", "--name-only", "--format=", "HEAD")


def test_partially_staged_file_commits_like_git(tmp_path, monkeypatch, capsys):
    """Boundary: a staged file with FURTHER unstaged edits still commits.

    Matches `git commit` semantics (the index is committed as-is): a
    non-empty index takes the normal guarded path even though the working
    tree is dirty. This is exactly the case AC 2's setup must NOT create —
    a staged-then-re-edited file has a NON-empty index.
    """
    repo = _init_repo(tmp_path)
    (repo / "staged.txt").write_text("staged version\n")
    _git(repo, "add", "staged.txt")
    (repo / "staged.txt").write_text("further unstaged edit\n")
    head_before = _git(repo, "rev-parse", "HEAD")
    monkeypatch.chdir(repo)
    guard_stub = _make_guard_stub()
    monkeypatch.setattr("engine.guard_manager.GuardManager", guard_stub)

    cmd_commit(_commit_args())

    captured = capsys.readouterr()
    assert guard_stub.constructed is True
    assert "Tier 1 PASSED" in captured.out
    assert "Nothing staged" not in captured.err
    head_after = _git(repo, "rev-parse", "HEAD")
    assert head_after != head_before
    # The commit carries the staged ("staged version") blob, like git commit.
    committed = _git(repo, "show", "HEAD:staged.txt")
    assert committed == "staged version"
