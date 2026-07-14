"""Stable, bounded JSON evidence contract for GitReins automation consumers.

The v1 contract is intentionally small.  Every string is redacted and capped,
collections are bounded, and the final JSON document is guaranteed not to
exceed ``MAX_EVIDENCE_BYTES``.  Human-readable CLI output remains separate.
"""

from __future__ import annotations

import copy
import json
import re
from datetime import datetime, timezone
from typing import Any, Iterable

from engine.version import __version__

EVIDENCE_SCHEMA = "https://gitreins.dev/schemas/evidence/v1.json"
EVIDENCE_SCHEMA_VERSION = "1.0"
MAX_EVIDENCE_BYTES = 32 * 1024
MAX_TEXT_CHARS = 2048
MAX_CHECKS = 32
MAX_REPORT_ENTRIES = 50

_SECRET_PATTERNS = (
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+"),
    re.compile(r"(?i)((?:token|secret|password|api[_-]?key|authorization|credential)\s*[:=]\s*)[^\s,;'\"}]+"),
    re.compile(r"\b(?:gh[pousr]_|github_pat_|xox[baprs]-|sk-)[A-Za-z0-9_.-]{12,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
)


def redact_text(value: object, limit: int = MAX_TEXT_CHARS) -> tuple[str, bool, bool]:
    """Return redacted/capped text and ``(redacted, truncated)`` flags."""
    text = str(value)
    redacted = False
    for pattern in _SECRET_PATTERNS:
        replaced, count = pattern.subn(
            lambda match: (match.group(1) if match.lastindex else "") + "[REDACTED]",
            text,
        )
        if count:
            redacted = True
            text = replaced
    truncated = len(text) > limit
    if truncated:
        text = text[: max(0, limit - 14)] + "... [truncated]"
    return text, redacted, truncated


def _safe_text(value: object, flags: dict[str, bool], limit: int = MAX_TEXT_CHARS) -> str:
    text, redacted, truncated = redact_text(value, limit)
    flags["redacted"] = flags["redacted"] or redacted
    flags["truncated"] = flags["truncated"] or truncated
    return text


def _outcome(passed: bool | None, error: bool = False) -> str:
    if error:
        return "error"
    if passed is None:
        return "unknown"
    return "pass" if passed else "fail"


def _base(command: str, scope: str, passed: bool | None, flags: dict[str, bool]) -> dict[str, Any]:
    return {
        "$schema": EVIDENCE_SCHEMA,
        "schemaVersion": EVIDENCE_SCHEMA_VERSION,
        "producer": {"name": "gitreins", "version": __version__},
        "command": command,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "scope": scope,
        "outcome": _outcome(passed),
        "passed": passed,
        "summary": "",
        "checks": [],
        "metadata": {
            "redacted": True,
            "redactionsApplied": False,
            "truncated": False,
        },
    }


def guard_evidence(result: Any, scope: str) -> dict[str, Any]:
    flags = {"redacted": False, "truncated": False}
    document = _base("guard", scope, bool(result.passed), flags)
    checks = []
    for item in list(result.results)[:MAX_CHECKS]:
        raw = item.output or item.error or ("check passed" if item.passed else "check failed")
        checks.append({
            "id": _safe_text(item.name, flags, 128),
            "outcome": _outcome(bool(item.passed), bool(item.error and not item.output)),
            "passed": bool(item.passed),
            "summary": _safe_text(raw, flags),
        })
    if len(result.results) > MAX_CHECKS:
        flags["truncated"] = True
    fallback_summary = "All guards passed" if result.passed else "One or more guards failed"
    document["summary"] = _safe_text(result.summary or fallback_summary, flags)
    document["checks"] = checks
    document["metadata"].update({
        "redactionsApplied": flags["redacted"],
        "truncated": flags["truncated"],
        "checkCount": len(result.results),
        "changedFileCount": int(result.extra.get("changed_count", 0)),
    })
    return document


def judge_evidence(result: Any, task: Any, scope: str, ephemeral: bool) -> dict[str, Any]:
    flags = {"redacted": False, "truncated": False}
    document = _base("judge", scope, bool(result.passed), flags)
    document["subject"] = {
        "taskId": _safe_text(task.id, flags, 128),
        "title": _safe_text(task.title, flags, 512),
        "ephemeral": bool(ephemeral),
    }
    checks: list[dict[str, Any]] = []
    if result.pipeline_result:
        if result.pipeline_result.get("error"):
            document["outcome"] = "error"
        for stage_id, stage in list(result.pipeline_result.get("stages", {}).items())[:MAX_CHECKS]:
            checks.append({
                "id": _safe_text(stage_id, flags, 128),
                "outcome": _outcome(bool(stage.get("passed"))),
                "passed": bool(stage.get("passed")),
                "summary": _safe_text(stage.get("summary", ""), flags),
            })
    elif result.tier1:
        for item in list(result.tier1.results)[:MAX_CHECKS]:
            checks.append({
                "id": _safe_text(item.name, flags, 128),
                "outcome": _outcome(bool(item.passed)),
                "passed": bool(item.passed),
                "summary": _safe_text(item.output or item.error, flags),
            })
    if result.verdict:
        remaining = max(0, MAX_CHECKS - len(checks))
        for index, item in enumerate(list(result.verdict.items)[:remaining]):
            passed = item.status == "PASS"
            checks.append({
                "id": f"criterion-{index + 1}",
                "outcome": _outcome(passed),
                "passed": passed,
                "summary": _safe_text(f"{item.criterion}: {item.detail}", flags),
            })
    document["summary"] = _safe_text(result.summary, flags)
    document["checks"] = checks
    document["metadata"].update({
        "redactionsApplied": flags["redacted"],
        "truncated": flags["truncated"] or len(checks) >= MAX_CHECKS,
        "checkCount": len(checks),
        "historyPersisted": not ephemeral,
    })
    return document


def report_evidence(entries: Iterable[dict[str, Any]], storage_mode: str) -> dict[str, Any]:
    raw_entries = list(entries)
    flags = {"redacted": False, "truncated": len(raw_entries) > MAX_REPORT_ENTRIES}
    selected = raw_entries[:MAX_REPORT_ENTRIES]
    passed_count = sum(1 for entry in selected if entry.get("passed") is True)
    failed_count = sum(1 for entry in selected if entry.get("passed") is False)
    document = _base("report", "history", None, flags)
    document["summary"] = (
        f"{len(selected)} recent verdicts: {passed_count} pass, {failed_count} fail"
    )
    checks = []
    for entry in selected:
        entry_passed = entry.get("passed") if isinstance(entry.get("passed"), bool) else None
        entry_summary = entry.get("task_title") or entry.get("summary") or "verdict"
        checks.append({
            "id": _safe_text(entry.get("task_id", "unknown"), flags, 128),
            "outcome": _outcome(entry_passed),
            "passed": entry_passed,
            "summary": _safe_text(entry_summary, flags),
        })
    document["checks"] = checks
    document["metadata"].update({
        "redactionsApplied": flags["redacted"],
        "truncated": flags["truncated"],
        "checkCount": len(raw_entries),
        "storage": _safe_text(storage_mode, flags, 32),
    })
    return document


def dumps_evidence(document: dict[str, Any], max_bytes: int = MAX_EVIDENCE_BYTES) -> str:
    """Serialize an evidence document under a hard byte ceiling.

    The caller-provided document is never mutated. Checks are dropped from the
    tail if necessary; if fixed fields alone are oversized, summaries are
    reduced. The result always remains valid v1 JSON.
    """
    bounded = copy.deepcopy(document)
    metadata = bounded.setdefault("metadata", {})
    checks = bounded.setdefault("checks", [])

    def encode() -> str:
        return json.dumps(bounded, separators=(",", ":"), ensure_ascii=False, sort_keys=True)

    payload = encode()
    while len(payload.encode("utf-8")) > max_bytes and checks:
        checks.pop()
        metadata["truncated"] = True
        payload = encode()

    if len(payload.encode("utf-8")) > max_bytes:
        no_flags = {"redacted": False, "truncated": False}
        bounded["summary"] = _safe_text(bounded.get("summary", ""), no_flags, 256)
        subject = bounded.get("subject")
        if isinstance(subject, dict) and "title" in subject:
            subject["title"] = _safe_text(subject["title"], no_flags, 128)
        metadata["truncated"] = True
        payload = encode()

    if len(payload.encode("utf-8")) > max_bytes:
        # Defensive fallback for future v1 extensions with unexpectedly large fields.
        bounded = {
            "$schema": EVIDENCE_SCHEMA,
            "schemaVersion": EVIDENCE_SCHEMA_VERSION,
            "producer": {"name": "gitreins", "version": __version__},
            "command": str(document.get("command", "unknown"))[:32],
            "generatedAt": document.get("generatedAt"),
            "scope": str(document.get("scope", "unknown"))[:32],
            "outcome": document.get("outcome", "error"),
            "passed": document.get("passed"),
            "summary": "Evidence exceeded output limit",
            "checks": [],
            "metadata": {"redacted": True, "redactionsApplied": True, "truncated": True},
        }
        payload = json.dumps(bounded, separators=(",", ":"), ensure_ascii=False, sort_keys=True)

    return payload
