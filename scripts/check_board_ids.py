#!/usr/bin/env python3
"""Board id hygiene — one id, one finding (QA-GITREINS-POC-8).

The QA harness re-filed its per-cycle findings under the SAME ids
(``QA-GITREINS-POC-1`` carried 8 different pending titles), so any downstream
dedupe that matches on id/title could never fire: the 12 legacy rows were
un-dedupable. This gate keeps that from happening again:

* every ``id`` in ``tasks.jsonl`` must be bound to exactly ONE row;
* legacy duplicates are grandfathered by count in a baseline file, so the
  check is meaningful for this repo's history without pretending it is clean
  (``.coding-hermes/board/id-baseline.json``);
* a baseline entry whose rows are no longer duplicated (or whose count no
  longer matches) FAILS: the baseline may only shrink, and it must be edited in
  the commit that changes the board;
* every row must carry a non-empty ``id``, ``title`` and ``status`` (a row
  appended without ``status`` is invisible to state filters and was a real
  append bug).

Exit 0 = clean, 1 = a violation, 2 = the board could not be read at all.

Usage:
    python scripts/check_board_ids.py [BOARD_DIR] [--baseline PATH]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict

DEFAULT_BOARD_DIR = ".coding-hermes/board"
DEFAULT_BASELINE_NAME = "id-baseline.json"


def load_rows(path: str) -> tuple[list[dict], int]:
    """Return (rows, unparsable-line count); a malformed line is skipped, not fatal."""
    rows: list[dict] = []
    bad = 0
    with open(path, encoding="utf-8", errors="replace") as stream:
        for line in stream:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                bad += 1
                continue
            if isinstance(row, dict):
                rows.append(row)
            else:
                bad += 1
    return rows, bad


def load_baseline(path: str) -> dict[str, int]:
    """``{id: expected_row_count}`` for duplicates that predate this gate."""
    if not path or not os.path.isfile(path):
        return {}
    with open(path, encoding="utf-8") as stream:
        data = json.load(stream)
    duplicates = data.get("duplicate_ids", {}) if isinstance(data, dict) else {}
    return {str(key): int(value) for key, value in duplicates.items()}


def audit(rows: list[dict], baseline: dict[str, int]) -> list[str]:
    """Return the failure lines for *rows*; empty means clean."""
    failures: list[str] = []
    by_id: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        by_id[str(row.get("id", ""))].append(row)

    for row_id, group in sorted(by_id.items()):
        if not row_id:
            failures.append(f"row without an id ({len(group)} row(s))")
            continue
        if len(group) == 1:
            if row_id in baseline:
                failures.append(
                    f"stale baseline entry: {row_id} is no longer duplicated "
                    f"({len(group)} row) — delete it from the baseline"
                )
            continue
        expected = baseline.get(row_id)
        titles = sorted({str(row.get("title", "")) for row in group})
        if expected is None:
            failures.append(
                f"duplicate id {row_id}: {len(group)} rows, {len(titles)} distinct "
                f"title(s) — renumber the new row(s) after the highest suffix in use"
            )
        elif expected != len(group):
            failures.append(
                f"baseline count for {row_id} is {expected}, board has {len(group)} — "
                "the baseline must be updated in the same commit as the board"
            )

    for index, row in enumerate(rows, start=1):
        for field in ("id", "title", "status"):
            if not str(row.get(field, "")).strip():
                failures.append(f"row {index} ({row.get('id') or '<no id>'}) has no {field}")
    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("board_dir", nargs="?", default=DEFAULT_BOARD_DIR)
    parser.add_argument(
        "--baseline",
        default=None,
        help=f"baseline JSON (default: <board_dir>/{DEFAULT_BASELINE_NAME})",
    )
    args = parser.parse_args(argv)

    baseline_path = args.baseline or os.path.join(args.board_dir, DEFAULT_BASELINE_NAME)
    tasks_path = os.path.join(args.board_dir, "tasks.jsonl")
    if not os.path.isfile(tasks_path):
        print(f"error: no board at {tasks_path}", file=sys.stderr)
        return 2

    rows, unparsable = load_rows(tasks_path)
    baseline = load_baseline(baseline_path)
    failures = audit(rows, baseline)

    if failures:
        print(f"✗ board ids — FAIL ({len(failures)} finding(s) in {tasks_path})")
        for failure in failures:
            print(f"      {failure}")
        return 1

    grandfathered = ", ".join(f"{key}×{value}" for key, value in sorted(baseline.items()))
    scope = f"{len(rows)} row(s)"
    if grandfathered:
        scope += f"; grandfathered legacy duplicates: {grandfathered}"
    if unparsable:
        scope += f"; {unparsable} unparsable line(s) skipped"
    print(f"✓ board ids — unique ({scope})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
