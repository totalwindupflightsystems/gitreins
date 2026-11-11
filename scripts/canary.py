"""Append-only fixture canary ledger and recall reporting."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def append_record(path: str | Path, record: dict[str, Any]) -> None:
    """Append one canary record without changing existing ledger entries."""
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
            if not record["must_catch"]:
                continue
            if record["caught"]:
                caught += 1
            else:
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
