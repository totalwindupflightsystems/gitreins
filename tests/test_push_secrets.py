"""Push-time secret scanning tests over committed history."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from engine.push_secrets import check_push_range, scan_push_range


def _git(path: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=path, text=True, capture_output=True, check=True)
    return result.stdout.strip()


def _repo(path: Path) -> None:
    path.mkdir(exist_ok=True)
    _git(path, "init", "-q", "--initial-branch=main")
    _git(path, "config", "user.name", "Push test")
    _git(path, "config", "user.email", "push-test@example.invalid")


def _commit(path: Path, name: str, value: str) -> str:
    (path / name).write_text(value, encoding="utf-8")
    _git(path, "add", name)
    env = os.environ.copy()
    env.update(
        GIT_AUTHOR_NAME="Push test",
        GIT_AUTHOR_EMAIL="push-test@example.invalid",
        GIT_COMMITTER_NAME="Push test",
        GIT_COMMITTER_EMAIL="push-test@example.invalid",
    )
    subprocess.run(["git", "commit", "-q", "-m", "fixture"], cwd=path, env=env, check=True)
    return _git(path, "rev-parse", "HEAD")


def test_push_range_detects_secret_in_existing_commit(tmp_path, capsys):
    repo = tmp_path / "repo"
    _repo(repo)
    base = _commit(repo, "safe.txt", "safe\n")
    fake_token = "ghp_" + "FAKEKEYnotreal" + "0000000000000000001"
    head = _commit(repo, "notes.txt", f"token={fake_token}\n")

    assert scan_push_range(str(repo), head, base) == ["GitHub personal access token"]
    assert check_push_range(str(repo), head, base) == 1
    output = capsys.readouterr().out
    assert "PUSH REFUSED: GitHub personal access token" in output
    assert "Rotate the exposed credential" in output
    assert fake_token not in output


def test_push_range_accepts_clean_and_initial_push(tmp_path):
    repo = tmp_path / "repo"
    _repo(repo)
    head = _commit(repo, "safe.txt", "ordinary content\n")
    assert scan_push_range(str(repo), head, "0" * 40) == []


def test_push_range_catches_secret_removed_later_in_outgoing_history(tmp_path):
    repo = tmp_path / "repo"
    _repo(repo)
    base = _commit(repo, "base.txt", "safe\n")
    fake_token = "ghp_" + "FAKEKEYnotreal" + "0000000000000000001"
    _commit(repo, "secret.txt", fake_token + "\n")
    (repo / "secret.txt").unlink()
    _git(repo, "add", "-u")
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Push test",
        "GIT_AUTHOR_EMAIL": "push-test@example.invalid",
        "GIT_COMMITTER_NAME": "Push test",
        "GIT_COMMITTER_EMAIL": "push-test@example.invalid",
    }
    subprocess.run(["git", "commit", "-q", "-m", "remove token"], cwd=repo, env=env, check=True)
    head = _git(repo, "rev-parse", "HEAD")
    assert scan_push_range(str(repo), head, base) == ["GitHub personal access token"]


def test_installer_emits_executable_pre_push_hook(tmp_path, monkeypatch, capsys):
    from gitreins import cli

    _repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    cli.cmd_install(None)
    hook = tmp_path / ".git" / "hooks" / "pre-push"
    assert hook.is_file() and os.access(hook, os.X_OK)
    text = hook.read_text()
    assert "while read local_ref local_sha remote_ref remote_sha" in text
    assert "push-check" in text
    assert "Created:" in capsys.readouterr().out


def test_pre_push_hook_refuses_legacy_secret_commit(tmp_path, monkeypatch):
    from gitreins import cli

    repo = tmp_path / "repo"
    remote = tmp_path / "remote.git"
    _repo(repo)
    _git(tmp_path, "init", "-q", "--bare", "--initial-branch=main", str(remote))
    (repo / "safe.txt").write_text("base\\n", encoding="utf-8")
    _git(repo, "add", "safe.txt")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "-q", "-u", "origin", "main")
    base = _git(repo, "rev-parse", "HEAD")

    fake_token = "ghp_" + "FAKEKEYnotreal" + "0000000000000000001"
    (repo / "legacy.txt").write_text(fake_token + "\\n", encoding="utf-8")
    _git(repo, "add", "legacy.txt")
    _git(repo, "commit", "-q", "-m", "legacy secret commit")
    head = _git(repo, "rev-parse", "HEAD")

    monkeypatch.chdir(repo)
    cli.cmd_install(None)
    push_env = os.environ.copy()
    push_env["PYTHONPATH"] = (
        str(Path(__file__).resolve().parents[1]) + os.pathsep + push_env.get("PYTHONPATH", "")
    )
    push = subprocess.run(
        ["git", "push", "origin", "main"],
        cwd=repo,
        env=push_env,
        text=True,
        capture_output=True,
        check=False,
    )
    output = push.stdout + push.stderr
    print(output)
    assert push.returncode != 0
    assert "PUSH REFUSED: GitHub personal access token" in output
    assert "Rotate the exposed credential" in output
    assert fake_token not in output
    assert _git(remote, "rev-parse", "refs/heads/main") == base
    assert _git(repo, "rev-parse", "HEAD") == head
