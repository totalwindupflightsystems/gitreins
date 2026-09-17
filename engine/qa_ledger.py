"""QA run ledger — durable QA verdicts in the harness record.

The harness's QA surfaces (``gitreins worktree fresh``, ``repro``, ``dogfood``)
used to leave their outcome in a gitignored, ceiling-pruned registry and on
stdout only.  The judgment record therefore covered ``task complete`` verdicts
(dev / foreman work) and *no QA verdicts at all*, so nothing — not a report,
not a fleet QA discovery that sorts candidates by "never QA'd" — could read how
a checkout's last QA battery actually went.

This module appends one JSON line per QA run to a ledger file and reads it
back:

    {"ts": "...", "project": "repo-name", "status": "pass", "verdict": "PASS",
     "kind": "dogfood", "run_id": "...", "exit_code": 0, "commit": "<sha>",
     "cells": {"init": "passed", ...}, "findings": [], "evidence": "",
     "harness_version": "0.13.0", "note": "", "detail": {...}}

A row is a **superset** of the fleet QA-ledger vocabulary (``ts``,
``project``, ``status``, ``cells``, ``findings``, ``evidence``, ``note``): a
discovery that already reads that schema can consume a harness-written ledger
directly, and pointing :data:`QA_LEDGER_ENV` at a fleet ledger appends there.
The harness-specific keys (``kind``, ``verdict``, ``run_id``, ``exit_code``,
``commit``, ``harness_version``, ``detail``) are additive, so no fleet reader
loses a field it knew.

Path resolution (first match wins):

1. ``GITREINS_QA_LEDGER`` — a file path, or a directory (existing, or ending
   with a separator) that receives ``qa-ledger.jsonl``.
2. ``qa_ledger.path`` in ``.gitreins/config.yaml`` (relative paths resolve
   against the repo root).
3. ``<repo>/.gitreins/qa-ledger.jsonl``.

Recording is skipped when ``qa_ledger.enabled`` is false, and the newest
``qa_ledger.max_entries`` rows are kept (default 1000).

A ledger failure must never fail the run it records: callers treat
``OSError`` / ``ValueError`` as "not recorded" and say so on stderr instead of
silently pretending the row landed.
"""

from __future__ import annotations

import json
import os
import subprocess
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from engine.config import load_raw_config
from engine.version import __version__

try:  # POSIX only; Windows falls back to plain appends.
    import fcntl
except ImportError:  # pragma: no cover - platform dependent
    fcntl = None  # type: ignore[assignment]

QA_LEDGER_ENV = "GITREINS_QA_LEDGER"
DEFAULT_QA_LEDGER_FILE = "qa-ledger.jsonl"
DEFAULT_QA_LEDGER_MAX_ENTRIES = 1000
CELL_LIMIT = 24
PASSING_CELL_STATUSES = ("pass", "passed", "ok")
FAILING_CELL_STATUSES = ("fail", "failed", "error")


# ── Configuration and path resolution ──────────────────────────


def _coerce_max_entries(value: Any) -> int:
    """Positive int, or the default for anything unusable."""
    if isinstance(value, bool) or value is None:
        return DEFAULT_QA_LEDGER_MAX_ENTRIES
    try:
        entries = int(value)
    except (TypeError, ValueError):
        return DEFAULT_QA_LEDGER_MAX_ENTRIES
    return entries if entries >= 1 else DEFAULT_QA_LEDGER_MAX_ENTRIES


def qa_ledger_path(workdir: str) -> str:
    """Resolve the ledger file: env override, config, then the repo default."""
    override = os.environ.get(QA_LEDGER_ENV, "").strip()
    if override:
        override = os.path.expanduser(override)
        if override.endswith(os.sep) or os.path.isdir(override):
            return os.path.join(override, DEFAULT_QA_LEDGER_FILE)
        return override

    section: Any = {}
    try:
        section = (load_raw_config(workdir) or {}).get("qa_ledger") or {}
    except Exception:  # a broken config must not blind the reader
        section = {}
    if not isinstance(section, dict):
        section = {}

    configured = str(section.get("path") or "").strip()
    if configured:
        configured = os.path.expanduser(configured)
        if os.path.isabs(configured):
            return configured
        return os.path.join(workdir, configured)

    return os.path.join(workdir, ".gitreins", DEFAULT_QA_LEDGER_FILE)


def qa_ledger_settings(workdir: str) -> dict[str, Any]:
    """Resolved ledger settings: ``enabled``, ``path``, ``max_entries``."""
    section: Any = {}
    try:
        section = (load_raw_config(workdir) or {}).get("qa_ledger") or {}
    except Exception:
        section = {}
    if not isinstance(section, dict):
        section = {}
    return {
        "enabled": bool(section.get("enabled", True)),
        "path": qa_ledger_path(workdir),
        "max_entries": _coerce_max_entries(section.get("max_entries")),
    }


def project_name(workdir: str) -> str:
    """Ledger project id — the repo directory name, like the fleet ledger."""
    return os.path.basename(os.path.abspath(workdir).rstrip(os.sep)) or "unknown"


# ── Row construction ───────────────────────────────────────────


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _head_sha(workdir: str) -> str:
    """Best-effort HEAD sha; an empty string when the checkout has none."""
    try:
        result = subprocess.run(
            ["git", "-C", workdir, "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def _exit_code(value: Any) -> int:
    if isinstance(value, bool) or value is None:
        return 1
    try:
        return int(value)
    except (TypeError, ValueError):
        return 1


def _run_exit_code(kind: str, report: dict[str, Any]) -> int:
    """A repro report carries passes/failures, not an exit code of its own."""
    if kind == "repro":
        failures = report.get("failures")
        if isinstance(failures, int) and not isinstance(failures, bool):
            return 0 if failures == 0 else 1
        passes, total = report.get("passes"), report.get("k")
        if isinstance(passes, int) and isinstance(total, int) and total > 0:
            return 0 if passes == total else 1
    return _exit_code(report.get("exit_code"))


def _cell_status(exit_code: int) -> str:
    return "passed" if exit_code == 0 else "failed"


def _bounded_cells(cells: dict[str, str]) -> dict[str, str]:
    """Name the skipped remainder instead of truncating silently."""
    if len(cells) <= CELL_LIMIT:
        return cells
    kept = list(cells.items())[: CELL_LIMIT - 1]
    return dict(kept) | {"…": f"{len(cells) - CELL_LIMIT + 1} more"}


def _cells_and_detail(
    kind: str, report: dict[str, Any], command: str | None
) -> tuple[dict[str, str], dict[str, Any]]:
    """Map a disposable-run report onto ledger cells + detail for its kind."""
    if kind == "repro":
        runs = [run for run in report.get("runs", []) if isinstance(run, dict)]
        cells: dict[str, str] = {}
        for run in runs:
            cells[f"run-{run.get('index')}"] = _cell_status(_exit_code(run.get("exit_code")))
        detail = {
            "command": report.get("command") or command or "",
            "k": report.get("k"),
            "concurrency": report.get("concurrency"),
            "passes": report.get("passes"),
            "failures": report.get("failures"),
            "pass_rate": report.get("pass_rate"),
            "head": report.get("head", ""),
            "runs": [
                {
                    "index": run.get("index"),
                    "exit_code": run.get("exit_code"),
                    "duration_s": run.get("duration_s"),
                }
                for run in runs[:CELL_LIMIT]
            ],
        }
        return _bounded_cells(cells), detail

    if kind == "dogfood":
        steps = [step for step in report.get("steps", []) if isinstance(step, dict)]
        cells = {
            str(step.get("name") or f"step-{i}"): str(step.get("status") or "unknown")
            for i, step in enumerate(steps, 1)
        }
        judge_raw = report.get("judge")
        judge: dict[str, Any] = judge_raw if isinstance(judge_raw, dict) else {}
        cells["judge"] = str(judge.get("status") or "unknown")
        detail = {
            "command": report.get("command") or "gitreins dogfood",
            "tree": report.get("tree", ""),
            "steps": [
                {
                    "name": step.get("name"),
                    "status": step.get("status"),
                    "exit_code": step.get("exit_code"),
                    "duration_s": step.get("duration_s"),
                }
                for step in steps[:CELL_LIMIT]
            ],
            "judge": judge,
        }
        return _bounded_cells(cells), detail

    # "fresh" and any other single-command run.
    cells = {kind or "run": _cell_status(_exit_code(report.get("exit_code")))}
    detail = {
        "command": command or report.get("command") or "",
        "duration_s": report.get("duration_s"),
        "tree": report.get("tree", ""),
        "kept": bool(report.get("kept", False)),
        "output": report.get("output", ""),
    }
    return cells, detail


def build_row(
    workdir: str,
    *,
    kind: str,
    cells: dict[str, str],
    verdict: str,
    exit_code: int | None = None,
    run_id: str = "",
    commit: str | None = None,
    findings: list[Any] | None = None,
    evidence: str = "",
    note: str = "",
    detail: dict[str, Any] | None = None,
    project: str | None = None,
    status: str | None = None,
    ts: str | None = None,
    agent: str = "",
    server: str = "",
) -> dict[str, Any]:
    """One ledger row, with the fleet keys first (stable key order)."""
    verdict = (verdict or "").upper() or "UNKNOWN"
    if not status:
        status = {"PASS": "pass", "FAIL": "fail"}.get(verdict, verdict.lower())
    row: dict[str, Any] = {
        "ts": ts or _utc_now_iso(),
        "project": project or project_name(workdir),
        "status": status,
        "verdict": verdict,
        "kind": kind,
        "cells": cells,
        "findings": list(findings or []),
        "evidence": evidence,
        "note": note,
    }
    if exit_code is not None:
        row["exit_code"] = exit_code
    if run_id:
        row["run_id"] = run_id
    row["commit"] = commit if commit is not None else _head_sha(workdir)
    if agent:
        row["agent"] = agent
    if server:
        row["server"] = server
    row["harness_version"] = __version__
    if detail:
        row["detail"] = detail
    return row


def record_run(
    workdir: str,
    kind: str,
    report: dict[str, Any],
    *,
    command: str | None = None,
    note: str = "",
) -> dict[str, Any] | None:
    """Append one QA-run row for a disposable-run report. ``None`` when disabled."""
    kind = str(kind or "run")
    exit_code = _run_exit_code(kind, report)
    cells, detail = _cells_and_detail(kind, report, command)
    row = build_row(
        workdir,
        kind=kind,
        cells=cells,
        verdict="PASS" if exit_code == 0 else "FAIL",
        exit_code=exit_code,
        run_id=str(report.get("run_id") or ""),
        findings=[],
        evidence="",
        note=note,
        detail=detail,
    )
    return row if append_row(workdir, row) else None


def record_external(
    workdir: str,
    *,
    project: str | None = None,
    status: str | None = None,
    kind: str = "lane",
    cells: dict[str, str] | None = None,
    findings: list[Any] | None = None,
    evidence: str = "",
    note: str = "",
    agent: str = "",
    server: str = "",
    commit: str | None = None,
    verdict: str | None = None,
    exit_code: int | None = None,
    ts: str | None = None,
    detail: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Record a QA run produced outside the harness (a fleet lane, a bunker battery)."""
    if not verdict:
        if isinstance(exit_code, int) and not isinstance(exit_code, bool):
            verdict = "PASS" if exit_code == 0 else "FAIL"
        elif status:
            verdict = str(status).upper()
        else:
            verdict = "UNKNOWN"
    cells = {str(k): str(v) for k, v in (cells or {}).items()}
    row = build_row(
        workdir,
        kind=str(kind or "lane"),
        cells=cells,
        verdict=verdict,
        exit_code=exit_code,
        findings=findings,
        evidence=str(evidence or ""),
        note=note,
        detail=detail,
        project=project,
        status=status,
        ts=ts,
        agent=agent,
        server=server,
        commit=commit,
    )
    return row if append_row(workdir, row) else None


# ── Reading and writing ────────────────────────────────────────


@contextmanager
def _ledger_lock(path: str) -> Iterator[None]:
    """Serialize appends/prunes across concurrent repro-farm processes."""
    if fcntl is None:  # pragma: no cover - platform dependent
        yield
        return
    handle = open(path, "a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def _prune(path: str, max_entries: int) -> None:
    """Keep the newest ``max_entries`` lines; rewrite atomically."""
    with open(path, encoding="utf-8") as stream:
        lines = [line for line in stream.read().splitlines() if line.strip()]
    if len(lines) <= max_entries:
        return
    kept = lines[-max_entries:]
    tmp = f"{path}.tmp{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as stream:
        stream.write("".join(f"{line}\n" for line in kept))
    os.replace(tmp, path)


def append_row(workdir: str, row: dict[str, Any]) -> str | None:
    """Append ``row`` under the ledger lock; ``None`` when recording is disabled.

    Raises ``OSError`` / ``TypeError`` when the row cannot be stored — callers
    surface that as "not recorded" rather than swallowing it.
    """
    settings = qa_ledger_settings(workdir)
    if not settings["enabled"]:
        return None
    path = settings["path"]
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    line = json.dumps(row, ensure_ascii=True, separators=(",", ":"))
    with _ledger_lock(path):
        with open(path, "a", encoding="utf-8") as stream:
            stream.write(f"{line}\n")
        _prune(path, settings["max_entries"])
    return path


def list_rows(
    workdir: str, n: int | None = None, *, path: str | None = None
) -> list[dict[str, Any]]:
    """Ledger rows oldest-first; malformed lines are skipped, not fatal."""
    ledger = path or qa_ledger_path(workdir)
    rows: list[dict[str, Any]] = []
    try:
        # QA-GITREINS-POC-6: decode with errors="replace" so an undecodable byte
        # in ONE line costs that line only. A strict decode raised
        # UnicodeDecodeError out of the loop body and threw away every row the
        # reader had already salvaged (``gitreins qa list`` / ``report`` died
        # with a traceback on a binary-corrupted ledger).
        with open(ledger, encoding="utf-8", errors="replace") as stream:
            for line in stream:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict):
                    rows.append(row)
    except OSError:
        return []
    if n is not None:
        rows = rows[-n:] if n > 0 else []
    return rows


# ── Presentation ───────────────────────────────────────────────


def _cell_summary(cells: Any) -> str:
    """``3/3 passed, 1 skipped`` — graded cells first, then ungraded statuses."""
    if not isinstance(cells, dict) or not cells:
        return "no cells"
    passed = failed = 0
    others: dict[str, int] = {}
    for value in cells.values():
        status = str(value).lower()
        if status in PASSING_CELL_STATUSES:
            passed += 1
        elif status in FAILING_CELL_STATUSES:
            failed += 1
        else:
            others[status] = others.get(status, 0) + 1
    graded = passed + failed
    parts = [f"cells {passed}/{graded} passed"] if graded else ["cells no graded outcome"]
    if others:
        parts.append(", ".join(f"{count} {status}" for status, count in sorted(others.items())))
    return ", ".join(parts)


def _row_line(row: dict[str, Any]) -> str:
    verdict = str(row.get("verdict") or "?").upper()
    icon = "✓" if verdict == "PASS" else ("✗" if verdict == "FAIL" else "·")
    commit = str(row.get("commit") or "")[:7]
    commit_str = f"  commit {commit}" if commit else ""
    exit_code = row.get("exit_code")
    exit_str = f"  exit {exit_code}" if isinstance(exit_code, int) else ""
    return (
        f"  {icon} {str(row.get('kind') or '?'):<8} {str(row.get('project') or '?'):<20} "
        f"{str(row.get('ts') or '?')}  {_cell_summary(row.get('cells'))}{exit_str}{commit_str}"
    )


def format_rows(workdir: str, n: int = 20, *, path: str | None = None) -> str:
    """Text block for ``gitreins qa list``."""
    ledger = path or qa_ledger_path(workdir)
    rows = list_rows(workdir, path=ledger)
    lines = ["═══ GitReins QA Ledger ═══", ""]
    if not rows:
        lines.append("No QA runs recorded.")
    else:
        shown = rows[-n:] if n > 0 else []
        skipped = len(rows) - len(shown)
        if skipped > 0:
            lines.append(f"Showing newest {len(shown)} of {len(rows)} QA run(s):")
        else:
            lines.append(f"{len(rows)} QA run(s):")
        lines.extend(_row_line(row) for row in shown)
    lines.append("")
    lines.append(f"Ledger: {ledger}")
    return "\n".join(lines)


def format_report_section(workdir: str, n: int = 5, *, path: str | None = None) -> str:
    """Short QA block for ``gitreins report``; empty when the ledger has no rows."""
    ledger = path or qa_ledger_path(workdir)
    rows = list_rows(workdir, path=ledger)
    if not rows:
        return ""
    shown = rows[-n:] if n > 0 else []
    lines = [f"QA runs (newest {len(shown)} of {len(rows)}):"]
    lines.extend(_row_line(row) for row in shown)
    lines.append(f"QA ledger: {ledger}")
    return "\n".join(lines)
