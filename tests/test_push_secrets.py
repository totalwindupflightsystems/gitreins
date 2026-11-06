"""Push-time secret scanning tests over committed history."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from engine.push_secrets import (
    _load_gitleaks_path_allowlist,
    _looks_like_placeholder,
    check_push_range,
    scan_push_range,
)


def _git(path: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=path, text=True, capture_output=True, check=True)
    return result.stdout.strip()


def _repo(path: Path) -> None:
    path.mkdir(exist_ok=True)
    _git(path, "init", "-q", "--initial-branch=main")
    _git(path, "config", "user.name", "Push test")
    _git(path, "config", "user.email", "push-test@example.invalid")


def _commit(path: Path, name: str, value: str) -> str:
    (path / name).parent.mkdir(parents=True, exist_ok=True)
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


# Secret-shaped fixture constants. The pieces stay in separate variables on
# purpose, twice over: (1) a single committed line that glues a token prefix
# to its body in one expression is itself refused by `gitreins push-check`
# (the DF-026 concat-glue rule), so the pre-push hook would block every push
# that carries this file; (2) GR-GAP-066 (C) treats repeated-block or
# long-run bodies as placeholder fixtures, so every body below avoids both
# shapes and stays scanner-detectable as a real-shaped credential.
_FAKE_GHP_PREFIX = "ghp_"
_FAKE_GHP_BODY = "FaK3n0tReaL7x2Qm5Vb9Ld4Wp8Zc1j6Ht3s"
_FAKE_SK_PREFIX = "sk-"
_FAKE_SK_BODY = "vN4pX8kR2tW6yB3cF5jH9dQ1sG7mZ0aL"
_PRIVATE_KEY_OPEN = "-----BEGIN RSA "
_PRIVATE_KEY_OPEN_CONT = "PRIVATE KEY-----"
_PRIVATE_KEY_CLOSE = "-----END RSA PRIVATE KEY-----"
_HEX_PART_A = "3f9a1c7e5b2d8406"
_HEX_PART_B = "a9e3c1f7b5d2084e"
_ASSIGN_VALUE_A = "A1b2C3d4"
_ASSIGN_VALUE_B = "E5f6G7h8I9j0KlMn"


def _fake_github_token() -> str:
    """Build a scanner-detectable token at runtime from separate constants."""
    return _FAKE_GHP_PREFIX + _FAKE_GHP_BODY


def test_push_range_detects_secret_in_existing_commit(tmp_path, capsys):
    repo = tmp_path / "repo"
    _repo(repo)
    base = _commit(repo, "safe.txt", "safe\n")
    fake_token = _fake_github_token()
    head = _commit(repo, "notes.txt", f"token={fake_token}\n")

    assert scan_push_range(str(repo), head, base) == ["GitHub personal access token"]
    assert check_push_range(str(repo), head, base) == 1
    output = capsys.readouterr().out
    assert "PUSH REFUSED: GitHub personal access token" in output
    assert "Rotate the exposed credential" in output
    assert fake_token not in output


def test_push_range_detects_secret_in_split_concatenation(tmp_path):
    repo = tmp_path / "repo"
    _repo(repo)
    base = _commit(repo, "safe.txt", "safe\n")
    fake_body = "FaK3n0tReaL" + "7x2Qm5Vb9Ld4Wp8Zc1j6Ht3s"
    head = _commit(repo, "notes.txt", 'API_TOKEN = "ghp_" + "' + fake_body + '"\n')

    findings = scan_push_range(str(repo), head, base)
    assert findings, "split-concatenated credential must be detected"
    assert any("GitHub personal access token" in finding for finding in findings)


def test_push_range_detects_secret_in_joined_literal(tmp_path):
    repo = tmp_path / "repo"
    _repo(repo)
    base = _commit(repo, "safe.txt", "safe\n")
    fake_token = _fake_github_token()
    head = _commit(repo, "notes.txt", "API_TOKEN = " + repr(fake_token) + "\n")

    findings = scan_push_range(str(repo), head, base)
    assert findings == ["GitHub personal access token"]


def test_push_range_accepts_clean_and_initial_push(tmp_path):
    repo = tmp_path / "repo"
    _repo(repo)
    head = _commit(repo, "safe.txt", "ordinary content\n")
    assert scan_push_range(str(repo), head, "0" * 40) == []


def test_push_range_catches_secret_removed_later_in_outgoing_history(tmp_path):
    repo = tmp_path / "repo"
    _repo(repo)
    base = _commit(repo, "base.txt", "safe\n")
    fake_token = _fake_github_token()
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

    fake_token = _fake_github_token()
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


def test_zero_sha_scans_only_commits_not_on_remote(tmp_path, capsys):
    repo = tmp_path / "repo"
    _repo(repo)
    fake_token = _fake_github_token()
    base = _commit(repo, "fixture.txt", f"token={fake_token}\n")
    _git(repo, "update-ref", "refs/remotes/origin/main", base)
    head = _commit(repo, "notes.txt", "clean new branch work\n")

    assert scan_push_range(str(repo), head, "0" * 40) == []
    assert check_push_range(str(repo), head, "0" * 40) == 0
    output = capsys.readouterr().out
    assert f"scanned 1 commit in {head[:12]} --not --remotes" in output
    assert "scanned 2 commits" not in output


def test_zero_sha_refuses_credential_on_new_branch(tmp_path, capsys):
    repo = tmp_path / "repo"
    _repo(repo)
    base = _commit(repo, "safe.txt", "safe\n")
    _git(repo, "update-ref", "refs/remotes/origin/main", base)
    fake_token = _fake_github_token()
    head = _commit(repo, "leak.txt", f"token={fake_token}\n")

    findings = scan_push_range(str(repo), head, "0" * 40)
    assert findings, "credential on the new branch must be detected"
    assert any("GitHub personal access token" in finding for finding in findings)
    assert check_push_range(str(repo), head, "0" * 40) == 1
    output = capsys.readouterr().out
    assert "PUSH REFUSED: GitHub personal access token" in output
    assert f"Scanned 1 commit in {head[:12]} --not --remotes" in output
    assert "Rotate the exposed credential" in output
    assert fake_token not in output


def test_nonzero_remote_message_reports_scope(tmp_path, capsys):
    repo = tmp_path / "repo"
    _repo(repo)
    base = _commit(repo, "safe.txt", "safe\n")
    fake_token = _fake_github_token()
    head = _commit(repo, "leak.txt", f"token={fake_token}\n")

    assert check_push_range(str(repo), head, base) == 1
    output = capsys.readouterr().out
    assert f"Scanned 1 commit in {base[:12]}..{head[:12]}" in output
    assert check_push_range(str(repo), base, base) == 0
    output = capsys.readouterr().out
    assert f"PUSH SECRET CHECK: clean (scanned 0 commits in {base[:12]}..{base[:12]})" in output


def test_deleted_ref_push_is_refused_fail_closed(tmp_path, capsys):
    repo = tmp_path / "repo"
    _repo(repo)
    base = _commit(repo, "safe.txt", "safe\n")
    _commit(repo, "more.txt", "more\n")

    assert check_push_range(str(repo), "0" * 40, base) == 1
    output = capsys.readouterr().out
    assert "could not verify the outgoing history" in output
    assert "Invalid revision range" in output
    with pytest.raises(RuntimeError, match="Invalid revision range"):
        scan_push_range(str(repo), "0" * 40, base)


def test_push_range_allowlists_coding_hermes_fixture_paths(tmp_path, capsys):
    repo = tmp_path / "repo"
    _repo(repo)
    base = _commit(repo, "safe.txt", "safe\n")
    fake_token = _fake_github_token()
    fixture_line = '{"id": "DF-026", "repro": "' + fake_token + '"}\n'
    head = _commit(repo, ".coding-hermes/board/tasks.jsonl", fixture_line)

    assert scan_push_range(str(repo), head, base) == []
    assert check_push_range(str(repo), head, base) == 0
    output = capsys.readouterr().out
    assert "PUSH SECRET CHECK: clean" in output
    assert "allowlisted paths skipped: 1 file" in output
    assert fake_token not in output


def test_push_range_still_refuses_identical_token_outside_allowlist(tmp_path, capsys):
    repo = tmp_path / "repo"
    _repo(repo)
    base = _commit(repo, "safe.txt", "safe\n")
    fake_token = _fake_github_token()
    head = _commit(repo, "engine/notes.py", f'token = "{fake_token}"\n')

    assert scan_push_range(str(repo), head, base) == ["GitHub personal access token"]
    assert check_push_range(str(repo), head, base) == 1
    output = capsys.readouterr().out
    assert "PUSH REFUSED: GitHub personal access token" in output
    assert "allowlisted paths skipped" not in output
    assert fake_token not in output


def test_push_range_allowlist_is_path_scoped_not_content_scoped(tmp_path, capsys):
    repo = tmp_path / "repo"
    _repo(repo)
    base = _commit(repo, "safe.txt", "safe\n")
    fake_token = _fake_github_token()
    fixture = repo / ".coding-hermes" / "board" / "tasks.jsonl"
    fixture.parent.mkdir(parents=True, exist_ok=True)
    fixture.write_text("repro: " + fake_token + "\n", encoding="utf-8")
    _git(repo, "add", ".coding-hermes/board/tasks.jsonl")
    # _commit stages engine/notes.py and commits every staged path, so both the
    # allowlisted fixture and the source token land in ONE commit.
    head = _commit(repo, "engine/notes.py", f'token = "{fake_token}"\n')

    assert scan_push_range(str(repo), head, base) == ["GitHub personal access token"]
    assert check_push_range(str(repo), head, base) == 1
    output = capsys.readouterr().out
    assert "PUSH REFUSED: GitHub personal access token" in output
    assert "allowlisted paths skipped" not in output
    assert fake_token not in output


def test_credential_assignment_ignores_call_expression_values(tmp_path):
    """A source line whose value is a CALL is code, not a credential literal."""
    repo = tmp_path / "repo"
    _repo(repo)
    base = _commit(repo, "safe.txt", "safe\n")
    call_line = (
        '_SECRETS_FINDING_LINE = re.compile(r"SENSITIVE PATH \\(credential-store '
        'path class\\)" r"|^Finding:")\n'
    )
    head = _commit(repo, "engine/types.py", call_line)
    assert scan_push_range(str(repo), head, base) == []

    quoted = _commit(repo, "cfg.py", 'API_TOKEN = "' + _ASSIGN_VALUE_A + _ASSIGN_VALUE_B + '"\n')
    assert scan_push_range(str(repo), quoted, base) == ["credential assignment"]

    bare = _commit(repo, ".env", "API_TOKEN=" + _ASSIGN_VALUE_A + _ASSIGN_VALUE_B + "\n")
    assert scan_push_range(str(repo), bare, base) == ["credential assignment"]


def test_looks_like_placeholder_detects_fixture_shapes():
    """GR-GAP-066 (C): repeated periods and long identical runs are fixtures."""
    assert _looks_like_placeholder("a1" * 18)
    assert _looks_like_placeholder("A1b2C3d4" * 5)
    assert _looks_like_placeholder("0" * 18)
    assert not _looks_like_placeholder("kX9mQ2vL7pR4wT8yB3nF6jH1cD5sG0aZ")


def test_push_range_accepts_deliberate_fixture_shapes(tmp_path):
    """Every fixture shape that produced a false positive on GR-GAP-066 passes."""
    repo = tmp_path / "repo"
    _repo(repo)
    base = _commit(repo, "safe.txt", "safe\n")
    _commit(repo, "engine/scope.py", 'FULL_TREE_SECRETS_SCOPE = "working-tree"\n')
    _commit(repo, "engine/prose.py", 'api_key = "DEEPSEEK (fallback)"\n')
    _commit(repo, "engine/prose_kwargs.py", 'api_key_source="DEEPSEEK_API_KEY (fallback)",\n')
    _commit(repo, "engine/prose_upper.py", 'api_key = "SECRET-MUST-NOT-LAND"\n')
    _commit(repo, "engine/prose_lower.py", 'api_key = "abcdefghijklmnopqrstuvwxyz"\n')
    _commit(repo, "engine/sk_fixture.py", '    sk_secret = "sk-proj-" + "aB1cD2" * 6\n')
    _commit(
        repo,
        "engine/gh_line_fixture.py",
        "_GH_TOKEN_LINE = 'GITHUB_TOKEN = \"ghp_' + \"a1\" * 18 + '\"'\n",
    )
    _commit(
        repo,
        "engine/slack_line_fixture.py",
        "_SLACK_TOKEN_LINE = 'SLACK_TOKEN = \"xoxb-' + \"1\" * 20 + '\"'\n",
    )
    _commit(repo, "engine/planted.py", 'PLANTED_SECRET = "ghp_" + "A1b2C3d4" * 5\n')
    _commit(
        repo,
        "engine/fake_token.py",
        '    fake_token = "ghp_" + "FAKEKEYnotreal" + "0000000000000000001"\n',
    )
    head = _git(repo, "rev-parse", "HEAD")
    assert scan_push_range(str(repo), head, base) == []
    assert check_push_range(str(repo), head, base) == 0


def test_push_range_still_refuses_real_shaped_secrets(tmp_path):
    """GR-GAP-066 keeps refusing randomly-shaped credentials."""
    repo = tmp_path / "repo"
    _repo(repo)
    base = _commit(repo, "safe.txt", "safe\n")
    fake_token = _fake_github_token()
    fake_key = _FAKE_SK_PREFIX + _FAKE_SK_BODY
    hex_key = _HEX_PART_A + _HEX_PART_B
    _commit(repo, "engine/gh_token.py", f'token = "{fake_token}"\n')
    _commit(repo, "engine/sk_key.py", f'key = "{fake_key}"\n')
    _commit(
        repo,
        "engine/service.pem",
        _PRIVATE_KEY_OPEN + _PRIVATE_KEY_OPEN_CONT + "\nMIIB\n" + _PRIVATE_KEY_CLOSE + "\n",
    )
    _commit(repo, "engine/settings.py", f'api_key = "{hex_key}"\n')
    head = _git(repo, "rev-parse", "HEAD")

    findings = scan_push_range(str(repo), head, base)
    assert "GitHub personal access token" in findings
    assert "API key" in findings
    assert "private key" in findings
    assert "credential assignment" in findings
    assert check_push_range(str(repo), head, base) == 1


def test_refusal_names_commit_and_path_without_value(tmp_path, capsys):
    """GR-GAP-066 (D): the refusal names commit + path, never the value."""
    repo = tmp_path / "repo"
    _repo(repo)
    base = _commit(repo, "safe.txt", "safe\n")
    fake_token = _fake_github_token()
    head = _commit(repo, "engine/leaky.py", f"MY_TOKEN = {fake_token!r}\n")

    assert check_push_range(str(repo), head, base) == 1
    output = capsys.readouterr().out
    assert f"in engine/leaky.py @ {head[:8]}" in output
    assert "GitHub personal access token" in output
    assert fake_token not in output


def test_refusal_groups_by_class_path_and_commit(tmp_path, capsys):
    """One refusal line per (class, path, commit) group with an occurrence count."""
    repo = tmp_path / "repo"
    _repo(repo)
    base = _commit(repo, "safe.txt", "safe\n")
    hex_key = _HEX_PART_A + _HEX_PART_B
    first = _commit(
        repo,
        "engine/a.py",
        f'api_key = "{hex_key}"\napi_key_secondary = "{hex_key}b1"\n',
    )
    second = _commit(repo, "engine/b.py", f'api_key = "{hex_key}"\n')

    assert check_push_range(str(repo), second, base) == 1
    output = capsys.readouterr().out
    assert f"credential assignment in engine/a.py @ {first[:8]} (2 places)" in output
    assert f"credential assignment in engine/b.py @ {second[:8]} (1 place)" in output
    assert hex_key not in output


def test_gitleaks_path_allowlist_honoured(tmp_path, capsys):
    """GR-GAP-066 (A): .gitleaks.toml [allowlist].paths skips listed paths only."""
    repo = tmp_path / "repo"
    _repo(repo)
    (repo / ".gitleaks.toml").write_text(
        "[allowlist]\npaths = ['''tests/fixtures/secrets/.*''']\n",
        encoding="utf-8",
    )
    base = _commit(repo, "safe.txt", "safe\n")
    fake_token = _fake_github_token()
    listed = _commit(repo, "tests/fixtures/secrets/x.py", f"token = {fake_token!r}\n")

    assert scan_push_range(str(repo), listed, base) == []
    assert check_push_range(str(repo), listed, base) == 0
    output = capsys.readouterr().out
    assert "allowlisted paths skipped: 1 file" in output

    outside = _commit(repo, "engine/x.py", f"token = {fake_token!r}\n")
    assert scan_push_range(str(repo), outside, base) == ["GitHub personal access token"]
    assert check_push_range(str(repo), outside, base) == 1


def test_gitleaks_allowlist_degrades_to_empty_on_bad_config(tmp_path):
    """GR-GAP-066 (A): any load failure degrades to () and never raises."""
    assert _load_gitleaks_path_allowlist(str(tmp_path)) == ()
    (tmp_path / ".gitleaks.toml").write_text("not [ valid toml", encoding="utf-8")
    assert _load_gitleaks_path_allowlist(str(tmp_path)) == ()
    (tmp_path / ".gitleaks.toml").write_text("[allowlist]\npaths = 42\n", encoding="utf-8")
    assert _load_gitleaks_path_allowlist(str(tmp_path)) == ()
    (tmp_path / ".gitleaks.toml").write_text(
        '[allowlist]\npaths = ["[unclosed"]\n', encoding="utf-8"
    )
    assert _load_gitleaks_path_allowlist(str(tmp_path)) == ()
    (tmp_path / ".gitleaks.toml").write_text(
        "[allowlist]\npaths = ['''sandbox/.*''', 42]\n", encoding="utf-8"
    )
    assert _load_gitleaks_path_allowlist(str(tmp_path)) == ()
    (tmp_path / ".gitleaks.toml").write_text(
        "[allowlist]\npaths = ['''sandbox/.*''']\n", encoding="utf-8"
    )
    loaded = _load_gitleaks_path_allowlist(str(tmp_path))
    assert len(loaded) == 1
    assert loaded[0].search("sandbox/leak.py") is not None


def test_gitleaks_allowlist_skips_sandbox_fixture_shape(tmp_path):
    """The sandbox DEFAULT_KEY fixture shape is skipped via path parity."""
    repo = tmp_path / "repo"
    _repo(repo)
    (repo / ".gitleaks.toml").write_text(
        "[allowlist]\npaths = ['''sandbox/.*''']\n",
        encoding="utf-8",
    )
    base = _commit(repo, "safe.txt", "safe\n")
    fake_key = _FAKE_SK_PREFIX + _FAKE_SK_BODY
    head = _commit(repo, "sandbox/test_review.py", f"    DEFAULT_KEY = {fake_key!r}\n")

    assert scan_push_range(str(repo), head, base) == []
    assert check_push_range(str(repo), head, base) == 0
