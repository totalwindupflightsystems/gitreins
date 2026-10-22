"""Hermetic tests for scripts/review_stale_bugs.py (RVR-GITREINS-106)."""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "review_stale_bugs.py"


def _load_module() -> object:
    spec = importlib.util.spec_from_file_location("review_stale_bugs", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _board(tmp_path: Path, rows: list) -> str:
    board = tmp_path / "tasks.jsonl"
    with open(board, "w", encoding="utf-8") as stream:
        for row in rows:
            stream.write((row if isinstance(row, str) else json.dumps(row)) + "\n")
    return str(board)


def _run(*args: object) -> object:
    return subprocess.run(
        [sys.executable, str(SCRIPT_PATH), *[str(a) for a in args]],
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_pending_bracket_row_is_detected(tmp_path: Path) -> None:
    board = _board(
        tmp_path,
        [
            {
                "id": "B-1",
                "title": "[P2] broken thing",
                "status": "pending",
                "updated_at": "2026-10-22T00:00:00Z",
            },
            {"id": "B-2", "title": "plain title", "status": "pending"},
            {"id": "B-3", "title": "[P2] done thing", "status": "complete"},
        ],
    )

    result = _run("--board", board)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "B-1" in result.stdout and "P2" in result.stdout
    assert "B-2" not in result.stdout and "B-3" not in result.stdout


def test_ids_only_prints_one_id_per_line(tmp_path: Path) -> None:
    board = _board(
        tmp_path,
        [
            {"id": "B-1", "title": "[P1] broken", "status": "pending"},
            {"id": "B-4", "title": "P2 — dash style", "status": "blocked"},
        ],
    )

    result = _run("--board", board, "--ids-only")

    assert result.returncode == 0
    assert result.stdout == "B-1\nB-4\n"


def test_missing_board_exits_2(tmp_path: Path) -> None:
    result = _run("--board", str(tmp_path / "nope" / "tasks.jsonl"))

    assert result.returncode == 2
    assert "error: no board at" in result.stderr


def test_age_past_max_age_days_gets_stale_marker(tmp_path: Path) -> None:
    board = _board(
        tmp_path,
        [
            {
                "id": "OLD-1",
                "title": "[P1] ancient",
                "status": "pending",
                "updated_at": "2026-01-01T00:00:00Z",
            },
            {
                "id": "NEW-1",
                "title": "[P1] fresh",
                "status": "pending",
                "updated_at": "2026-10-22T00:00:00Z",
            },
        ],
    )

    result = _run("--board", board, "--max-age-days", "30")

    assert result.returncode == 0
    stale_lines = [ln for ln in result.stdout.splitlines() if "OLD-1" in ln]
    assert stale_lines and "STALE" in stale_lines[0]
    assert all("STALE" not in ln for ln in result.stdout.splitlines() if "NEW-1" in ln)


def test_malformed_line_exits_1_with_file_line(tmp_path: Path) -> None:
    board = _board(tmp_path, [{"id": "B-1", "title": "[P1] ok", "status": "pending"}, "{not json"])

    result = _run("--board", board, "--ids-only")

    assert result.returncode == 1
    assert f"error: {board}:2: not a valid JSON object" in result.stderr
