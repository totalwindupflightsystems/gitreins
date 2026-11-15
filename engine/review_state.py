"""Persistent second-pass finding state (GR-211).

The review-state document is deliberately separate from verdict history.  It is
an audited, locked transaction store for finding identity and human
 dispositions; verdicts receive only a bounded receipt.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import re
import tempfile
import time
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any, Literal

ACTIVE = "ACTIVE"
RESOLVED = "RESOLVED"
DISMISSED = "DISMISSED"
ReviewStateStatus = Literal["ACTIVE", "RESOLVED", "DISMISSED"]
HumanDecision = Literal["dismiss"]
_STATE_KEYS = {
    "fingerprint",
    "status",
    "file",
    "header",
    "body",
    "echo",
    "severity",
    "category",
    "first_seen_run",
    "last_seen_run",
    "dismissed_by",
    "dismissed_reason",
}
_HEX = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class HumanReviewDecision:
    fingerprint: str
    decision: HumanDecision
    reviewer: str
    reason: str = ""


@dataclass(frozen=True)
class FindingStateRecord:
    fingerprint: str
    status: ReviewStateStatus
    file: str
    header: str
    body: str
    echo: str
    severity: str
    category: str
    first_seen_run: int
    last_seen_run: int
    dismissed_by: str | None = None
    dismissed_reason: str | None = None


@dataclass(frozen=True)
class ReviewState:
    version: int
    run: int
    findings: tuple[FindingStateRecord, ...]


@dataclass(frozen=True)
class FindingStateResult:
    state: ReviewState
    emitted: tuple[dict[str, Any], ...]
    carried: tuple[str, ...]
    state_error: str | None = None


class ReviewStateError(RuntimeError):
    """A review-state read, validation, lock, or atomic-write failure."""


def normalize_header(header: str) -> str:
    """Normalize only identity header whitespace and case."""
    return " ".join(unicodedata.normalize("NFC", header).split()).casefold()


def finding_fingerprint(
    file: str | Mapping[str, Any], header: str | None = None, body: str | None = None
) -> str:
    """Return SHA-256(file + normalized header + exact body).

    The mapping form is retained for callers from the pre-store implementation;
    the three-string form is the public GR-211 API.
    """
    if isinstance(file, Mapping):
        # Legacy verdict compatibility: old GR-211 records used a normalized
        # JSON identity.  New state records always use the string API below.
        legacy = {
            "file": normalize_header(str(file.get("file", ""))).replace("\\", "/"),
            "line": int(file.get("line", 0)),
            "header": normalize_header(str(file.get("detection", ""))),
            "body": normalize_header(str(file.get("detail", ""))),
        }
        return hashlib.sha256(
            json.dumps(legacy, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
    if header is None or body is None:
        raise TypeError("finding_fingerprint requires file, header, and body")
    payload = f"{file}{normalize_header(header)}{body}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def finding_parts(finding: Mapping[str, Any]) -> tuple[str, str, str]:
    """Extract identity fields without changing the displayed detail body."""
    file = str(finding.get("file", ""))
    severity = str(finding.get("severity", ""))
    category = str(finding.get("category", "unknown"))
    detection = str(finding.get("detection", ""))
    body = str(finding.get("detail", ""))
    return file, f"{severity}|{category}|{detection}", body


def finding_echo(finding: Mapping[str, Any]) -> str:
    """Serialize the exact displayed finding text used for carried findings."""
    severity = str(finding.get("severity", ""))
    category = str(finding.get("category", "unknown"))
    file = str(finding.get("file", ""))
    line = finding.get("line", 0)
    detection = str(finding.get("detection", ""))
    body = str(finding.get("detail", ""))
    return f"[{severity}][{category}] {file}:{line} — {detection}\n{body}"


def review_state_path(workdir: str) -> str:
    return os.path.join(workdir, ".gitreins", "review-state.json")


def _record_from_json(raw: Any) -> FindingStateRecord:
    if not isinstance(raw, dict):
        raise ReviewStateError("invalid review state: finding")
    if set(raw) != _STATE_KEYS:
        raise ReviewStateError("invalid review state: finding keys")
    fp = raw.get("fingerprint")
    if not isinstance(fp, str) or not _HEX.fullmatch(fp):
        raise ReviewStateError("invalid review state: fingerprint")
    status = raw.get("status")
    if status not in {ACTIVE, RESOLVED, DISMISSED}:
        raise ReviewStateError("invalid review state: status")
    for key in ("file", "header", "body", "echo", "severity", "category"):
        if not isinstance(raw.get(key), str):
            raise ReviewStateError(f"invalid review state: {key}")
    for key in ("first_seen_run", "last_seen_run"):
        value = raw.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ReviewStateError(f"invalid review state: {key}")
    for key in ("dismissed_by", "dismissed_reason"):
        if raw.get(key) is not None and not isinstance(raw.get(key), str):
            raise ReviewStateError(f"invalid review state: {key}")
    if status != DISMISSED and (
        raw["dismissed_by"] is not None or raw["dismissed_reason"] is not None
    ):
        raise ReviewStateError("invalid review state: dismissed metadata")
    if status == DISMISSED and raw["dismissed_by"] is None:
        raise ReviewStateError("invalid review state: dismissed_by")
    expected = finding_fingerprint(raw["file"], raw["header"], raw["body"])
    if expected != fp:
        raise ReviewStateError("invalid review state: fingerprint")
    return FindingStateRecord(**raw)


def _state_from_json(raw: Any) -> ReviewState:
    if not isinstance(raw, dict) or set(raw) != {"version", "run", "findings"}:
        raise ReviewStateError("invalid review state: top-level keys")
    if raw.get("version") != 1:
        raise ReviewStateError("invalid review state: version")
    run = raw.get("run")
    if isinstance(run, bool) or not isinstance(run, int) or run < 0:
        raise ReviewStateError("invalid review state: run")
    findings = raw.get("findings")
    if not isinstance(findings, list):
        raise ReviewStateError("invalid review state: findings")
    return ReviewState(1, run, tuple(_record_from_json(item) for item in findings))


def _empty_state() -> ReviewState:
    return ReviewState(version=1, run=0, findings=())


def _record_dict(record: FindingStateRecord) -> dict[str, Any]:
    return asdict(record)


class ReviewStateStore:
    def __init__(self, workdir: str = ".") -> None:
        self.workdir = os.path.abspath(workdir)
        self.path = review_state_path(self.workdir)
        self.lock_path = os.path.join(self.workdir, ".gitreins", "review-state.lock")

    def load(self) -> ReviewState:
        try:
            with open(self.path, encoding="utf-8") as stream:
                return _state_from_json(json.load(stream))
        except FileNotFoundError:
            return _empty_state()
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ReviewStateError(f"corrupt review state: {exc}") from exc

    def _lock(self) -> int:
        os.makedirs(os.path.dirname(self.lock_path), exist_ok=True)
        try:
            fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        except OSError as exc:
            raise ReviewStateError(f"review state lock failed: {exc}") from exc
        deadline = time.monotonic() + 30.0
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return fd
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN) or time.monotonic() >= deadline:
                    os.close(fd)
                    raise ReviewStateError(f"review state lock failed: {exc}") from exc
                time.sleep(0.01)

    def _save(self, state: ReviewState) -> None:
        directory = os.path.dirname(self.path)
        os.makedirs(directory, exist_ok=True)
        payload = {
            "version": 1,
            "run": state.run,
            "findings": [_record_dict(r) for r in state.findings],
        }
        fd, temp_path = tempfile.mkstemp(prefix="review-state.", dir=directory, text=True)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_path, self.path)
            directory_fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError as exc:
            try:
                os.unlink(temp_path)
            except OSError:
                pass
            raise ReviewStateError(f"review state write failed: {exc}") from exc

    def reconcile_and_save(
        self,
        findings: Sequence[Mapping[str, Any]],
        human_decisions: Sequence[HumanReviewDecision] = (),
        max_findings: int = 10,
    ) -> FindingStateResult:
        if isinstance(max_findings, bool) or not isinstance(max_findings, int) or max_findings <= 0:
            raise ReviewStateError("max_findings must be a positive integer")
        lock_fd = self._lock()
        try:
            try:
                prior = self.load()
            except ReviewStateError as exc:
                # A corrupt document is never overwritten.  The caller gets a
                # bounded error receipt and can repair the file explicitly.
                if os.path.isfile(self.path):
                    quarantine = f"{self.path}.corrupt-{time.time_ns()}"
                    try:
                        with open(self.path, "rb") as source, open(quarantine, "xb") as target:
                            target.write(source.read())
                    except OSError:
                        pass
                    marker = os.path.join(
                        os.path.dirname(self.path), "review-state.recovery-required"
                    )
                    try:
                        with open(marker, "w", encoding="utf-8") as stream:
                            stream.write(f"{self.path}: {exc}\n")
                    except OSError:
                        pass
                raise
            run = prior.run + 1
            by_fp = {record.fingerprint: record for record in prior.findings}
            for decision in human_decisions:
                if decision.decision != "dismiss":
                    raise ReviewStateError(f"unsupported human decision: {decision.decision}")
                record = by_fp.get(decision.fingerprint)
                if record is None:
                    return FindingStateResult(
                        prior, (), (), f"unknown fingerprint: {decision.fingerprint}"
                    )
                by_fp[decision.fingerprint] = FindingStateRecord(
                    **{
                        **_record_dict(record),
                        "status": DISMISSED,
                        "dismissed_by": decision.reviewer,
                        "dismissed_reason": decision.reason,
                    }
                )

            current_by_fp: dict[str, FindingStateRecord] = {}
            current_order: list[str] = []
            for finding in findings:
                file, header, body = finding_parts(finding)
                fp = finding_fingerprint(file, header, body)
                if fp in current_by_fp:
                    continue
                category = str(finding.get("category", "unknown"))
                severity = str(finding.get("severity", ""))
                record = by_fp.get(fp)
                if record is not None and record.status == DISMISSED:
                    current_by_fp[fp] = record
                else:
                    first_seen = record.first_seen_run if record is not None else run
                    current_by_fp[fp] = FindingStateRecord(
                        fingerprint=fp,
                        status="ACTIVE",
                        file=file,
                        header=header,
                        body=body,
                        echo=finding_echo(finding),
                        severity=severity,
                        category=category,
                        first_seen_run=first_seen,
                        last_seen_run=run,
                    )
                current_order.append(fp)

            updated: dict[str, FindingStateRecord] = dict(by_fp)
            for fp, record in current_by_fp.items():
                updated[fp] = record
            for fp, record in by_fp.items():
                if fp not in current_by_fp and record.status == ACTIVE:
                    updated[fp] = FindingStateRecord(
                        **{
                            **_record_dict(record),
                            "status": RESOLVED,
                            "dismissed_by": None,
                            "dismissed_reason": None,
                        }
                    )

            ordered: list[FindingStateRecord] = []
            seen: set[str] = set()
            for fp in current_order + [record.fingerprint for record in prior.findings]:
                if fp not in seen:
                    ordered.append(updated[fp])
                    seen.add(fp)
            state = ReviewState(1, run, tuple(ordered))
            self._save(state)

            current_emitted = [
                _record_to_emitted(updated[fp])
                for fp in current_order
                if current_by_fp[fp].status == ACTIVE
            ]
            carried_records = [
                record
                for record in state.findings
                if record.status == ACTIVE and record.fingerprint not in current_by_fp
            ]
            carried_records.sort(key=lambda record: record.fingerprint)
            emitted = current_emitted + [_record_to_emitted(record) for record in carried_records]
            emitted = emitted[:max_findings]
            carried = tuple(
                record.fingerprint
                for record in carried_records
                if _record_to_emitted(record) in emitted
            )
            return FindingStateResult(state, tuple(emitted), carried)
        finally:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(lock_fd)

    def dismiss(self, decision: HumanReviewDecision) -> FindingStateResult:
        """Apply a human dismissal through the same locked transaction."""
        state = self.load()
        current = [
            _record_to_finding(record) for record in state.findings if record.status == ACTIVE
        ]
        return self.reconcile_and_save(current, (decision,), max_findings=max(1, len(current)))


def _record_to_emitted(record: FindingStateRecord) -> dict[str, Any]:
    line = 0
    match = re.search(r":(\d+) — ", record.echo)
    if match:
        line = int(match.group(1))
    return {
        "severity": record.severity,
        "file": record.file,
        "line": line,
        "detection": record.header.split("|", 2)[-1],
        "detail": record.body,
        "category": record.category,
        "fingerprint": record.fingerprint,
        "state": record.status,
        "echo": record.echo,
    }


def _record_to_finding(record: FindingStateRecord) -> dict[str, Any]:
    return {
        "severity": record.severity,
        "file": record.file,
        "line": 0,
        "detection": record.header.split("|", 2)[-1],
        "detail": record.body,
        "category": record.category,
    }


def reconcile_review_findings(
    workdir: str,
    findings: Sequence[Mapping[str, Any]],
    *,
    human_decisions: Sequence[HumanReviewDecision] = (),
    max_findings: int = 10,
) -> FindingStateResult:
    try:
        return ReviewStateStore(workdir).reconcile_and_save(findings, human_decisions, max_findings)
    except ReviewStateError as exc:
        return FindingStateResult(_empty_state(), (), (), str(exc))


def reconcile_review(
    workdir: str, review: dict[str, Any], prior: dict[str, Any] | None
) -> dict[str, Any]:
    """Compatibility adapter for pre-GR-211 callers.

    The judge uses :func:`reconcile_review_findings` and the JSON state file.
    Older integrations passed a prior verdict directly; retain that shape so
    old records and third-party callers remain readable while new state is
    written through the real store.
    """
    current = [item for item in review.get("findings", []) if isinstance(item, dict)]
    prior_findings: dict[str, dict[str, Any]] = {}
    if isinstance(prior, dict):
        block = prior.get("review", {}).get("finding_state")
        if isinstance(block, dict) and isinstance(block.get("findings"), list):
            for entry in block["findings"]:
                if isinstance(entry, dict) and isinstance(entry.get("finding"), dict):
                    prior_findings[str(entry.get("fingerprint"))] = entry
    emitted: list[dict[str, Any]] = []
    state_entries: list[dict[str, Any]] = []
    current_fps: set[str] = set()
    current_tree = ""
    try:
        from engine.fingerprint import compute

        current_tree = compute(workdir).digest
    except Exception:
        pass
    prior_tree = (
        str(prior.get("working_tree_content_fingerprint", "")) if isinstance(prior, dict) else ""
    )
    unchanged = bool(current_tree and prior_tree and current_tree == prior_tree)
    for finding in current:
        fp = finding_fingerprint(finding)
        current_fps.add(fp)
        old = prior_findings.get(fp)
        if old and old.get("state") == DISMISSED:
            state_entries.append(old)
            continue
        if unchanged and old and old.get("state") == ACTIVE:
            emitted.append(dict(old.get("finding", finding)))
            state_entries.append(old)
            continue
        item = dict(finding)
        item.update({"fingerprint": fp, "state": ACTIVE})
        emitted.append(item)
        state_entries.append({"fingerprint": fp, "state": ACTIVE, "finding": item})
    for fp, old in prior_findings.items():
        if fp in current_fps:
            continue
        if old.get("state") == ACTIVE:
            state_entries.append({**old, "state": RESOLVED})
    result = dict(review)
    result["findings"] = emitted
    result["finding_state"] = {
        "version": 1,
        "tree_fingerprint": "",
        "findings": state_entries,
        "summary": {
            "carried": sum(
                1
                for item in state_entries
                if (
                    unchanged
                    and item.get("state") == ACTIVE
                    and item["fingerprint"] in current_fps
                    and item["fingerprint"] in prior_findings
                )
            ),
            "new": sum(
                1
                for item in emitted
                if not unchanged or item.get("fingerprint") not in prior_findings
            ),
            "resolved": sum(1 for item in state_entries if item.get("state") == RESOLVED),
            "dismissed": sum(1 for item in state_entries if item.get("state") == DISMISSED),
        },
    }
    return result
