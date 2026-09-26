"""
DF-GITREINS-POC-66: `gitreins commit` must refuse while tasks are in_progress.

The harness has two commit doors and only one was gated. The MCP door
(`gitreins_mcp/server.py` `_commit`) refuses while any task is in_progress —
`task.complete` runs the quality judge against the committed state, so a
commit made mid-task skips the judge the rule exists to force. The CLI door
(`gitreins/cli.py` `cmd_commit`) ran Tier 1 and committed silently (real
repro: dogfood run 14 committed 8e1c239 while consumer-14 was in_progress).

These tests pin the CLI to the same source of truth as the MCP door
(`TaskManager(workdir).list_tasks("in_progress")`), the same rationale
wording, and the `--allow-in-progress` escape hatch.
"""

import argparse
import re
import subprocess
from pathlib import Path

import pytest

from gitreins.cli import cmd_commit
from engine.task_manager import TaskManager

# The exact rationale clause both surfaces must carry (MCP wording parity).
RATIONALE_CLAUSE = (
    "commits are blocked while a task is in_progress because task.complete "
    "runs the quality judge against the committed state"
)


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def _init_repo(tmp_path: Path) -> Path:
    """Real repo with one base commit; tests stage files on top of it."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "GitReins Tests")
    _git(repo, "config", "user.email", "tests@gitreins.local")
    (repo / "base.txt").write_text("base\n")
    _git(repo, "add", "base.txt")
    _git(repo, "commit", "-q", "-m", "base")
    # GR-GAP-051: cmd_commit refuses without .gitreins/config.yaml before the
    # guard stage. GuardManager is stubbed in these tests, so only the file's
    # existence matters — write the same minimal shape test_cli.py uses.
    cfg = repo / ".gitreins" / "config.yaml"
    cfg.parent.mkdir()
    cfg.write_text("guards:\n  test_command: echo ok\n  allow_skips: true\n")
    return repo


def _seed_in_progress_task(workdir: str, task_id: str) -> None:
    tm = TaskManager(workdir)
    tm.create(task_id, "Started but not finished", ["criterion one"])
    tm.start(task_id)


def _commit_args(**overrides) -> argparse.Namespace:
    defaults = dict(message="poc-66 commit", skip_tier2=True, allow_in_progress=False)
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


class _StubTier1:
    passed = True
    summary = "stub tier 1"


class _StubGuardManager:
    """Stands in for the guard stage so tests target the task check itself."""

    def __init__(self, workdir, config=None):
        self.workdir = workdir

    def run_all(self):
        return _StubTier1()


def test_commit_refuses_while_task_in_progress(tmp_path, monkeypatch, capsys):
    """Case 1: refusal exits 1 BEFORE guards, naming the task, no commit made."""
    repo = _init_repo(tmp_path)
    _seed_in_progress_task(str(repo), "consumer-14")
    _seed_in_progress_task(str(repo), "consumer-15")
    (repo / "staged.txt").write_text("staged\n")
    _git(repo, "add", "staged.txt")
    head_before = _git(repo, "rev-parse", "HEAD")
    monkeypatch.chdir(repo)

    with pytest.raises(SystemExit) as excinfo:
        cmd_commit(_commit_args())

    assert excinfo.value.code == 1
    err = capsys.readouterr().err
    assert "Tasks still in progress: consumer-14, consumer-15" in err
    assert RATIONALE_CLAUSE in err
    # Short-circuit proof: guards never ran, nothing was committed.
    assert "Tier 1" not in err
    assert _git(repo, "rev-parse", "HEAD") == head_before
    status = _git(repo, "status", "--porcelain")
    assert "A  staged.txt" in status  # the staged payload is untouched


def test_refusal_wording_parity_with_mcp_surface():
    """Cases 2+5: CLI refusal and MCP error share the exact rationale clause.

    Both files build the message from adjacent string-literal fragments, so
    the sources are fragment-joined the same way Python joins them at runtime
    before the clause is asserted. (The CLI's runtime output is asserted
    verbatim in test_commit_refuses_while_task_in_progress.)
    """
    root = Path(__file__).resolve().parents[1]

    def source_contains_clause(path: Path) -> bool:
        src = path.read_text()
        joined = re.sub(r'"\s*\n\s*"', "", src)
        return RATIONALE_CLAUSE in joined

    assert source_contains_clause(root / "gitreins" / "cli.py"), (
        "CLI refusal lost the MCP rationale clause"
    )
    assert source_contains_clause(root / "gitreins_mcp" / "server.py"), (
        "MCP error no longer carries the rationale clause — surfaces drifted"
    )


def test_allow_in_progress_warns_and_proceeds(tmp_path, monkeypatch, capsys):
    """Case 3: escape hatch prints ONE warning line, then commits normally."""
    repo = _init_repo(tmp_path)
    _seed_in_progress_task(str(repo), "consumer-14")
    (repo / "staged.txt").write_text("staged\n")
    _git(repo, "add", "staged.txt")
    head_before = _git(repo, "rev-parse", "HEAD")
    monkeypatch.chdir(repo)
    monkeypatch.setattr("engine.guard_manager.GuardManager", _StubGuardManager)

    cmd_commit(_commit_args(allow_in_progress=True))

    captured = capsys.readouterr()
    assert (
        "WARNING: committing with 1 task(s) in_progress: consumer-14 "
        "(--allow-in-progress)" in captured.err
    )
    assert "Tasks still in progress" not in captured.err
    assert "Commit completeness confirmed: 1 staged path(s)" in captured.out
    head_after = _git(repo, "rev-parse", "HEAD")
    assert head_after != head_before
    assert "staged.txt" in _git(repo, "show", "--name-only", "--format=", "HEAD")


def test_no_in_progress_tasks_adds_no_output(tmp_path, monkeypatch, capsys):
    """Case 4: no tasks — no refusal, no warning, unchanged commit behavior."""
    repo = _init_repo(tmp_path)
    (repo / "staged.txt").write_text("staged\n")
    _git(repo, "add", "staged.txt")
    head_before = _git(repo, "rev-parse", "HEAD")
    monkeypatch.chdir(repo)
    monkeypatch.setattr("engine.guard_manager.GuardManager", _StubGuardManager)

    cmd_commit(_commit_args())

    captured = capsys.readouterr()
    assert "Tasks still in progress" not in captured.err
    assert "WARNING: committing with" not in captured.err
    assert _git(repo, "rev-parse", "HEAD") != head_before
