#!/usr/bin/env python3
"""Staleness sweep of open bug rows on the board (RVR-GITREINS-106).

Open bug rows can go stale: fixed upstream in a later merge but never
re-checked on the board (three such rows were closed by hand during
RVR-GITREINS-105). The -review lane needs a cheap, machine-readable way to
re-smoke each open bug row — this script is that sweep:

* selects rows whose ``status`` is ``pending`` or ``blocked`` AND whose title
  starts with a bug-class marker (``[P0]``…``[P5]`` bracket style, or the
  ``P1 -`` / ``P2 —`` dash style);
* prints a compact table (id, P-level, age in days from ``updated_at``, the
  first 80 characters of the title); rows older than ``--max-age-days`` get
  an extra ``STALE`` marker;
* ``--ids-only`` prints one id per line for a lane tick to consume.

Exit code 0 normally, 2 when the board file is missing, 1 when any non-empty
line of the board is not a complete JSON object (the offending file:line is
reported). No network, no LLM, no subprocesses — a pure stdlib file scan.

Usage:
    python scripts/review_stale_bugs.py [--board PATH] [--max-age-days N] [--ids-only]

Example:
    $ python scripts/review_stale_bugs.py --max-age-days 30
    RVR-GITREINS-101  P1        44d  [P1] guard passes with zero findings on a red board   STALE
    QA-GITREINS-2     P2         0d  P2 — clean-machine QA battery reports chaos-errorpath INFO…
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone

DEFAULT_BOARD = ".coding-hermes/board/tasks.jsonl"
DEFAULT_MAX_AGE_DAYS = 30

# Bug-class markers a row title may start with. Both repo styles are real:
# "[P2] <finding>" and "P1 - <finding>" / "P2 — <finding>" (hyphen or em dash).
_BRACKET_MARKER_RE = re.compile(r"^\[(P[0-5])\]")
_DASH_MARKER_RE = re.compile(r"^(P[0-5])\s*[-—]\s")


def classify_title(title: str) -> str | None:
    """Return the P-level of a bug-class title, or None if it is not one."""
    for pattern in (_BRACKET_MARKER_RE, _DASH_MARKER_RE):
        match = pattern.match(title)
        if match:
            return match.group(1)
    return None


def parse_timestamp(value: object) -> datetime | None:
    """Parse a board updated_at value; naive timestamps are taken as UTC."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        stamp = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp


def age_in_days(row: dict) -> int | None:
    """Whole days since ``updated_at``; None when the row carries no stamp."""
    stamp = parse_timestamp(row.get("updated_at"))
    if stamp is None:
        return None
    return max((datetime.now(timezone.utc) - stamp).days, 0)


def open_bug_rows(rows: list) -> list:
    """The pending/blocked rows whose title starts with a bug-class marker."""
    selected = []
    for row in rows:
        if row.get("status") not in ("pending", "blocked"):
            continue
        level = classify_title(str(row.get("title", "")))
        if level is None:
            continue
        selected.append((row, level))
    return selected


def render_table(selected: list, max_age_days: int) -> str:
    """The compact table (age unknown prints as '?', not a fake number)."""
    lines = []
    header = f"{'id':<16} P  age  title"
    lines.append(header)
    for row, level in selected:
        age = age_in_days(row)
        title = str(row.get("title", ""))[:80]
        age_text = "?" if age is None else f"{age}d"
        marker = " STALE" if age is not None and age > max_age_days else ""
        lines.append(f"{str(row.get('id', '')):<16} {level}  {age_text:>4}  {title}{marker}")
    return "\n".join(lines)


def scan_board(path: str) -> tuple[list, list]:
    """Read the board: (rows, [(line_no, raw_line)] unparsable non-empty lines)."""
    try:
        stream = open(path, encoding="utf-8", errors="replace")
    except OSError:
        return None, []  # caller turns a missing file into exit 2
    rows, bad_lines = [], []
    with stream:
        for line_no, line in enumerate(stream, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                bad_lines.append((line_no, line))
                continue
            if isinstance(row, dict):
                rows.append(row)
            else:
                bad_lines.append((line_no, line))
    return rows, bad_lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Staleness sweep of open bug rows on the board (see module docstring).",
    )
    parser.add_argument(
        "--board",
        default=DEFAULT_BOARD,
        help=f"tasks.jsonl path (default: {DEFAULT_BOARD})",
    )
    parser.add_argument(
        "--max-age-days",
        type=int,
        default=DEFAULT_MAX_AGE_DAYS,
        help="age in days past which a row gets a STALE marker (default: %(default)s)",
    )
    parser.add_argument(
        "--ids-only",
        action="store_true",
        help="print just the selected ids, one per line (machine-consumable)",
    )
    args = parser.parse_args(argv)

    result = scan_board(args.board)
    if result is None or isinstance(result, tuple) and result[0] is None:
        print(f"error: no board at {args.board}", file=sys.stderr)
        return 2
    rows, bad_lines = result

    selected = open_bug_rows(rows)
    if args.ids_only:
        for row, _level in selected:
            print(row.get("id", ""))
    else:
        print(render_table(selected, args.max_age_days))

    if bad_lines:
        for line_no, _raw in bad_lines:
            print(f"error: {args.board}:{line_no}: not a valid JSON object", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
