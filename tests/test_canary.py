from pathlib import Path

from scripts.canary import append_record, report


TRACE_MARKER = (
    "ch:trace row=GR-209 spec=.coding-hermes/board/tasks.jsonl#GR-209 "
    "wave=none:serial test=tests/test_canary.py doc=docs/operations.md "
    "evidence=scripts/canary.py witness=none:fixture-or-mock-only "
    "verdict=none:pending-judge commit=none:pending "
    "memory=none:pending"
)


def test_expected_caught_must_catch_canary(tmp_path: Path) -> None:
    ledger = tmp_path / "canaries.jsonl"

    append_record(ledger, {"canary_id": "caught-1", "must_catch": True, "caught": True})

    assert report(ledger) == {"caught": 1, "missed": 0, "recall": 1.0}


def test_expected_missed_must_catch_canary_is_counted(tmp_path: Path) -> None:
    ledger = tmp_path / "canaries.jsonl"

    append_record(ledger, {"canary_id": "missed-1", "must_catch": True, "caught": False})
    append_record(ledger, {"canary_id": "control-1", "must_catch": False, "caught": False})

    assert report(ledger) == {"caught": 0, "missed": 1, "recall": 0.0}
    assert len(ledger.read_text(encoding="utf-8").splitlines()) == 2
