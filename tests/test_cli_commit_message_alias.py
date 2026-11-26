"""
DF-033: `gitreins commit` accepts `-m/--message` as an alias for the
positional message.

Git instinct types `gitreins commit -m 'msg'`, which used to die with
rc=2 "error: unrecognized arguments: -m" plus the full subcommand list.
The parser now takes a positional (`message_pos`) OR the `-m/--message`
alias (`message`); cmd_commit resolves the two:

- both supplied -> rc=2 naming the conflict,
- neither supplied -> rc=2 naming both forms,
- either supplied alone -> the normal guarded commit path, unchanged.
"""

import argparse
import subprocess
from pathlib import Path

import pytest

from gitreins.cli import build_parser, cmd_commit


def _parse_commit(argv: list[str]) -> argparse.Namespace:
    parser = build_parser()
    return parser.parse_args(["commit", *argv])


# ---------------------------------------------------------------- parser shape


def test_parser_accepts_short_m_alias() -> None:
    args = _parse_commit(["-m", "alias msg"])
    assert args.message == "alias msg"
    assert args.message_pos is None


def test_parser_accepts_long_message_alias() -> None:
    args = _parse_commit(["--message", "alias msg"])
    assert args.message == "alias msg"
    assert args.message_pos is None


def test_parser_keeps_positional_form() -> None:
    args = _parse_commit(["positional msg"])
    assert args.message_pos == "positional msg"
    assert args.message is None


# --------------------------------------------------- cmd_commit conflict rules


def _commit_args(**overrides: object) -> argparse.Namespace:
    defaults = dict(message=None, message_pos=None, skip_tier2=True, allow_in_progress=False)
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def test_cmd_commit_rejects_message_given_twice(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both forms supplied -> clear error naming the conflict, exit 2, no guards."""
    guard_stub_constructed = []
    monkeypatch.setattr(
        "engine.guard_manager.GuardManager",
        lambda *a, **kw: guard_stub_constructed.append(1) or None,
    )

    with pytest.raises(SystemExit) as excinfo:
        cmd_commit(_commit_args(message="alias msg", message_pos="positional msg"))

    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert "given twice" in err
    assert "-m/--message" in err
    assert "positional" in err
    assert guard_stub_constructed == []


def test_cmd_commit_rejects_missing_message(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Neither form supplied -> error naming both forms, exit 2, no guards."""
    guard_stub_constructed = []
    monkeypatch.setattr(
        "engine.guard_manager.GuardManager",
        lambda *a, **kw: guard_stub_constructed.append(1) or None,
    )

    with pytest.raises(SystemExit) as excinfo:
        cmd_commit(_commit_args())

    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert "commit takes a message" in err
    assert 'gitreins commit "msg"' in err
    assert 'commit -m "msg"' in err
    assert guard_stub_constructed == []


# ------------------------------------------------------------ end-to-end commit


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def _init_repo(tmp_path: Path) -> Path:
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


def _stub_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    class _StubTier1:
        passed = True
        summary = "stub tier 1"

    class _GuardManagerStub:
        def __init__(self, workdir: str, config: dict = None) -> None:
            pass

        def run_all(self) -> object:
            return _StubTier1()

    monkeypatch.setattr("engine.guard_manager.GuardManager", _GuardManagerStub)


def test_commit_via_m_alias_writes_the_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """End to end: parser -> cmd_commit -> guarded commit carrying the -m text."""
    repo = _init_repo(tmp_path)
    (repo / "staged.txt").write_text("staged\n")
    _git(repo, "add", "staged.txt")
    head_before = _git(repo, "rev-parse", "HEAD")
    monkeypatch.chdir(repo)
    _stub_guard(monkeypatch)

    args = _parse_commit(["-m", "df033 alias commit", "--skip-tier2"])
    cmd_commit(args)

    captured = capsys.readouterr()
    assert "Tier 1 PASSED" in captured.out
    assert _git(repo, "rev-parse", "HEAD") != head_before
    subject = _git(repo, "log", "-1", "--format=%s")
    assert subject == "df033 alias commit"


def test_commit_via_positional_writes_the_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The original positional form still lands its message through the parser."""
    repo = _init_repo(tmp_path)
    (repo / "staged.txt").write_text("staged\n")
    _git(repo, "add", "staged.txt")
    head_before = _git(repo, "rev-parse", "HEAD")
    monkeypatch.chdir(repo)
    _stub_guard(monkeypatch)

    args = _parse_commit(["df033 positional commit", "--skip-tier2"])
    cmd_commit(args)

    captured = capsys.readouterr()
    assert "Tier 1 PASSED" in captured.out
    assert _git(repo, "rev-parse", "HEAD") != head_before
    subject = _git(repo, "log", "-1", "--format=%s")
    assert subject == "df033 positional commit"
