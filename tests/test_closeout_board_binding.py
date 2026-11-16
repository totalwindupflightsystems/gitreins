"""GR-231: a closeout must be witnessed by the fleet board.

The defect this pins, measured on the fleet's own history: of 4,104 PASS
verdicts, 18 were later contradicted by what actually landed.  Two shapes
accounted for most of them, and both closed cleanly:

* the verdict graded one commit while the thing that landed was another — the
  task store said ``complete`` because the author completed it, and nothing
  ever compared it to the commit the verdict had just read;
* the row was still open (``pending`` / ``in_progress`` / ``reopened``) when
  the closeout ran, so a closed record was manufactured out of an open one;
* and a verdict could simply CLAIM ``pass_class: judged`` with no Tier 2
  artifact behind the claim, which made forging the class cheaper than running
  the judge.

Every test here drives a real entrypoint — the CLI's ``task complete`` or the
worktree manager's ``merge`` — never the helper the fix is built on, so a
gate that is wired to nothing fails these tests.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

CLI_SCRIPT = Path(__file__).parents[1] / "gitreins" / "cli.py"

#: Credentials are irrelevant to every case here (``--skip-tier2`` and forged
#: verdicts), and inheriting a live one would let a stray provider call make a
#: refusal look like a timeout.
_LLM_KEYS = (
    "GITREINS_LLM_API_KEY",
    "GITREINS_LLM_BASE_URL",
    "GITREINS_LLM_MODEL",
    "OPENROUTER_API_KEY",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
)


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=check, timeout=60
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A real git repository with one commit — the control root for a closeout."""
    path = tmp_path / "repo"
    path.mkdir()
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.name", "GR-231 Fixture")
    _git(path, "config", "user.email", "gr231@example.invalid")
    (path / "base.txt").write_text("base\n", encoding="utf-8")
    _git(path, "add", "base.txt")
    _git(path, "commit", "-qm", "initial")
    # The board is harness state for these tests: present on disk (the closeout
    # must read it) but not part of the tree the merge gate calls "clean", so a
    # board write cannot turn every arm into a dirty-main refusal.
    exclude = path / ".git" / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    exclude.write_text(".coding-hermes/\n.gitreins-jobs/\n", encoding="utf-8")
    return path


def _head(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _commit_work(repo: Path, name: str = "work.txt") -> str:
    (repo / name).write_text("work\n", encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "commit", "-qm", f"land {name}")
    return _head(repo)


def _board_row(task_id: str, status: str, commit: str | None) -> dict:
    row: dict = {"id": task_id, "status": status}
    if commit is not None:
        row["commit_hash"] = commit
    return row


def _write_board(repo: Path, *rows: dict) -> Path:
    """Write the canonical fleet board (append-log semantics: last row wins)."""
    board = repo / ".coding-hermes" / "board"
    board.mkdir(parents=True, exist_ok=True)
    tasks = board / "tasks.jsonl"
    tasks.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return tasks


def _cli(repo: Path, *args: str) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k not in _LLM_KEYS}
    env["PYTHONPATH"] = str(CLI_SCRIPT.parents[1])
    env["GITREINS_JOB_DIR"] = str(repo / ".gitreins-jobs")
    return subprocess.run(
        [sys.executable, str(CLI_SCRIPT), *args],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=180,
        env=env,
    )


def _seed_task(repo: Path, task_id: str = "GR-231") -> None:
    created = _cli(repo, "task", "create", task_id, "Bind closeout to the board", "c1")
    assert created.returncode == 0, created.stdout + created.stderr
    started = _cli(repo, "task", "start", task_id)
    assert started.returncode == 0, started.stdout + started.stderr


def _task_status(repo: Path, task_id: str) -> str:
    listed = _cli(repo, "task", "list")
    assert listed.returncode == 0, listed.stdout + listed.stderr
    return listed.stdout


# --------------------------------------------------------------------------
# (a) commit drift
# --------------------------------------------------------------------------


def test_drifted_commit_refuses_the_closeout_and_leaves_the_task_alone(repo: Path) -> None:
    """The row closed at commit A while the verdict grades commit B: refuse.

    This is the drift class verbatim.  The board says the row closed at the
    commit it was closed at; a later commit landed on top; a closeout that only
    reads ``tasks.yaml`` closes the task anyway and records a PASS over a tree
    the row never named.
    """
    _seed_task(repo)
    graded_before = _head(repo)
    _write_board(repo, _board_row("GR-231", "complete", graded_before))
    drifted = _commit_work(repo, "drift.txt")
    assert drifted != graded_before

    result = _cli(repo, "task", "complete", "--skip-tier2", "GR-231")

    assert result.returncode != 0, result.stdout + result.stderr
    assert "board row GR-231 references commit" in result.stderr, result.stderr
    assert "describe different trees" in result.stderr, result.stderr
    assert graded_before[:12] in result.stderr and drifted[:12] in result.stderr, result.stderr
    # Non-vacuous: the refusal happened BEFORE the task state moved, so the
    # store still shows the task in progress rather than a half-closed record.
    assert "◐" in _task_status(repo, "GR-231")
    assert "●" not in _task_status(repo, "GR-231")


# --------------------------------------------------------------------------
# (b) reopened after a PASS closed it
# --------------------------------------------------------------------------


def test_reopened_row_refuses_even_though_an_earlier_row_closed_it(repo: Path) -> None:
    """A row reopened after its PASS must refuse — the tail is the current state.

    The board is an append log: the row that closed the task is still in the
    file, so a reader that takes the FIRST copy (or any copy) sees a clean
    terminal row.  The last copy is what the owner actually believes.
    """
    _seed_task(repo)
    commit = _head(repo)
    _write_board(
        repo,
        _board_row("GR-231", "complete", commit),
        _board_row("GR-231", "reopened", commit),
    )

    result = _cli(repo, "task", "complete", "--skip-tier2", "GR-231")

    assert result.returncode != 0, result.stdout + result.stderr
    assert "'reopened', not terminal" in result.stderr, result.stderr
    assert "◐" in _task_status(repo, "GR-231")


def test_pending_row_refuses_the_closeout(repo: Path) -> None:
    """A row that was never opened still cannot be closed out."""
    _seed_task(repo)
    _write_board(repo, _board_row("GR-231", "pending", _head(repo)))

    result = _cli(repo, "task", "complete", "--skip-tier2", "GR-231")

    assert result.returncode != 0, result.stdout + result.stderr
    assert "'pending', not terminal" in result.stderr, result.stderr


# --------------------------------------------------------------------------
# (c) the matching case still works
# --------------------------------------------------------------------------


def test_matching_terminal_row_and_commit_lets_the_closeout_succeed(repo: Path) -> None:
    """Terminal row + the commit the verdict grades: the closeout proceeds.

    The control arm. Without it every refusal above could equally be "the gate
    refuses everything", which would be just as wrong as accepting everything.
    """
    _seed_task(repo)
    commit = _head(repo)
    _write_board(repo, _board_row("GR-231", "complete", commit))

    result = _cli(repo, "task", "complete", "--skip-tier2", "GR-231")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Completed: GR-231" in result.stdout, result.stdout
    assert "Cannot complete" not in result.stderr, result.stderr
    assert "●" in _task_status(repo, "GR-231")


def test_a_checkout_without_a_board_is_not_refused(repo: Path) -> None:
    """The board stays optional — most repositories never create one."""
    _seed_task(repo)
    assert not (repo / ".coding-hermes" / "board" / "tasks.jsonl").exists()

    result = _cli(repo, "task", "complete", "--skip-tier2", "GR-231")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Completed: GR-231" in result.stdout, result.stdout


# --------------------------------------------------------------------------
# (d) a forged verdict, and the unbacked judged claim
# --------------------------------------------------------------------------


def _forged_verdict(record, tree: Path, tip: str) -> dict:
    """A verdict that CLAIMS a judged pass for *tip* (the shape a forger writes)."""
    return {
        "task_id": record.task_id,
        "passed": True,
        "pass_class": "judged",
        "tier2": {"verdict": "COMPLETE"},
        "worktree": str(tree.resolve()),
        "branch": record.branch,
        "commit": tip,
    }


def test_forged_verdict_with_a_wrong_board_commit_is_refused_at_merge(repo: Path) -> None:
    """The negative control: a verdict whose commit the board does not confirm.

    `worktree merge` is the other closeout — it lands the commit and reaps the
    branch — and it consumes a STORED verdict, which is exactly the document a
    forger controls.  The verdict below is internally consistent (its commit is
    the branch tip, so every pre-GR-231 check passes); only the board row
    disagrees, which is the one reading an author cannot forge from inside the
    tree they are landing.
    """
    from engine.worktree_manager import WorktreeError, WorktreeManager, write_disk_verdict

    manager = WorktreeManager(repo)
    record, _created = manager.create("GR-231-FORGED")
    tree = Path(record.path)
    (tree / "lane.txt").write_text("lane\n", encoding="utf-8")
    _git(tree, "add", "lane.txt")
    _git(tree, "commit", "-qm", "lane work")
    tip = _git(tree, "rev-parse", "HEAD").stdout.strip()
    write_disk_verdict(tree, _forged_verdict(record, tree, tip))

    # A board that does not know this task at all cannot witness its closeout.
    _write_board(repo, _board_row("GR-231", "complete", tip))
    with pytest.raises(WorktreeError, match="has no row for"):
        manager.merge(record.task_id)

    # A row that references a different commit is the forged-verdict control.
    other = _head(repo)
    assert other != tip
    _write_board(repo, _board_row(record.task_id, "complete", other))
    with pytest.raises(WorktreeError, match="describe different trees"):
        manager.merge(record.task_id)

    # A row reopened after the verdict cannot close out either.
    _write_board(repo, _board_row(record.task_id, "reopened", tip))
    with pytest.raises(WorktreeError, match="not terminal"):
        manager.merge(record.task_id)

    # Control: the same verdict, with a terminal row at the graded commit, merges.
    _write_board(repo, _board_row(record.task_id, "complete", tip))
    merged = manager.merge(record.task_id)
    assert merged["source_commit"] == tip
    assert _head(repo) == tip


def test_judged_claim_without_a_tier2_artifact_is_never_a_pass() -> None:
    """A claimed ``judged`` class with nothing behind it is not a PASS at all.

    ``pass_class`` is persisted input.  Honouring the claim made forging the
    field the cheapest way through the gate, so the claim is only believed when
    the artifact it names is present — and the advisory routing degrades to
    ``needs_review`` rather than ``no_concerns_found``.
    """
    from engine.gate import (
        PASS_CLASS_JUDGED,
        PASS_CLASS_UNVERIFIED,
        merge_recommendation,
        pass_class,
    )

    claimed = {
        "task_id": "GR-231",
        "passed": True,
        "pass_class": "judged",
        "verdict": "COMPLETE",
        "items": [],
    }
    assert pass_class(claimed) == PASS_CLASS_UNVERIFIED
    assert merge_recommendation(claimed) == "needs_review"

    backed = dict(claimed, tier2={"verdict": "COMPLETE", "items": [{"status": "PASS"}]})
    assert pass_class(backed) == PASS_CLASS_JUDGED
    assert merge_recommendation(backed) != "needs_review"


def test_unbacked_judged_verdict_is_refused_at_merge(repo: Path) -> None:
    """And the merge gate routes it to review instead of landing it."""
    from engine.worktree_manager import WorktreeError, WorktreeManager, write_disk_verdict

    manager = WorktreeManager(repo)
    record, _created = manager.create("GR-231-CLAIM")
    tree = Path(record.path)
    (tree / "lane.txt").write_text("lane\n", encoding="utf-8")
    _git(tree, "add", "lane.txt")
    _git(tree, "commit", "-qm", "lane work")
    tip = _git(tree, "rev-parse", "HEAD").stdout.strip()
    forged = _forged_verdict(record, tree, tip)
    forged.pop("tier2")  # the claim without the artifact
    write_disk_verdict(tree, forged)
    _write_board(repo, _board_row(record.task_id, "complete", tip))

    with pytest.raises(WorktreeError, match="unverified-never-pass"):
        manager.merge(record.task_id)
    assert _head(repo) != tip
