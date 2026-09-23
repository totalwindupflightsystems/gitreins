"""Integration coverage for scripts/check_unpushed.sh."""

import os
import subprocess
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "check_unpushed.sh"


def _git(cwd, *args, capture_output=True):
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=capture_output,
        text=True,
    )


def _commit_identity_env():
    env = os.environ.copy()
    env.update(
        {
            "GIT_AUTHOR_NAME": "Guard Test",
            "GIT_AUTHOR_EMAIL": "guard-test@example.invalid",
            "GIT_COMMITTER_NAME": "Guard Test",
            "GIT_COMMITTER_EMAIL": "guard-test@example.invalid",
        }
    )
    return env


def _commit(cwd, message):
    subprocess.run(
        ["git", "commit", "-q", "-m", message],
        cwd=cwd,
        check=True,
        env=_commit_identity_env(),
    )


def _run_guard(cwd):
    return subprocess.run(
        [str(SCRIPT_PATH)],
        cwd=cwd,
        capture_output=True,
        text=True,
    )


def test_guard_distinguishes_content_from_identical_merge_history(tmp_path):
    """A content commit alarms, while merge-only history with equal trees passes."""
    remote = tmp_path / "remote.git"
    clone = tmp_path / "clone"
    _git(tmp_path, "init", "--bare", "--initial-branch=main", str(remote))
    subprocess.run(
        ["git", "clone", "-q", str(remote), str(clone)],
        check=True,
        env=_commit_identity_env(),
    )
    _git(clone, "config", "user.name", "Guard Test")
    _git(clone, "config", "user.email", "guard-test@example.invalid")

    (clone / "tracked.txt").write_text("base\n", encoding="utf-8")
    _git(clone, "add", "tracked.txt")
    _commit(clone, "base commit")
    subprocess.run(
        ["git", "commit", "--allow-empty", "-q", "-m", "upstream history"],
        cwd=clone,
        check=True,
        env=_commit_identity_env(),
    )
    _git(clone, "push", "-q", "-u", "origin", "main")

    (clone / "tracked.txt").write_text("content\n", encoding="utf-8")
    _git(clone, "add", "tracked.txt")
    _commit(clone, "content commit")
    content_result = _run_guard(clone)
    assert content_result.returncode == 1, content_result.stdout + content_result.stderr
    assert "ALARM unpushed-content" in content_result.stdout
    assert "content commit" in content_result.stdout
    assert "WARN dirty-tree" not in content_result.stdout

    # Return to the pushed tree, then create eleven merge commits whose trees
    # are identical to their first parent. This mirrors the repo's mirror/main
    # artifacts without adding non-merge content commits.
    _git(clone, "reset", "-q", "--hard", "origin/main")
    for index in range(11):
        first_parent = _git(clone, "rev-parse", "HEAD").stdout.strip()
        second_parent = _git(clone, "rev-parse", "HEAD^").stdout.strip()
        tree = _git(clone, "rev-parse", "HEAD^{tree}").stdout.strip()
        merge_sha = subprocess.run(
            [
                "git",
                "commit-tree",
                tree,
                "-p",
                first_parent,
                "-p",
                second_parent,
                "-m",
                f"Merge branch mirror/main into main ({index + 1})",
            ],
            cwd=clone,
            check=True,
            capture_output=True,
            text=True,
            env=_commit_identity_env(),
        ).stdout.strip()
        _git(clone, "update-ref", "refs/heads/main", merge_sha, first_parent)

    merge_result = _run_guard(clone)
    assert merge_result.returncode == 0, merge_result.stdout + merge_result.stderr
    assert "PASS unpushed-content" in merge_result.stdout
    assert "11 total commit(s) ahead" in merge_result.stdout
    assert "0 non-merge content commit(s)" in merge_result.stdout
    assert "0 changed path(s)" in merge_result.stdout

    (clone / "tracked.txt").write_text("work in progress\n", encoding="utf-8")
    dirty_result = _run_guard(clone)
    assert dirty_result.returncode == 0, dirty_result.stdout + dirty_result.stderr
    assert "PASS unpushed-content" in dirty_result.stdout
    assert "WARN dirty-tree" in dirty_result.stdout
    assert "tracked.txt" in dirty_result.stdout
