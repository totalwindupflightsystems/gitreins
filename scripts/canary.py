"""Run provenance-bound canaries and report their append-only recall ledger.

The report path deliberately does not trust ``judged_commit`` or a caller's
status field.  A record contributes only when its persisted verdict artifact is
readable, its SHA-256 matches the recorded digest, and the artifact binds the
same task, commit, and worktree as the record.  ``CanaryRunner`` invokes an
external GitReins executable in a disposable repository; it never fabricates a
verdict in this process.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence


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


class CanaryProvenanceError(ValueError):
    """The record or persisted verdict cannot be bound to this canary run."""


def _sha256(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except (OSError, UnicodeError) as exc:
        raise CanaryProvenanceError(f"cannot read verdict artifact {path}: {exc}") from exc


def _load_verdict(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CanaryProvenanceError(f"cannot parse verdict artifact {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise CanaryProvenanceError(f"verdict artifact is not a JSON object: {path}")
    return value


def verify_verdict_artifact(
    artifact: str | Path,
    *,
    task_id: str,
    judged_commit: str,
    worktree: str | Path,
    artifact_sha256: str | None = None,
) -> dict[str, Any]:
    """Read and verify one verdict produced for a just-created scratch run.

    The expected task, commit, and worktree are supplied by the runner, not
    taken from the verdict.  This makes a forged JSON document or a verdict
    copied from another run fail closed.  The returned mapping is the parsed
    persisted document, not a synthesized verdict.
    """
    path = Path(artifact)
    if not path.is_file():
        raise CanaryProvenanceError(f"missing verdict artifact: {path}")
    digest = _sha256(path)
    if not artifact_sha256 or digest != artifact_sha256:
        expected = artifact_sha256 or "<missing>"
        raise CanaryProvenanceError(
            f"verdict artifact digest mismatch for {path}: expected {expected}, got {digest}"
        )
    verdict = _load_verdict(path)
    if verdict.get("task_id") != task_id:
        raise CanaryProvenanceError(
            f"verdict task mismatch for {path}: expected {task_id!r}, "
            f"got {verdict.get('task_id')!r}"
        )
    if verdict.get("commit") != judged_commit:
        raise CanaryProvenanceError(
            f"verdict commit mismatch for {path}: expected {judged_commit!r}, "
            f"got {verdict.get('commit')!r}"
        )
    actual_worktree = verdict.get("worktree")
    if not isinstance(actual_worktree, str) or (
        os.path.realpath(actual_worktree) != os.path.realpath(str(worktree))
    ):
        raise CanaryProvenanceError(
            f"verdict worktree mismatch for {path}: expected {os.path.realpath(str(worktree))!r}, "
            f"got {actual_worktree!r}"
        )
    return verdict


def _record_provenance(record: dict[str, Any], ledger: Path) -> tuple[Path, str, str, str]:
    """Resolve the caller-recorded provenance tuple, rejecting legacy strings."""
    provenance = record.get("provenance")
    if not isinstance(provenance, dict):
        raise CanaryProvenanceError("record provenance is not a persisted-verdict mapping")
    artifact_value = record.get("artifact")
    provenance_path = provenance.get("artifact_path")
    if not isinstance(artifact_value, str) or not isinstance(provenance_path, str):
        raise CanaryProvenanceError("record is missing artifact and provenance.artifact_path")
    artifact = Path(artifact_value)
    if not artifact.is_absolute():
        artifact = ledger.parent / artifact
    if os.path.realpath(str(artifact)) != os.path.realpath(provenance_path):
        raise CanaryProvenanceError("record artifact path differs from provenance.artifact_path")
    digest = provenance.get("artifact_sha256")
    if not isinstance(digest, str) or len(digest) != 64:
        raise CanaryProvenanceError("record is missing provenance.artifact_sha256")
    task_id = provenance.get("task_id")
    commit = provenance.get("judged_commit")
    worktree = provenance.get("worktree")
    if not all(isinstance(value, str) and value for value in (task_id, commit, worktree)):
        raise CanaryProvenanceError("record provenance must name task, commit, and worktree")
    if record.get("judged_commit") != commit:
        raise CanaryProvenanceError("record judged_commit differs from provenance.judged_commit")
    return artifact, digest, task_id, worktree


def verify_record_provenance(record: dict[str, Any], ledger_path: str | Path) -> dict[str, Any]:
    """Verify a ledger record against the exact persisted artifact it names."""
    ledger = Path(ledger_path)
    artifact, digest, task_id, worktree = _record_provenance(record, ledger)
    return verify_verdict_artifact(
        artifact,
        task_id=task_id,
        judged_commit=str(record["judged_commit"]),
        worktree=worktree,
        artifact_sha256=digest,
    )


def append_record(path: str | Path, record: dict[str, Any]) -> None:
    """Append one canary record without changing existing ledger entries.

    Append remains intentionally append-only.  Records that do not carry
    verifiable persisted-verdict provenance are retained for auditability but
    are reported as excluded rather than being allowed to inflate recall.
    """
    missing = sorted(_REQUIRED_KEYS - record.keys())
    if missing:
        raise ValueError(f"missing required keys: {', '.join(missing)}")

    ledger = Path(path)
    ledger.parent.mkdir(parents=True, exist_ok=True)
    with ledger.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, sort_keys=True))
        stream.write("\n")


def report(path: str | Path) -> dict[str, float | int]:
    """Return verified caught/missed recall and excluded-record count.

    Malformed JSON lines and provenance failures are excluded and counted in
    ``excluded``.  They never become misses, because doing so would let a
    caller manufacture the denominator by submitting unverifiable records.
    """
    caught = 0
    missed = 0
    excluded = 0
    ledger = Path(path)
    if ledger.exists():
        for line in ledger.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                record = json.loads(line)
                if not isinstance(record, dict) or record.get("expected_outcome") != "caught":
                    continue
                verify_record_provenance(record, ledger)
            except (json.JSONDecodeError, CanaryProvenanceError, TypeError, KeyError):
                excluded += 1
                continue
            if record.get("actual_verdict_outcome") == "caught":
                caught += 1
            else:
                missed += 1
    total = caught + missed
    return {
        "caught": caught,
        "missed": missed,
        "excluded": excluded,
        "recall": caught / total if total else 0.0,
    }


CommandRunner = Callable[[Sequence[str], Path], subprocess.CompletedProcess[str]]


@dataclass(frozen=True)
class CanaryRunner:
    """External-binary canary runner using only disposable scratch repositories."""

    binary_path: str = "gitreins"
    scratch_root: Path | None = None
    command_runner: CommandRunner | None = None

    def _resolve_binary(self) -> str:
        resolved = (
            shutil.which(self.binary_path) if os.sep not in self.binary_path else self.binary_path
        )
        if not resolved or not os.access(resolved, os.X_OK):
            raise RuntimeError(f"installed GitReins binary is not executable: {self.binary_path}")
        return os.path.realpath(resolved)

    @staticmethod
    def _safe_environment() -> dict[str, str]:
        env = dict(os.environ)
        for key in (
            "GITREINS_LLM_API_KEY",
            "OPENAI_API_KEY",
            "OPENROUTER_API_KEY",
            "ANTHROPIC_API_KEY",
            "DEEPSEEK_API_KEY",
            "ZAI_API_KEY",
            "XAI_API_KEY",
        ):
            env.pop(key, None)
        return env

    def _run_command(self, argv: Sequence[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        if self.command_runner is not None:
            return self.command_runner(argv, cwd)
        return subprocess.run(
            list(argv),
            cwd=cwd,
            env=self._safe_environment(),
            capture_output=True,
            text=True,
            check=False,
            timeout=300,
        )

    def _git(self, scratch: Path, *args: str) -> str:
        result = subprocess.run(
            ["git", *args], cwd=scratch, capture_output=True, text=True, check=False, timeout=30
        )
        if result.returncode:
            raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
        return result.stdout.strip()

    def _fixture(self, scratch: Path, canary_id: str) -> str:
        (scratch / ".gitreins").mkdir()
        (scratch / ".gitreins" / "config.yaml").write_text(
            "guards:\n"
            "  secrets: false\n"
            "  lint: false\n"
            "  tests: true\n"
            "  test_mode: full\n"
            "  test_command: python -c 'raise SystemExit(1)'\n"
            "  allow_skips: false\n"
            "defaults:\n"
            "  check_for_updates: false\n"
            "history:\n  enabled: false\n",
            encoding="utf-8",
        )
        (scratch / "fixture_defect.py").write_text(
            f"# Deliberate canary defect {canary_id}\nraise RuntimeError('canary defect')\n",
            encoding="utf-8",
        )
        self._git(scratch, "add", ".")
        self._git(scratch, "commit", "-m", "canary fixture")
        return self._git(scratch, "rev-parse", "HEAD")

    def run(
        self,
        ledger_path: str | Path,
        *,
        cycles: int = 1,
        defects_per_cycle: int = 1,
    ) -> list[dict[str, Any]]:
        """Create, judge, and record ``defects_per_cycle`` scratch defects per cycle.

        The external command is ``<binary> judge <task> --ephemeral
        --persist-verdict --skip-tier2 --scope working-tree ...``.  The
        persisted ``.gitreins/verdicts/verdict.json`` is copied nowhere and is
        retained in the scratch tree named by the record, so report-time digest
        and provenance checks remain possible.  ``cycles`` and
        ``defects_per_cycle`` must both be positive.
        """
        if cycles < 1 or defects_per_cycle < 1:
            raise ValueError("cycles and defects_per_cycle must be positive")
        binary = self._resolve_binary()
        ledger = Path(ledger_path)
        records: list[dict[str, Any]] = []
        root = Path(
            tempfile.mkdtemp(
                prefix="gitreins-canary-",
                dir=str(self.scratch_root) if self.scratch_root is not None else None,
            )
        )
        for cycle in range(1, cycles + 1):
            for defect in range(1, defects_per_cycle + 1):
                canary_id = f"canary-{cycle}-{defect}"
                scratch = root / canary_id
                scratch.mkdir()
                self._git(scratch, "init", "-q", "-b", "main")
                self._git(scratch, "config", "user.name", "GitReins Canary")
                self._git(scratch, "config", "user.email", "gitreins-canary@localhost")
                judged_commit = self._fixture(scratch, canary_id)
                task_id = f"canary:{canary_id}"
                artifact = scratch / ".gitreins" / "verdicts" / "verdict.json"
                result = self._run_command(
                    [
                        binary,
                        "judge",
                        task_id,
                        "--ephemeral",
                        "--persist-verdict",
                        "--skip-tier2",
                        "--scope",
                        "working-tree",
                        "--title",
                        f"Canary defect {canary_id}",
                        "--criterion",
                        "The scratch fixture defect is caught",
                    ],
                    scratch,
                )
                if not artifact.is_file():
                    raise RuntimeError(
                        f"GitReins exited {result.returncode} but produced no persisted verdict: "
                        f"{artifact}\nstderr: {result.stderr.strip()}"
                    )
                digest = _sha256(artifact)
                verdict = verify_verdict_artifact(
                    artifact,
                    task_id=task_id,
                    judged_commit=judged_commit,
                    worktree=scratch,
                    artifact_sha256=digest,
                )
                actual = "caught" if verdict.get("passed") is False else "missed"
                records.append(
                    {
                        "canary_id": canary_id,
                        "defect_class": "scratch-fixture",
                        "expected_outcome": "caught",
                        "actual_verdict_outcome": actual,
                        "judged_commit": judged_commit,
                        "status": "verified",
                        "artifact": str(artifact),
                        "provenance": {
                            "artifact_path": str(artifact),
                            "artifact_sha256": digest,
                            "task_id": task_id,
                            "judged_commit": judged_commit,
                            "worktree": str(scratch),
                        },
                    }
                )
        for record in records:
            append_record(ledger, record)
        return records


def run_canary(
    ledger_path: str | Path,
    *,
    binary_path: str = "gitreins",
    scratch_root: str | Path | None = None,
    cycles: int = 1,
    defects_per_cycle: int = 1,
) -> list[dict[str, Any]]:
    """Convenience API for a real external-binary canary cycle."""
    return CanaryRunner(
        binary_path=binary_path,
        scratch_root=Path(scratch_root) if scratch_root is not None else None,
    ).run(ledger_path, cycles=cycles, defects_per_cycle=defects_per_cycle)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Report provenance-verified canary recall or run scratch canaries."
    )
    parser.add_argument("ledger", type=Path, help="append-only JSONL ledger")
    parser.add_argument(
        "--run",
        action="store_true",
        help=(
            "create scratch defects, invoke the external GitReins binary, "
            "and append verified records"
        ),
    )
    parser.add_argument(
        "--binary",
        default="gitreins",
        help="installed GitReins executable (default: gitreins)",
    )
    parser.add_argument(
        "--scratch-root",
        type=Path,
        help="directory for retained disposable canary trees",
    )
    parser.add_argument("--cycles", type=int, default=1, help="number of cycles (default: 1)")
    parser.add_argument(
        "--defects-per-cycle", type=int, default=1, help="fixture defects per cycle (default: 1)"
    )
    args = parser.parse_args()
    if args.run:
        records = run_canary(
            args.ledger,
            binary_path=args.binary,
            scratch_root=args.scratch_root,
            cycles=args.cycles,
            defects_per_cycle=args.defects_per_cycle,
        )
        print(json.dumps({"records": len(records), "report": report(args.ledger)}, sort_keys=True))
    else:
        print(json.dumps(report(args.ledger), sort_keys=True))


if __name__ == "__main__":
    main()
