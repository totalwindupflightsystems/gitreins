import hashlib
import json
import stat
from pathlib import Path

import pytest

from scripts.canary import CanaryProvenanceError, append_record, report, run_canary


TRACE_MARKER = (
    "ch:trace row=GR-209 spec=.coding-hermes/board/tasks.jsonl#GR-209 "
    "wave=none:serial test=tests/test_canary.py doc=docs/operations.md "
    "evidence=scripts/canary.py witness=none:fixture-or-mock-only "
    "verdict=none:pending-judge commit=none:pending "
    "memory=none:pending"
)

# ch:trace row=GR-224 spec=.coding-hermes/board/tasks.jsonl#GR-224 wave=none:serial test=tests/test_canary.py::test_canary_runner_uses_external_binary_and_verifies_persisted_verdict doc=docs/operations.md evidence=scripts/canary.py witness=none:worker-local-fake-binary verdict=none:foreman-judge-after-merge commit=none:worker-commit-after-commit memory=none:foreman-managed


def _record(
    tmp_path: Path,
    *,
    canary_id: str,
    expected_outcome: str = "caught",
    actual_verdict_outcome: str = "caught",
    commit: str = "abc123",
    task_id: str | None = None,
    worktree: Path | None = None,
) -> dict[str, object]:
    task_id = task_id or f"task:{canary_id}"
    worktree = worktree or tmp_path / "worktree"
    worktree.mkdir(parents=True, exist_ok=True)
    artifact = tmp_path / f"{canary_id}.verdict.json"
    artifact.write_text(
        json.dumps(
            {
                "task_id": task_id,
                "commit": commit,
                "worktree": str(worktree),
                "passed": actual_verdict_outcome == "caught",
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    return {
        "canary_id": canary_id,
        "defect_class": "fixture",
        "expected_outcome": expected_outcome,
        "actual_verdict_outcome": actual_verdict_outcome,
        "judged_commit": commit,
        "status": "verified",
        "artifact": str(artifact),
        "provenance": {
            "artifact_path": str(artifact),
            "artifact_sha256": digest,
            "task_id": task_id,
            "judged_commit": commit,
            "worktree": str(worktree),
        },
    }


def test_caught_canary_round_trips_all_fields_and_reports_recall(
    tmp_path: Path,
) -> None:
    ledger = tmp_path / "canaries.jsonl"
    record = _record(tmp_path, canary_id="caught-1")

    append_record(ledger, record)

    assert json.loads(ledger.read_text(encoding="utf-8")) == record
    assert report(ledger) == {"caught": 1, "missed": 0, "excluded": 0, "recall": 1.0}

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
        _record(tmp_path, canary_id="missed-1", actual_verdict_outcome="missed"),
    )
    append_record(
        ledger,
        _record(
            tmp_path,
            canary_id="control-1",
            expected_outcome="informational",
            actual_verdict_outcome="missed",
        ),
    )

    assert len(ledger.read_text(encoding="utf-8").splitlines()) == 2
    assert report(ledger) == {"caught": 0, "missed": 1, "excluded": 0, "recall": 0.0}


def test_unverifiable_forged_record_is_excluded_from_recall(tmp_path: Path) -> None:
    ledger = tmp_path / "canaries.jsonl"
    valid = _record(tmp_path, canary_id="valid")
    forged = _record(tmp_path, canary_id="forged")
    forged["provenance"]["artifact_sha256"] = "0" * 64  # type: ignore[index]
    append_record(ledger, valid)
    append_record(ledger, forged)

    assert report(ledger) == {"caught": 1, "missed": 0, "excluded": 1, "recall": 1.0}


def test_missing_provenance_is_excluded_from_recall(tmp_path: Path) -> None:
    ledger = tmp_path / "canaries.jsonl"
    record = _record(tmp_path, canary_id="missing-provenance")
    record["provenance"] = "caller supplied"  # type: ignore[assignment]
    append_record(ledger, record)

    assert report(ledger) == {"caught": 0, "missed": 0, "excluded": 1, "recall": 0.0}


def test_digest_or_commit_mismatch_is_rejected(tmp_path: Path) -> None:
    ledger = tmp_path / "canaries.jsonl"
    record = _record(tmp_path, canary_id="mismatch")
    record["provenance"]["artifact_sha256"] = "f" * 64  # type: ignore[index]
    append_record(ledger, record)

    assert report(ledger)["excluded"] == 1
    with pytest.raises(CanaryProvenanceError, match="digest mismatch"):
        from scripts.canary import verify_record_provenance

        verify_record_provenance(record, ledger)


def test_provenance_commit_mismatch_is_excluded(tmp_path: Path) -> None:
    ledger = tmp_path / "canaries.jsonl"
    record = _record(tmp_path, canary_id="wrong-commit")
    artifact = Path(record["artifact"])
    artifact.write_text(
        artifact.read_text(encoding="utf-8").replace("abc123", "def456"),
        encoding="utf-8",
    )
    record["provenance"]["artifact_sha256"] = hashlib.sha256(  # type: ignore[index]
        artifact.read_bytes()
    ).hexdigest()
    append_record(ledger, record)

    assert report(ledger) == {"caught": 0, "missed": 0, "excluded": 1, "recall": 0.0}


def test_canary_runner_uses_external_binary_and_verifies_persisted_verdict(
    tmp_path: Path,
) -> None:
    binary = tmp_path / "fake-gitreins"
    binary.write_text(
        "#!/usr/bin/env python3\n"
        "import json, pathlib, sys\n"
        "root = pathlib.Path.cwd()\n"
        "task = sys.argv[2]\n"
        "commit = __import__('subprocess').check_output(['git', 'rev-parse', 'HEAD'], text=True).strip()\n"
        "path = root / '.gitreins' / 'verdicts' / 'verdict.json'\n"
        "path.parent.mkdir(parents=True, exist_ok=True)\n"
        "path.write_text(json.dumps({'task_id': task, 'commit': commit, 'worktree': str(root), 'passed': False}))\n",
        encoding="utf-8",
    )
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    ledger = tmp_path / "runner.jsonl"

    records = run_canary(ledger, binary_path=str(binary), scratch_root=tmp_path)

    assert len(records) == 1
    assert report(ledger) == {"caught": 1, "missed": 0, "excluded": 0, "recall": 1.0}
    assert records[0]["provenance"]["artifact_sha256"]
