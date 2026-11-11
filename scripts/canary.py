"""Append-only fixture canary ledger and recall reporting."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


_REQUIRED_KEYS = {
    "canary_id",
    "defect_class",
    "expected_outcome",
    "actual_verdict_outcome",
    "judged_commit",
    "status",
    "artifact",
    "provenance",
}


def append_record(path: str | Path, record: dict[str, Any]) -> None:
    """Append one canary record without changing existing ledger entries."""
    missing = sorted(_REQUIRED_KEYS - record.keys())
    if missing:
        raise ValueError(f"missing required keys: {', '.join(missing)}")

    ledger = Path(path)
    with ledger.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, sort_keys=True))
        stream.write("\n")


def report(path: str | Path) -> dict[str, float | int]:
    """Return caught, missed, and recall for must-catch canary records."""
    caught = 0
    missed = 0
    ledger = Path(path)
    if ledger.exists():
        for line in ledger.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            if record["expected_outcome"] != "caught":
                continue
            if (
                record["expected_outcome"] == "caught"
                and record["actual_verdict_outcome"] == "caught"
            ):
                caught += 1
            elif record["expected_outcome"] == "caught":
                missed += 1
    total = caught + missed
    return {
        "caught": caught,
        "missed": missed,
        "recall": caught / total if total else 0.0,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Report fixture canary recall")
    parser.add_argument("ledger", type=Path)
    args = parser.parse_args()
    print(json.dumps(report(args.ledger), sort_keys=True))


if __name__ == "__main__":
    main()
