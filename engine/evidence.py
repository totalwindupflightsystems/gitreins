"""Evidence for automation consumers — bound a verdict's run, emit the v1 document.

Two families live here:

**Verdict evidence (EVID-001 era).** A verdict records *what* was decided; the
run that produced it (the brief the worker was handed, the driver log it wrote,
the patch the judge graded) usually lives outside the repository — often in
``/tmp`` — and dies with the tick. This module copies those artifacts next to
``verdict.json`` so one verdict directory is a self-contained audit unit, and
serves them back to the judgment viewer.

**Evidence v1 emitters (EVID-002).** :func:`guard_evidence`,
:func:`judge_evidence` and :func:`report_evidence` build the bounded, redacted
JSON document described by ``schemas/evidence-v1.schema.json`` and
``docs/evidence-contract-v1.md``; :func:`dumps_evidence` serializes it under a
hard 32 KiB ceiling. Every string crosses :func:`redact_text` (secret-shaped
spans replaced, then capped at 2048 chars) and the whole document is re-swept
before serialization, so ``metadata.redacted`` is always true and
``redactionsApplied`` says whether a replacement actually happened.

Sources (all optional; a verdict is never failed or delayed by missing
evidence — every failure is swallowed and reported as a missing item):

  brief  ``GITREINS_WORKER_BRIEF`` — path to the worker brief that was
         dispatched for the task, else ``<workdir>/.gitreins/worker-brief.md``
  log    ``GITREINS_DRIVER_LOG`` — path to the worker/driver log; only the tail
         is kept, because the interesting part of a driver log is its end
  patch  the patch of the commit the verdict stamped — the fix as landed
  worktree  ``git diff HEAD`` — whatever was uncommitted, i.e. what the judge
         read when it graded; recorded separately so a permanently dirty tree
         (generated files, graph caches) cannot pass itself off as the fix

Everything is bounded: a brief keeps its head, a log keeps its tail, and the
patch is capped — each artifact records ``bytes``/``truncated`` so a reader can
tell a small artifact from a clipped one.
"""

from __future__ import annotations

import copy
import json
import os
import re
import subprocess
from datetime import datetime, timezone
from typing import Any

from engine.version import __version__ as _gitreins_version

BRIEF_ENV = "GITREINS_WORKER_BRIEF"
LOG_ENV = "GITREINS_DRIVER_LOG"

BRIEF_NAME = "brief"
LOG_NAME = "log"
PATCH_NAME = "patch"
WORKTREE_NAME = "worktree"

BRIEF_FILENAME = "worker-brief.md"
LOG_FILENAME = "driver-log.tail.txt"
PATCH_FILENAME = "commit.patch"
WORKTREE_FILENAME = "worktree.patch"

#: Character labels used by the viewer and the docs.
ITEM_LABELS = {
    BRIEF_NAME: "Worker brief",
    LOG_NAME: "Driver log (tail)",
    PATCH_NAME: "Landed commit patch",
    WORKTREE_NAME: "Working-tree diff (graded)",
}

#: Bounds. The patches are the largest artifacts on purpose: a verdict for a
#: multi-file change is unreadable without them, and 256 KiB is still far below
#: what a single HTTP response can carry comfortably.
MAX_BRIEF_BYTES = 32 * 1024
MAX_LOG_BYTES = 16 * 1024
MAX_PATCH_BYTES = 256 * 1024

#: Marker written in place of the dropped head (tail reads) or tail (head
#: reads). It names the arithmetic so a reader never has to guess the loss.
DROP_MARKER = "... [{dropped} of {total} bytes dropped]\n"


def _decode(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")


def _bounded_head(data: bytes, cap: int) -> tuple[str, bool]:
    """``(text, truncated)`` keeping the FIRST ``cap`` bytes of ``data``."""
    if len(data) <= cap:
        return _decode(data), False
    marker = DROP_MARKER.format(dropped=len(data) - cap, total=len(data))
    return marker + _decode(data[:cap]), True


def _bounded_tail(data: bytes, cap: int, total: int | None = None) -> tuple[str, bool]:
    """``(text, truncated)`` keeping the LAST ``cap`` bytes of a source.

    ``data`` is expected to already hold the source's tail (see
    :func:`_read_tail_bytes`) and ``total`` its full size, so the marker can
    name the dropped head without reading a multi-megabyte log into memory.
    """
    total = len(data) if total is None else total
    dropped = max(0, total - min(len(data), cap))
    if not dropped:
        return _decode(data), False
    marker = DROP_MARKER.format(dropped=dropped, total=total)
    return marker + _decode(data[len(data) - cap :]), True


def _read_bytes(path: str, cap: int) -> bytes | None:
    """First ``cap + 1`` bytes of ``path`` (so callers can see the overflow)."""
    try:
        with open(path, "rb") as handle:
            return handle.read(cap + 1)
    except OSError:
        return None


def _read_tail_bytes(path: str, cap: int) -> tuple[bytes, int] | None:
    """``(tail, total_size)`` of ``path`` — the last ``cap`` bytes only."""
    try:
        total = os.path.getsize(path)
        with open(path, "rb") as handle:
            if total > cap:
                handle.seek(total - cap)
            return handle.read(), total
    except OSError:
        return None


def _run_git(workdir: str, args: list[str], cap: int) -> str:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if result.returncode != 0:
        return ""
    return _decode(result.stdout.encode("utf-8", errors="replace")[: cap + 1])


def commit_patch(workdir: str, commit: str = "") -> tuple[str, str]:
    """The landed commit's own patch, as ``(text, source)``.

    ``commit`` is the SHA the verdict stamped (HEAD at judge time); ``HEAD`` is
    the fallback for callers that collected evidence without one.
    """
    target = commit or "HEAD"
    text = _run_git(
        workdir,
        ["show", "--no-color", "--stat", "--patch", "--format=fuller", target],
        MAX_PATCH_BYTES,
    )
    if not text:
        return "", ""
    source = f"git show {target[:12]}" if commit else "git show HEAD"
    return text, source


def worktree_patch(workdir: str) -> tuple[str, str]:
    """The uncommitted diff the judge graded, as ``(text, source)``.

    Kept separate from the commit patch on purpose: a checkout whose tree is
    permanently dirty (generated files, graph caches) would otherwise store that
    noise under the name "the fix". Both artifacts are recorded, so a reader can
    tell the landed patch from whatever else was in the working tree.
    """
    text = _run_git(workdir, ["diff", "HEAD", "--no-color"], MAX_PATCH_BYTES)
    if not text.strip():
        return "", ""
    return text, "git diff HEAD (working tree)"


def _write_artifact(entry_dir: str, filename: str, text: str) -> int:
    path = os.path.join(entry_dir, filename)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    return len(text.encode("utf-8"))


def _item(
    name: str, filename: str, text: str, truncated: bool, source: str
) -> tuple[dict[str, Any], str]:
    """A manifest item plus the text to write for it."""
    return (
        {
            "name": name,
            "label": ITEM_LABELS[name],
            "file": filename,
            "bytes": len(text.encode("utf-8")),
            "truncated": truncated,
            "source": source,
        },
        text,
    )


def _collect_brief(workdir: str, env: dict[str, str]) -> tuple[dict[str, Any], str] | None:
    candidates: list[tuple[str, str]] = []
    env_path = env.get(BRIEF_ENV, "").strip()
    if env_path:
        candidates.append((env_path, BRIEF_ENV))
    candidates.append((os.path.join(workdir, ".gitreins", "worker-brief.md"), "worktree brief"))
    for path, source in candidates:
        data = _read_bytes(path, MAX_BRIEF_BYTES)
        if data is None:
            continue
        text, truncated = _bounded_head(data, MAX_BRIEF_BYTES)
        return _item(BRIEF_NAME, BRIEF_FILENAME, text, truncated, f"{source}: {path}")
    return None


def _collect_log(env: dict[str, str]) -> tuple[dict[str, Any], str] | None:
    path = env.get(LOG_ENV, "").strip()
    if not path:
        return None
    read = _read_tail_bytes(path, MAX_LOG_BYTES)
    if read is None:
        return None
    data, total = read
    text, truncated = _bounded_tail(data, MAX_LOG_BYTES, total=total)
    return _item(LOG_NAME, LOG_FILENAME, text, truncated, f"{LOG_ENV}: {path}")


def _collect_patch(workdir: str, commit: str) -> tuple[dict[str, Any], str] | None:
    text, source = commit_patch(workdir, commit)
    if not text.strip():
        return None
    text, truncated = _bounded_head(text.encode("utf-8"), MAX_PATCH_BYTES)
    return _item(PATCH_NAME, PATCH_FILENAME, text, truncated, source)


def _collect_worktree(workdir: str) -> tuple[dict[str, Any], str] | None:
    text, source = worktree_patch(workdir)
    if not text.strip():
        return None
    text, truncated = _bounded_head(text.encode("utf-8"), MAX_PATCH_BYTES)
    return _item(WORKTREE_NAME, WORKTREE_FILENAME, text, truncated, source)


def collect_evidence(
    workdir: str,
    entry_dir: str,
    commit: str = "",
    task_id: str = "",
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Copy the run's artifacts into ``entry_dir`` and return their manifest.

    Best-effort by contract: a source that is missing, unreadable or empty is
    left out of the manifest (the reader then sees "not recorded" instead of a
    fabricated artifact), and an unwritable ``entry_dir`` yields an empty
    manifest rather than an exception. ``task_id`` is recorded for provenance
    only — the artifacts carry whatever the source holds.
    """
    environ = dict(os.environ if env is None else env)
    items: list[dict[str, Any]] = []
    try:
        os.makedirs(entry_dir, exist_ok=True)
    except OSError:
        return {"collected_at": _now(), "items": [], "detail": "evidence directory is not writable"}

    for collector in (
        lambda: _collect_brief(workdir, environ),
        lambda: _collect_log(environ),
        lambda: _collect_patch(workdir, commit),
        lambda: _collect_worktree(workdir),
    ):
        try:
            collected = collector()
        except Exception:  # evidence must never break a verdict
            collected = None
        if not collected:
            continue
        item, text = collected
        try:
            _write_artifact(entry_dir, item["file"], text)
        except OSError:
            continue
        items.append(item)

    manifest: dict[str, Any] = {"collected_at": _now(), "items": items}
    if task_id:
        manifest["task_id"] = task_id
    return manifest


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def manifest_items(verdict: dict[str, Any]) -> list[dict[str, Any]]:
    """Manifest items of a verdict record, validated for serving.

    Legacy verdicts (recorded before evidence embedding) have no ``evidence``
    block and simply yield ``[]``.
    """
    evidence = verdict.get("evidence")
    if not isinstance(evidence, dict):
        return []
    raw_items = evidence.get("items")
    if not isinstance(raw_items, list):
        return []
    items: list[dict[str, Any]] = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        name = raw.get("name")
        filename = raw.get("file")
        if not isinstance(name, str) or not isinstance(filename, str):
            continue
        if not _safe_artifact_name(filename):
            continue
        items.append(raw)
    return items


def _safe_artifact_name(filename: str) -> bool:
    """A manifest filename must be a plain file name — never a path."""
    return bool(filename) and filename not in (".", "..") and os.path.basename(filename) == filename


def read_evidence(entry_dir: str, verdict: dict[str, Any], name: str) -> tuple[str, str] | None:
    """``(filename, text)`` for a manifest-declared artifact, else ``None``.

    The artifact must be declared in the verdict's own manifest AND be a plain
    file name, so a crafted ``name`` cannot walk out of the verdict directory;
    an unreadable artifact reads as ``None`` (a 404) rather than a 500.
    """
    for item in manifest_items(verdict):
        if item.get("name") != name:
            continue
        filename = item["file"]
        if not _safe_artifact_name(filename):
            return None
        path = os.path.join(entry_dir, filename)
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                return filename, handle.read()
        except OSError:
            return None
    return None


# ── Evidence v1 emitters (EVID-002) ─────────────────────────────────────
#
# The document shape is the contract in schemas/evidence-v1.schema.json:
# additive metadata fields are allowed (the schema leaves ``metadata`` open),
# every other field is closed and typed. The emitters below are the only
# producers of that shape, so the schema's consts are declared once here.

#: ``$schema``/``schemaVersion`` identity — the schema pins both as consts, so
#: an incompatible shape requires a NEW URL and major version.
EVIDENCE_SCHEMA = "https://gitreins.dev/schemas/evidence/v1.json"
EVIDENCE_SCHEMA_VERSION = "1.0"

#: Hard ceiling for one serialized document (32 KiB), then the per-string and
#: per-collection caps applied BEFORE it.
MAX_EVIDENCE_BYTES = 32 * 1024
MAX_TEXT_CHARS = 2048
MAX_ID_CHARS = 128
MAX_TITLE_CHARS = 512
MAX_CHECKS = 32
MAX_REPORT_ENTRIES = 50

#: Scope vocabulary shared with the guard's ``--scope`` flag.
GUARD_SCOPES = ("staged", "working-tree")
HISTORY_SCOPE = "history"

_TRUNCATION_MARKER = "[truncated]"

#: Secret-shaped spans replaced by ``[REDACTED]``. Deliberately shaped, not
#: exhaustive: a provider prefix, a credential assignment, or an obvious
#: high-entropy blob. Pattern 2 keeps its group(1) (the ``token=`` label) so a
#: reader still sees WHICH credential was dropped.
_SECRET_PATTERNS = (
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+"),
    re.compile(
        r"(?i)((?:token|secret|password|api[_-]?key|authorization|credential)\s*[:=]\s*)"
        r"[^\s,;'\"}]+"
    ),
    re.compile(r"\b(?:gh[pousr]_|github_pat_|xox[baprs]-|sk-)[A-Za-z0-9_.-]{12,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
)


def redact_text(value: object, limit: int = MAX_TEXT_CHARS) -> tuple[str, bool, bool]:
    """``(text, redacted, truncated)`` — secret spans replaced, then capped.

    The cap is applied AFTER redaction so a secret that straddles the cap
    cannot survive by being cut in half, and the marker keeps the result at
    exactly ``limit`` characters (the schema's ``maxLength``).
    """
    text = "" if value is None else str(value)
    redacted = False
    for pattern in _SECRET_PATTERNS:
        text, count = pattern.subn(
            lambda match: (match.group(1) if match.lastindex else "") + "[REDACTED]",
            text,
        )
        redacted = redacted or bool(count)
    truncated = len(text) > limit
    if truncated:
        text = text[: max(0, limit - len(_TRUNCATION_MARKER))] + _TRUNCATION_MARKER
    return text, redacted, truncated


def redact_document(document: dict[str, Any]) -> tuple[bool, bool]:
    """Redact/cap every string VALUE of a document in place.

    The final boundary before serialization: the builders already redact what
    they compose, and this sweep means a string added by a future caller
    cannot reach stdout unredacted. Keys are never touched — the document's
    shape is the contract, only its text crosses the boundary.

    Returns ``(redacted, truncated)``.
    """
    redacted = False
    truncated = False
    stack: list[Any] = [document]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            for key, value in node.items():
                if isinstance(value, str):
                    text, did_redact, did_truncate = redact_text(value)
                    if did_redact:
                        node[key] = text
                        redacted = True
                    truncated = truncated or did_truncate
                elif isinstance(value, (dict, list)):
                    stack.append(value)
        elif isinstance(node, list):
            for index, value in enumerate(node):
                if isinstance(value, str):
                    text, did_redact, did_truncate = redact_text(value)
                    if did_redact:
                        node[index] = text
                        redacted = True
                    truncated = truncated or did_truncate
                elif isinstance(value, (dict, list)):
                    stack.append(value)
    return redacted, truncated


def _safe_text(value: object, flags: dict[str, bool], limit: int = MAX_TEXT_CHARS) -> str:
    """Redacted/capped text, recording both facts in the document's flags."""
    text, redacted, truncated = redact_text(value, limit)
    flags["redacted"] = flags["redacted"] or redacted
    flags["truncated"] = flags["truncated"] or truncated
    return text


def evidence_outcome(passed: bool | None) -> str:
    """The ``outcome`` vocabulary for a tri-state pass flag."""
    if passed is None:
        return "unknown"
    return "pass" if passed else "fail"


def _step_id(name: object) -> str:
    """Base guard id for a result name ('tests (diff: 3 files)' → 'tests')."""
    return str(name or "check").split(" ", 1)[0].strip() or "check"


def _base_document(
    command: str, scope: str, passed: bool | None, flags: dict[str, bool]
) -> dict[str, Any]:
    """A v1 document with every required field present and no checks yet."""
    return {
        "$schema": EVIDENCE_SCHEMA,
        "schemaVersion": EVIDENCE_SCHEMA_VERSION,
        "producer": {"name": "gitreins", "version": str(_gitreins_version)[:64]},
        "command": command,
        "generatedAt": _now(),
        "scope": scope,
        "outcome": evidence_outcome(passed),
        "passed": passed,
        "summary": "",
        "checks": [],
        "metadata": {
            "redacted": True,
            "redactionsApplied": False,
            "truncated": False,
        },
    }


def _guard_check(result: object, flags: dict[str, bool]) -> dict[str, Any]:
    """One tier-1 step as a v1 check.

    A SKIPPED step did no work, so it is reported as the honest no-grade
    shape — ``outcome: unknown`` with ``passed: null`` — never as a green
    ``pass`` (TRUST-001: a gate that never ran is not a passing gate). The
    skip reason rides in the summary.
    """
    skipped = bool(getattr(result, "skipped", False))
    passed = bool(getattr(result, "passed", False))
    output = getattr(result, "output", "") or ""
    error = getattr(result, "error", "") or ""
    if skipped:
        reason = getattr(result, "skip_reason", "") or "reason not recorded"
        outcome, check_passed, raw = "unknown", None, f"skipped — {reason}"
    elif error and not output:
        outcome, check_passed, raw = "error", False, error
    else:
        outcome, check_passed = evidence_outcome(passed), passed
        raw = output or error or ("check passed" if passed else "check failed")
    return {
        "id": _safe_text(_step_id(getattr(result, "name", "check")), flags, MAX_ID_CHARS),
        "outcome": outcome,
        "passed": check_passed,
        "summary": _safe_text(raw, flags),
    }


def _item_field(item: object, name: str) -> Any:
    """Read a criterion field off either a dict or a verdict item object."""
    if isinstance(item, dict):
        return item.get(name)
    return getattr(item, name, None)


def _verdict_items(result: object) -> list[Any]:
    """The tier-2 criterion items of a JudgeResult, in either result shape.

    The legacy path keeps them on ``result.verdict.items``; the pipeline path
    buries the verdict in a stage step's ``data``. Both are tried so the
    emitted checks describe what was actually graded.
    """
    verdict = getattr(result, "verdict", None)
    items = getattr(verdict, "items", None) if verdict is not None else None
    if isinstance(items, (list, tuple)) and items:
        return list(items)
    pipeline_result = getattr(result, "pipeline_result", None)
    if not isinstance(pipeline_result, dict):
        return []
    for stage in (pipeline_result.get("stages") or {}).values():
        if not isinstance(stage, dict):
            continue
        for step in stage.get("steps") or []:
            data = step.get("data") if isinstance(step, dict) else None
            if isinstance(data, dict) and data.get("verdict"):
                return list(data.get("items") or [])
    return []


def _judge_gate_checks(result: object, flags: dict[str, bool], limit: int) -> list[dict]:
    """Tier-1 evidence for a judge document: pipeline stages, else guard steps."""
    pipeline_result = getattr(result, "pipeline_result", None)
    stages = (pipeline_result.get("stages") or {}) if isinstance(pipeline_result, dict) else {}
    checks: list[dict] = []
    for stage_id, stage in list(stages.items())[:limit]:
        stage = stage if isinstance(stage, dict) else {}
        stage_passed = stage.get("passed") is True
        summary = stage.get("summary") or ""
        if stage.get("degraded"):
            skipped = ", ".join(stage.get("skipped_steps") or []) or "steps"
            summary = f"{summary}\nDEGRADED — did no work: {skipped}".strip()
        checks.append(
            {
                "id": _safe_text(f"stage-{stage_id}", flags, MAX_ID_CHARS),
                "outcome": evidence_outcome(stage_passed),
                "passed": stage_passed,
                "summary": _safe_text(summary, flags),
            }
        )
    if checks:
        return checks
    tier1 = getattr(result, "tier1", None)
    for item in list(getattr(tier1, "results", None) or [])[:limit]:
        checks.append(_guard_check(item, flags))
    return checks


def guard_evidence(
    result: object,
    scope: str = "staged",
    *,
    changed_file_count: int | None = None,
) -> dict[str, Any]:
    """Build the ``guard`` evidence document from a Tier1Result.

    ``passed`` mirrors ``result.passed`` — a DEGRADED pass is ``passed: true``
    with the skipped steps reported per check (``outcome: unknown``), and
    ``metadata.degraded`` / ``metadata.skippedSteps`` name the degradation, so
    a green document can never hide a gate that never ran.
    """
    flags = {"redacted": False, "truncated": False}
    passed = bool(getattr(result, "passed", False))
    document = _base_document("guard", scope, passed, flags)

    results = list(getattr(result, "results", None) or [])
    checks = [_guard_check(item, flags) for item in results[:MAX_CHECKS]]
    if len(results) > MAX_CHECKS:
        flags["truncated"] = True

    summary = getattr(result, "summary", "") or (
        "All guards passed" if passed else "One or more guards failed"
    )
    if getattr(result, "degraded", False):
        summary = f"{summary}\nDEGRADED PASS (skips: {getattr(result, 'skip_summary', '')})"
    document["summary"] = _safe_text(summary, flags)
    document["checks"] = checks

    extra = getattr(result, "extra", None) or {}
    if changed_file_count is None:
        changed_file_count = extra.get("changed_count")
        if changed_file_count is None:
            changed_file_count = extra.get("staged_count", 0)
    try:
        # Emitting evidence must never raise: a caller that put a non-numeric
        # count in `extra` degrades to 0 rather than breaking the document.
        changed_file_count = max(0, int(changed_file_count or 0))
    except (TypeError, ValueError):
        changed_file_count = 0
    document["metadata"].update(
        {
            "redactionsApplied": flags["redacted"],
            "truncated": flags["truncated"],
            "checkCount": len(results),
            "changedFileCount": changed_file_count,
            "degraded": bool(getattr(result, "degraded", False)),
            "skippedSteps": [
                str(step.get("step", "")) for step in getattr(result, "skipped_steps", None) or []
            ],
        }
    )
    return document


def judge_evidence(
    result: object,
    task: object,
    scope: str = "staged",
    ephemeral: bool = False,
) -> dict[str, Any]:
    """Build the ``judge`` evidence document from a JudgeResult and its task.

    The checks carry the TIER 2 criteria evaluation first (one check per
    criterion: ``criterion-1``…), then the tier-1 evidence that gated it —
    pipeline stages, or the guard steps on the legacy path.
    """
    flags = {"redacted": False, "truncated": False}
    passed = bool(getattr(result, "passed", False))
    document = _base_document("judge", scope, passed, flags)
    document["subject"] = {
        "taskId": _safe_text(getattr(task, "id", "") or "unknown", flags, MAX_ID_CHARS),
        "title": _safe_text(getattr(task, "title", "") or "", flags, MAX_TITLE_CHARS),
        "ephemeral": bool(ephemeral),
    }

    pipeline_result = getattr(result, "pipeline_result", None)
    if isinstance(pipeline_result, dict) and pipeline_result.get("error"):
        document["outcome"] = "error"

    criteria = _verdict_items(result)
    checks: list[dict[str, Any]] = []
    for index, item in enumerate(criteria[:MAX_CHECKS]):
        item_passed = _item_field(item, "status") == "PASS"
        detail = _item_field(item, "detail") or ""
        criterion = _item_field(item, "criterion") or f"criterion {index + 1}"
        checks.append(
            {
                "id": f"criterion-{index + 1}",
                "outcome": evidence_outcome(item_passed),
                "passed": item_passed,
                "summary": _safe_text(f"{criterion}: {detail}", flags),
            }
        )
    if len(criteria) > MAX_CHECKS:
        flags["truncated"] = True

    remaining = max(0, MAX_CHECKS - len(checks))
    if remaining:
        checks.extend(_judge_gate_checks(result, flags, remaining))

    document["summary"] = _safe_text(getattr(result, "summary", ""), flags)
    document["checks"] = checks
    document["metadata"].update(
        {
            "redactionsApplied": flags["redacted"],
            "truncated": flags["truncated"],
            "checkCount": len(checks),
            "criterionCount": len(criteria),
            "historyPersisted": not ephemeral,
            "ephemeral": bool(ephemeral),
        }
    )
    return document


def report_evidence(entries: Any, storage_mode: str = "") -> dict[str, Any]:
    """Build the ``report`` evidence document from verdict-history entries.

    History has no single verdict, so ``passed`` stays ``null`` (``outcome:
    unknown``) and the rollup lives in ``summary``/``metadata.checkCount``.
    Each retained entry becomes one check, capped at :data:`MAX_REPORT_ENTRIES`.
    """
    raw_entries = [entry for entry in (entries or []) if isinstance(entry, dict)]
    flags = {"redacted": False, "truncated": len(raw_entries) > MAX_REPORT_ENTRIES}
    selected = raw_entries[:MAX_REPORT_ENTRIES]
    passed_count = sum(1 for entry in selected if entry.get("passed") is True)
    failed_count = sum(1 for entry in selected if entry.get("passed") is False)

    document = _base_document("report", HISTORY_SCOPE, None, flags)
    document["summary"] = _safe_text(
        f"{len(selected)} recent verdicts: {passed_count} pass, {failed_count} fail", flags
    )
    checks = []
    for entry in selected:
        entry_passed = entry.get("passed") if isinstance(entry.get("passed"), bool) else None
        title = entry.get("task_title") or entry.get("summary") or "verdict"
        checks.append(
            {
                "id": _safe_text(entry.get("task_id") or "unknown", flags, MAX_ID_CHARS),
                "outcome": evidence_outcome(entry_passed),
                "passed": entry_passed,
                "summary": _safe_text(title, flags),
            }
        )
    document["checks"] = checks
    document["metadata"].update(
        {
            "redactionsApplied": flags["redacted"],
            "truncated": flags["truncated"],
            "checkCount": len(raw_entries),
            "storage": _safe_text(storage_mode, flags, 32),
        }
    )
    return document


def _encode(document: dict[str, Any]) -> str:
    """Serialize an evidence document deterministically (sorted, no padding)."""
    return json.dumps(document, separators=(",", ":"), ensure_ascii=False, sort_keys=True)


def _byte_len(payload: str) -> int:
    return len(payload.encode("utf-8"))


def dumps_evidence(document: dict[str, Any], max_bytes: int = MAX_EVIDENCE_BYTES) -> str:
    """Serialize an evidence document under a hard byte ceiling.

    Applied in order — component text is capped by the builders, then the
    whole document:

    1. re-sweep every string (redaction boundary) and take the caps the
       builders recorded,
    2. drop checks from the tail while the document is over ``max_bytes``,
    3. shrink the summary/subject title,
    4. as a last resort emit a minimal, still schema-valid document naming the
       overflow.

    The caller's document is never mutated, ``metadata.truncated`` is set by
    ANY of these steps, and ``metadata.checkCount`` keeps reporting how many
    checks the run produced (pre-cap), so a clipped document says so.
    """
    bounded = copy.deepcopy(document)
    redacted, truncated = redact_document(bounded)
    metadata = bounded.setdefault("metadata", {})
    metadata["redacted"] = True
    metadata["redactionsApplied"] = bool(metadata.get("redactionsApplied")) or redacted
    metadata["truncated"] = bool(metadata.get("truncated")) or truncated

    checks = bounded.get("checks")
    if not isinstance(checks, list):
        checks = []
        bounded["checks"] = checks

    payload = _encode(bounded)
    while _byte_len(payload) > max_bytes and checks:
        checks.pop()
        metadata["truncated"] = True
        payload = _encode(bounded)

    if _byte_len(payload) > max_bytes:
        flags = {"redacted": False, "truncated": False}
        bounded["summary"] = _safe_text(bounded.get("summary", ""), flags, 256)
        subject = bounded.get("subject")
        if isinstance(subject, dict) and "title" in subject:
            subject["title"] = _safe_text(subject["title"], flags, 128)
        if flags["redacted"]:
            metadata["redactionsApplied"] = True
        metadata["truncated"] = True
        payload = _encode(bounded)

    if _byte_len(payload) > max_bytes:
        # Defensive fallback: a future v1 additive field could make even the
        # fixed fields oversized. Emit the smallest document that still
        # validates rather than an over-cap one.
        command = document.get("command")
        if command not in ("guard", "judge", "report"):
            command = "guard"
        scope = document.get("scope")
        if scope not in (*GUARD_SCOPES, HISTORY_SCOPE):
            scope = "staged"
        outcome = document.get("outcome")
        if outcome not in ("pass", "fail", "error", "unknown"):
            outcome = "unknown"
        passed = document.get("passed")
        if not isinstance(passed, bool):
            passed = None
        bounded = {
            "$schema": EVIDENCE_SCHEMA,
            "schemaVersion": EVIDENCE_SCHEMA_VERSION,
            "producer": {"name": "gitreins", "version": str(_gitreins_version)[:64]},
            "command": command,
            "generatedAt": document.get("generatedAt", _now()),
            "scope": scope,
            "outcome": outcome,
            "passed": passed,
            "summary": "Evidence exceeded the output limit",
            "checks": [],
            "metadata": {
                "redacted": True,
                "redactionsApplied": True,
                "truncated": True,
            },
        }
        payload = _encode(bounded)

    return payload
