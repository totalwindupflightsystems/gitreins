"""Working-tree content fingerprint contract (GR-153)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from engine.fingerprint import compute


REPO_ROOT = Path(__file__).resolve().parents[1]
CLI_SCRIPT = REPO_ROOT / "gitreins" / "cli.py"


def _git_env() -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(
        GIT_AUTHOR_NAME="Fingerprint Test",
        GIT_AUTHOR_EMAIL="fingerprint@example.invalid",
        GIT_COMMITTER_NAME="Fingerprint Test",
        GIT_COMMITTER_EMAIL="fingerprint@example.invalid",
    )
    return env


def _git(repo: Path, *args: str, input_bytes: bytes | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        env=_git_env(),
        input=input_bytes,
        capture_output=True,
        check=True,
    )


def _repo(tmp_path: Path, *, commit: bool = True) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    if commit:
        (repo / "tracked.txt").write_bytes(b"original\n")
        _git(repo, "add", "tracked.txt")
        _git(repo, "commit", "-qm", "initial")
    return repo


def _assert_authoritative(repo: Path):
    result = compute(repo)
    assert result.state == "authoritative"
    assert len(result.digest) == 64
    int(result.digest, 16)
    return result


def test_staging_does_not_change_the_digest(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "tracked.txt").write_bytes(b"modified\n")
    before = _assert_authoritative(repo)

    _git(repo, "add", "tracked.txt")
    after = _assert_authoritative(repo)

    assert after.digest == before.digest
    assert after.dirty_count == before.dirty_count == 1


def test_digest_is_independent_of_head_when_workspace_bytes_are_identical(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    before = _assert_authoritative(repo)

    _git(repo, "commit", "--allow-empty", "-qm", "different tip")
    after = _assert_authoritative(repo)

    assert _git(repo, "rev-list", "--count", "HEAD").stdout.strip() == b"2"
    assert after.digest == before.digest


def test_one_byte_edit_changes_the_digest(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    before = _assert_authoritative(repo)

    (repo / "tracked.txt").write_bytes(b"original?")

    assert _assert_authoritative(repo).digest != before.digest


def test_no_head_repo_is_valid(tmp_path: Path) -> None:
    repo = _repo(tmp_path, commit=False)
    (repo / "new.txt").write_bytes(b"unborn branch\n")

    result = _assert_authoritative(repo)

    assert result.dirty_count == 1


def test_dirty_count_counts_one_modified_tracked_path(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "tracked.txt").write_bytes(b"dirty\n")

    assert _assert_authoritative(repo).dirty_count == 1


def test_autocrlf_refuses_with_named_state(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _git(repo, "config", "core.autocrlf", "true")

    assert compute(repo).state == "autocrlf"


def test_sparse_checkout_refuses_with_named_state(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _git(repo, "config", "core.sparseCheckout", "true")

    assert compute(repo).state == "sparse-checkout"


def test_assume_unchanged_refuses_with_named_state(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _git(repo, "update-index", "--assume-unchanged", "tracked.txt")

    assert compute(repo).state == "assume-unchanged"


def test_gitlink_refuses_with_named_state(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    blob = _git(repo, "rev-parse", "HEAD").stdout.strip().decode("ascii")
    _git(repo, "update-index", "--add", "--cacheinfo", f"160000,{blob},submodule")

    assert compute(repo).state == "gitlink"


def test_working_tree_encoding_refuses_with_named_state(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / ".gitattributes").write_text("tracked.txt working-tree-encoding=UTF-16\n")

    assert compute(repo).state == "working-tree-encoding"


def test_active_filter_refuses_with_named_state(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / ".gitattributes").write_text("tracked.txt filter=fixture\n")
    _git(repo, "config", "filter.fixture.clean", "cat")

    assert compute(repo).state == "active-filter"


def test_staging_an_untracked_file_does_not_change_the_digest(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "new.txt").write_bytes(b"new content\n")
    before = _assert_authoritative(repo)

    _git(repo, "add", "new.txt")

    assert _assert_authoritative(repo).digest == before.digest


def test_staging_a_deletion_does_not_change_the_digest(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "tracked.txt").unlink()
    before = _assert_authoritative(repo)

    _git(repo, "add", "-u")

    assert _assert_authoritative(repo).digest == before.digest


def test_split_index_refuses_with_named_state(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _git(repo, "update-index", "--split-index")

    assert compute(repo).state == "split-index"


def test_skip_worktree_refuses_with_named_state(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _git(repo, "update-index", "--skip-worktree", "tracked.txt")

    assert compute(repo).state == "skip-worktree"


def test_active_textconv_refuses_with_named_state(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / ".gitattributes").write_text("tracked.txt diff=fixture\n")
    _git(repo, "config", "diff.fixture.textconv", "cat")

    assert compute(repo).state == "active-textconv"


def test_nested_repo_refuses_with_named_state(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    nested = repo / "nested"
    nested.mkdir()
    _git(nested, "init", "-q")
    (nested / "nested.txt").write_text("nested\n")

    assert compute(repo).state == "nested-repository"


def test_non_sha1_repo_refuses_with_named_state(tmp_path: Path) -> None:
    repo = tmp_path / "sha256-repo"
    repo.mkdir()
    _git(repo, "init", "-q", "--object-format=sha256")

    assert compute(repo).state == "non-sha1-object-format"


def test_zero_deadline_refuses_with_timeout_state(tmp_path: Path) -> None:
    repo = _repo(tmp_path)

    assert compute(repo, timeout_s=0).state == "timeout"


def test_unreadable_visible_file_refuses_with_named_state(tmp_path: Path) -> None:
    if os.geteuid() == 0:
        pytest.skip("root can read mode-000 files")
    repo = _repo(tmp_path)
    path = repo / "tracked.txt"
    path.chmod(0)
    try:
        assert compute(repo).state == "unreadable-path"
    finally:
        path.chmod(0o644)


def test_cli_json_emits_only_the_public_fingerprint_shape(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "tracked.txt").write_bytes(b"cli dirty\n")
    env = _git_env()
    env["PYTHONPATH"] = str(REPO_ROOT)

    proc = subprocess.run(
        [sys.executable, str(CLI_SCRIPT), "fingerprint", "--json"],
        cwd=repo,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert set(payload) == {"digest", "dirty_count", "state"}
    assert len(payload["digest"]) == 64
    assert payload["dirty_count"] == 1
    assert payload["state"] == "authoritative"

    human = subprocess.run(
        [sys.executable, str(CLI_SCRIPT), "fingerprint"],
        cwd=repo,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert human.returncode == 0, human.stderr
    assert human.stdout == (f"fingerprint: {payload['digest']} (dirty=1, state=authoritative)\n")


def test_cli_non_authoritative_state_is_named_and_nonzero(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _git(repo, "config", "core.autocrlf", "true")
    env = _git_env()
    env["PYTHONPATH"] = str(REPO_ROOT)

    proc = subprocess.run(
        [sys.executable, str(CLI_SCRIPT), "fingerprint", "--json"],
        cwd=repo,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert proc.returncode != 0
    assert json.loads(proc.stdout)["state"] == "autocrlf"


def test_verdict_json_persists_digest_and_count_only(tmp_path: Path) -> None:
    from engine.judge import JudgeResult
    from engine.persist import persist_evaluation
    from engine.task_manager import Task

    repo = _repo(tmp_path)
    (repo / "tracked.txt").write_bytes(b"verdict dirty\n")
    config_dir = repo / ".gitreins"
    config_dir.mkdir()
    (config_dir / "config.yaml").write_text(
        "history:\n  storage: filesystem\n  max_verdicts: 0\n",
        encoding="utf-8",
    )
    task = Task(id="GR-153", title="fingerprint", criteria=[])
    result = JudgeResult(task_id="GR-153", passed=True)

    persist_evaluation(str(repo), task, result)
    verdict_files = list((config_dir / "history").glob("*/*/verdict.json"))
    assert len(verdict_files) == 1
    data = json.loads(verdict_files[0].read_text(encoding="utf-8"))

    assert len(data["working_tree_content_fingerprint"]) == 64
    assert data["working_tree_dirty_path_count"] == 2
    assert "working_tree_fingerprint_state" not in data
    assert "working_tree_paths" not in data
    assert "working_tree_bytes" not in data
