import json
from pathlib import Path

from scripts.canary import append_record, report


TRACE_MARKER = (
    "ch:trace row=GR-209 spec=.coding-hermes/board/tasks.jsonl#GR-209 "
    "wave=none:serial test=tests/test_canary.py doc=docs/operations.md "
    "evidence=scripts/canary.py witness=none:fixture-or-mock-only "
    "verdict=none:pending-judge commit=none:pending "
    "memory=none:pending"
)


def _record(
    *,
    canary_id: str,
    expected_outcome: str = "caught",
    actual_verdict_outcome: str = "caught",
) -> dict[str, str]:
    return {
        "canary_id": canary_id,
        "defect_class": "fixture",
        "expected_outcome": expected_outcome,
        "actual_verdict_outcome": actual_verdict_outcome,
        "judged_commit": "abc123",
        "status": "verified",
        "artifact": "tests/fixtures/canary.json",
        "provenance": "test fixture",
    }


def test_caught_canary_round_trips_all_fields_and_reports_recall(
    tmp_path: Path,
) -> None:
    ledger = tmp_path / "canaries.jsonl"
    record = _record(canary_id="caught-1")

    append_record(ledger, record)

    assert json.loads(ledger.read_text(encoding="utf-8")) == record
    assert report(ledger) == {"caught": 1, "missed": 0, "recall": 1.0}

    try:
        append_record(ledger, {"canary_id": "incomplete"})
    except ValueError as error:
        message = str(error)
    else:
        raise AssertionError("append_record accepted an incomplete record")

    assert "defect_class" in message
    assert "provenance" in message


def test_missed_must_catch_canary_reports_zero_recall_and_two_lines(
    tmp_path: Path,
) -> None:
    ledger = tmp_path / "canaries.jsonl"

    append_record(
        ledger,
        _record(canary_id="missed-1", actual_verdict_outcome="missed"),
    )
    append_record(
        ledger,
        _record(
            canary_id="control-1",
            expected_outcome="informational",
            actual_verdict_outcome="missed",
        ),
    )

    assert len(ledger.read_text(encoding="utf-8").splitlines()) == 2
    assert report(ledger) == {"caught": 0, "missed": 1, "recall": 0.0}
