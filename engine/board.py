"""The fleet board as a closeout witness (GR-231).

``.gitreins/tasks.yaml`` is this harness's own task store: it records what the
author *claims* to have finished.  The fleet board
(``.coding-hermes/board/tasks.jsonl``) is the durable record of what was
*landed* — which row closed, and against which commit.  A closeout that only
reads ``tasks.yaml`` therefore trusts the same hand it is meant to check.

GR-231 measured the cost: a 2026-11-15 spot-check of 4,104 PASS verdicts found
18 confirmed contradictions, and the two largest classes were exactly the ones
a board readback would have caught — 4 cases where the verdict graded one
commit and another landed, and 8 records with no Tier 2 artifact behind them.
This module closes the first: a closeout may only succeed when the canonical
board row for the task is TERMINAL and references the same commit the verdict
graded.

The board stays an optional artifact — ``gitreins install``/``init`` never
create one, and most repositories do not have one.  "No board configured in
this checkout" is therefore not a refusal; "the board is configured but its row
does not say what the closeout claims" is.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from engine.repo_paths import WorktreeResolutionError, board_file_path

#: Row statuses that mean the work is finished and the row may be read back as
#: terminal.  Fleet boards use ``complete``; older rows also carry ``closed`` or
#: ``done``, and a compacted ``completed`` spelling appears in hand-edited rows.
#: Everything else — ``pending``, ``in_progress``, ``reopened``, ``blocked``, an
#: unknown status — is NON-terminal and refuses a closeout.
TERMINAL_STATUSES = frozenset({"complete", "completed", "closed", "done"})

#: Shortest commit prefix accepted as "the same commit".  Boards record the full
#: 40-character SHA; a hand-written row may abbreviate it, and git's own
#: minimum unambiguous abbreviation is 7.
MIN_COMMIT_PREFIX = 7

#: The board file this module reads.  The events sibling is written by the
#: worktree manager; only the task rows are a closeout witness.
BOARD_TASKS_FILE = "tasks.jsonl"


@dataclass(frozen=True)
class CloseoutRow:
    """The board row a closeout is checked against, and what it says."""

    row: dict[str, Any]

    @property
    def id(self) -> str:
        return str(self.row.get("id", ""))

    @property
    def status(self) -> str:
        return str(self.row.get("status", "")).strip().lower()

    @property
    def commit(self) -> str:
        """The commit this row references, if any (``commit_hash`` wins)."""
        for key in ("commit_hash", "commit"):
            value = self.row.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES


def board_tasks_path(workdir: str | os.PathLike[str] | None = None) -> Path | None:
    """Path of the canonical board's task rows, or ``None`` when unconfigured.

    A checkout without a fleet board (no ``.coding-hermes/board`` in the
    canonical main checkout, or no ``tasks.jsonl`` inside it) returns ``None``:
    the board is not a precondition for using GitReins, only a witness for it.
    """
    try:
        path = board_file_path(workdir, BOARD_TASKS_FILE)
    except (WorktreeResolutionError, ValueError):
        return None
    return path if path.is_file() else None


def load_rows(workdir: str | os.PathLike[str] | None = None) -> list[dict[str, Any]]:
    """Every parseable row of the canonical board, in file order.

    JSONL boards are append-logged: a row may appear more than once, older
    copies first.  Unparseable lines are skipped rather than fatal — one torn
    line must not turn every closeout in the fleet into an infrastructure error.
    """
    path = board_tasks_path(workdir)
    if path is None:
        return []
    rows: list[dict[str, Any]] = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def find_row(workdir: str | os.PathLike[str] | None, task_id: str) -> CloseoutRow | None:
    """The canonical row for *task_id*, or ``None`` when the board has none.

    The LAST occurrence wins: an updated row is appended, so the tail is the
    row's current state.  Ids repeat on real boards (``GR-235`` was filed
    twice), and reading the first copy would resurrect a stale status.
    """
    found: dict[str, Any] | None = None
    for row in load_rows(workdir):
        if str(row.get("id", "")) == task_id:
            found = row
    return CloseoutRow(found) if found is not None else None


def commits_match(left: str, right: str) -> bool:
    """True when two commit references name the same commit.

    Full SHAs are compared case-insensitively; a shortened reference of at
    least :data:`MIN_COMMIT_PREFIX` characters matches only as a prefix, so a
    short hand-written value cannot silently match a different commit that
    happens to share its first characters.
    """
    a = left.strip().lower()
    b = right.strip().lower()
    if not a or not b:
        return False
    if a == b:
        return True
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    return len(shorter) >= MIN_COMMIT_PREFIX and longer.startswith(shorter)


def closeout_refusal(
    workdir: str | os.PathLike[str] | None,
    task_id: str,
    verdict_commit: str,
) -> str | None:
    """Why a closeout of *task_id* at *verdict_commit* must be refused, or ``None``.

    The three refusals are the three ways a closeout can lie about what landed:

    * the board is configured but has no row for the task — nothing to read back;
    * the row is not terminal (``pending`` / ``in_progress`` / ``reopened`` /
      ``blocked``) — the work was never closed, so a closeout would create a
      closed record out of an open one;
    * the row references no commit, or a different one — the verdict graded a
      different tree than the board says landed.

    ``None`` means either the checkout has no board at all (nothing to bind; the
    board stays optional) or the row agrees with the verdict.
    """
    path = board_tasks_path(workdir)
    if path is None:
        return None
    row = find_row(workdir, task_id)
    if row is None:
        return (
            f"the fleet board ({path}) has no row for {task_id!r}, so no terminal "
            "board status can be read back for this closeout"
        )
    if not row.terminal:
        shown = row.status or "<missing>"
        return (
            f"board row {task_id} is {shown!r}, not terminal "
            f"({', '.join(sorted(TERMINAL_STATUSES))}) — the work was never closed, "
            "so it cannot be closed out"
        )
    if not row.commit:
        return (
            f"board row {task_id} is terminal but references no commit, so it cannot "
            f"confirm the {_short(verdict_commit)} the verdict graded"
        )
    if not commits_match(row.commit, verdict_commit):
        return (
            f"board row {task_id} references commit {_short(row.commit)} but this "
            f"verdict graded {_short(verdict_commit)} — the row and the verdict "
            "describe different trees"
        )
    return None


def _short(commit: str) -> str:
    """A commit reference for a message: the 12-character prefix, or the value."""
    value = commit.strip()
    return value[:12] if len(value) > 12 else (value or "<none>")
