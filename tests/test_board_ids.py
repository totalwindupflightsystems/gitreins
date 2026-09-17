"""Hermetic tests for scripts/check_board_ids.py (QA-GITREINS-POC-8).

The live board is the check subject in CI (the ``Verify board id hygiene``
step runs the script against .coding-hermes/board), so these tests stay
hermetic: every case builds its own board under tmp_path and exercises both the
importable functions and the real exit code. The legacy duplicate ids this repo
still carries are grandfathered by count in
``.coding-hermes/board/id-baseline.json`` — and a baseline entry that is no
longer needed is itself a failure, so the baseline can only shrink.
"""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "check_board_ids.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("check_board_ids", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _row(row_id, title="a finding", status="pending"):
    return {"id": row_id, "title": title, "status": status}


def _board(tmp_path, rows, baseline=None, name="board"):
    """Write a board dir with *rows* (and an optional baseline) and return it."""
    board = tmp_path / name
    board.mkdir()
    with open(board / "tasks.jsonl", "w", encoding="utf-8") as stream:
        for row in rows:
            stream.write((row if isinstance(row, str) else json.dumps(row)) + "\n")
    if baseline is not None:
        (board / "id-baseline.json").write_text(json.dumps({"duplicate_ids": baseline}))
    return board


def _run(*args):
    return subprocess.run(
        [sys.executable, str(SCRIPT_PATH), *[str(a) for a in args]],
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_unique_ids_are_clean(tmp_path):
    board = _board(tmp_path, [_row("A-1"), _row("A-2", status="complete")])

    result = _run(board)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "unique (2 row(s))" in result.stdout


def test_duplicate_id_without_a_baseline_fails(tmp_path):
    board = _board(tmp_path, [_row("QA-1", "first"), _row("QA-1", "second")])

    result = _run(board)

    assert result.returncode == 1
    assert "duplicate id QA-1" in result.stdout
    assert "renumber" in result.stdout


def test_grandfathered_duplicate_is_reported_but_passes(tmp_path):
    board = _board(tmp_path, [_row("QA-1", "first"), _row("QA-1", "second")], {"QA-1": 2})

    result = _run(board)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "grandfathered legacy duplicates: QA-1×2" in result.stdout


def test_baseline_count_must_match_the_board(tmp_path):
    board = _board(tmp_path, [_row("QA-1", "first"), _row("QA-1", "second")], {"QA-1": 8})

    result = _run(board)

    assert result.returncode == 1
    assert "baseline count for QA-1 is 8" in result.stdout


def test_stale_baseline_entry_fails_until_it_is_deleted(tmp_path):
    board = _board(tmp_path, [_row("QA-1", "only one")], {"QA-1": 2})

    result = _run(board)

    assert result.returncode == 1
    assert "stale baseline entry" in result.stdout


def test_a_row_without_status_fails(tmp_path):
    board = _board(tmp_path, [{"id": "A-1", "title": "no status here"}])

    result = _run(board)

    assert result.returncode == 1
    assert "has no status" in result.stdout


def test_a_row_without_an_id_fails(tmp_path):
    board = _board(tmp_path, [_row("A-1"), {"title": "anonymous", "status": "pending"}])

    result = _run(board)

    assert result.returncode == 1
    assert "row without an id" in result.stdout


def test_a_missing_board_is_a_distinct_exit_code(tmp_path):
    result = _run(tmp_path / "nope")

    assert result.returncode == 2
    assert "error: no board at" in result.stderr


def test_unparsable_lines_are_skipped_and_counted(tmp_path):
    board = _board(tmp_path, [_row("A-1"), "{not json", _row("A-2")])

    result = _run(board)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "unparsable line(s) skipped" in result.stdout


def test_audit_reports_every_duplicate_in_one_run(tmp_path):
    module = _load_module()
    rows = [_row("QA-1", "a"), _row("QA-1", "b"), _row("QA-2", "c"), _row("QA-2", "d")]

    failures = module.audit(rows, {})

    assert len(failures) == 2
    assert any("QA-1" in line for line in failures)
    assert any("QA-2" in line for line in failures)
