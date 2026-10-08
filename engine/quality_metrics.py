"""Consume repo-produced quality metrics without computing them.

One authority per run (GR-143): the FIRST surface (guard, judge, doctor, CLI,
MCP) to ask for a snapshot runs the repository's producer once and caches the
parsed artifact per (workdir, artifact_path). Every later surface in the same
process run re-reads that cached snapshot, so all of them report the SAME
numbers without re-running the producer. Cache scope is process-local — the
next `gitreins guard`/`doctor` invocation computes fresh.
"""

from __future__ import annotations

import json
import os
import subprocess
from typing import Any

from engine.config import QualityConfig

#: (abs workdir, artifact_path) → snapshot for the latest successful read in
#: this process. One entry per producer; a later surface never re-runs it.
_snapshot_cache: dict[tuple[str, str], dict[str, Any]] = {}


def read_quality_snapshot(workdir: str, config: QualityConfig) -> dict[str, Any]:
    """Run a configured producer once and read its JSON artifact.

    Values and targets are supplied by the repository. `stage` is descriptive
    only; only the metric target can cause an opt-in blocking failure.
    """
    if not config.enabled:
        return {"status": "disabled", "metrics": {}}
    if not config.command.strip():
        return {"status": "unavailable", "reason": "quality.command is empty", "metrics": {}}
    if not config.artifact_path.strip():
        return {"status": "unavailable", "reason": "quality.artifact_path is empty", "metrics": {}}
    # One authority per run: a surface that arrives after the producer already
    # ran reuses the cached snapshot instead of re-running the command. The
    # cached snapshot's per-metric modes are re-applied below so a caller with
    # a different per_metric_mode still grades the shared numbers itself.
    cache_key = (os.path.abspath(workdir), config.artifact_path)
    cached = _snapshot_cache.get(cache_key)
    if cached is not None:
        return _apply_modes(cached, config)
    artifact_path = os.path.join(workdir, config.artifact_path)
    try:
        completed = subprocess.run(  # noqa: S602 - user-configured command from quality-metrics config (trusted operator input)
            config.command,
            cwd=workdir,
            shell=True,
            check=False,
            capture_output=True,
            text=True,
            timeout=config.timeout,
        )
    except subprocess.TimeoutExpired:
        return {
            "status": "unavailable",
            "reason": f"command timed out after {config.timeout}s",
            "metrics": {},
        }
    except OSError as exc:
        return {"status": "unavailable", "reason": f"command could not run: {exc}", "metrics": {}}
    if completed.returncode:
        detail = completed.stderr.strip() or f"exit status {completed.returncode}"
        return {"status": "unavailable", "reason": f"command failed: {detail[:300]}", "metrics": {}}
    try:
        with open(artifact_path, encoding="utf-8") as artifact_file:
            artifact = json.load(artifact_file)
    except FileNotFoundError:
        return {
            "status": "unavailable",
            "reason": f"artifact not found: {config.artifact_path}",
            "metrics": {},
        }
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return {"status": "unavailable", "reason": f"artifact unreadable: {exc}", "metrics": {}}
    raw_metrics = artifact.get("metrics") if isinstance(artifact, dict) else None
    if not isinstance(raw_metrics, dict):
        return {"status": "unavailable", "reason": "artifact has no metrics object", "metrics": {}}
    metrics = {}
    for name, raw in raw_metrics.items():
        if not isinstance(name, str) or not isinstance(raw, dict):
            continue
        value, target = raw.get("value"), raw.get("target")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        if isinstance(target, bool) or not isinstance(target, (int, float)):
            target = None
        mode = config.per_metric_mode.get(name, "warn")
        if mode not in ("warn", "block"):
            mode = "warn"
        row = {"value": value, "target": target, "stage": raw.get("stage"), "mode": mode}
        if isinstance(target, (int, float)) and not isinstance(target, bool):
            row["met_target"] = value >= target
        else:
            row["met_target"] = None
        metrics[name] = row
    if not metrics:
        return {
            "status": "unavailable",
            "reason": "artifact contains no numeric metrics",
            "metrics": {},
        }
    snapshot = {
        "status": "available",
        "metrics": metrics,
        "produced_at": artifact.get("produced_at"),
        "command": artifact.get("command", config.command),
        "targets_source": config.targets_source or None,
        "diff_scope": _diff_scope(workdir, artifact),
    }
    # Published only after a complete read: a later surface either sees this
    # full snapshot or runs the producer itself — never a partial state.
    _snapshot_cache[cache_key] = snapshot
    return snapshot


def _diff_scope(workdir: str, artifact: dict[str, Any]) -> dict[str, Any]:
    """Match producer-owned surface metadata to the current git diff."""
    changed_files: list[str] = []
    try:
        staged = subprocess.run(
            ["git", "diff", "--cached", "--name-only", "-z", "HEAD"],  # noqa: S607 - git resolved via PATH by design
            cwd=workdir,
            capture_output=True,
            check=False,
        )
        staged_files = [p.decode(errors="replace") for p in staged.stdout.split(b"\0") if p]
        if staged.returncode == 0 and staged_files:
            changed_files = staged_files
        else:
            unstaged = subprocess.run(
                ["git", "diff", "--name-only", "-z", "HEAD"],  # noqa: S607 - git resolved via PATH by design
                cwd=workdir,
                capture_output=True,
                check=False,
            )
            if unstaged.returncode == 0:
                changed_files = [
                    p.decode(errors="replace") for p in unstaged.stdout.split(b"\0") if p
                ]
    except OSError:
        pass

    diff_file_count = len(set(changed_files))
    if "surfaces" not in artifact:
        return {
            "status": "unavailable",
            "reason": "producer artifact has no surfaces model",
            "base": "HEAD",
            "diff_files": diff_file_count,
            "surfaces": {},
        }
    floor_raw = artifact.get("class_floor")
    floor = {c for c in floor_raw if isinstance(c, str)} if isinstance(floor_raw, list) else set()
    touched: dict[str, Any] = {}
    surfaces = artifact.get("surfaces")
    if isinstance(surfaces, dict):
        for surface_id, metadata in surfaces.items():
            if not isinstance(surface_id, str) or not isinstance(metadata, dict):
                continue
            explicit_files = metadata.get("files")
            if isinstance(explicit_files, list):
                matches = any(p in changed_files for p in explicit_files if isinstance(p, str))
            else:
                matches = any(surface_id in path for path in changed_files)
            if not matches:
                continue
            links = metadata.get("test_links")
            if isinstance(links, bool) or links is None:
                link_status = "unknown"
            elif isinstance(links, int):
                link_status = "linked" if links > 0 else "none"
            elif isinstance(links, list):
                link_status = "linked" if links else "none"
            else:
                links, link_status = None, "unknown"
            raw_classes = metadata.get("classes")
            classes = (
                sorted({c for c in raw_classes if isinstance(c, str)})
                if isinstance(raw_classes, list)
                else None
            )
            touched[surface_id] = {
                "test_links": links,
                "link_status": link_status,
                "classes": classes,
                "missing_classes": sorted(floor - set(classes)) if classes is not None else [],
            }
    return {
        "status": "available" if touched else "no_diff",
        "base": "HEAD",
        "diff_files": len(set(changed_files)),
        "surfaces": touched,
    }


def quality_snapshot_peek(workdir: str, artifact_path: str = "") -> dict[str, Any] | None:
    """Read the run's cached snapshot WITHOUT running the producer (GR-143).

    Returns None when no surface of this run has computed a snapshot for
    (workdir, artifact_path) yet — an agent-facing read, never a trigger.
    With artifact_path empty, any cached entry for the workdir is returned.
    """
    if artifact_path:
        return _snapshot_cache.get((os.path.abspath(workdir), artifact_path))
    prefix = os.path.abspath(workdir)
    for (cached_wd, _path), snapshot in _snapshot_cache.items():
        if cached_wd == prefix:
            return snapshot
    return None


def _apply_modes(snapshot: dict[str, Any], config: QualityConfig) -> dict[str, Any]:
    """Clone a cached AVAILABLE snapshot under a caller's per-metric modes.
    The cache stores the raw computed snapshot; this re-derives each metric's
    ``mode`` and ``met_target`` from the CALLER's config so one shared set of
    numbers can be graded with different policies without re-running the
    producer. Metrics the caller does not name fall back to ``warn``.
    """

    metrics: dict[str, Any] = {}
    for name, metric in snapshot.get("metrics", {}).items():
        row = dict(metric)
        mode = config.per_metric_mode.get(name, "warn")
        if mode not in ("warn", "block"):
            mode = "warn"
        row["mode"] = mode
        target = row.get("target")
        if isinstance(target, (int, float)) and not isinstance(target, bool):
            row["met_target"] = row["value"] >= target
        else:
            row["met_target"] = None
        metrics[name] = row
    out = dict(snapshot)
    out["metrics"] = metrics
    return out


def quality_blocks(snapshot: dict[str, Any]) -> bool:
    """True only when a block-mode metric has a declared unmet target."""
    return snapshot.get("status") == "available" and any(
        metric.get("mode") == "block" and metric.get("met_target") is False
        for metric in snapshot.get("metrics", {}).values()
    )


def format_quality_snapshot(snapshot: dict[str, Any]) -> str:
    if snapshot.get("status") == "disabled":
        return ""
    if snapshot.get("status") != "available":
        return f"quality: unavailable ({snapshot.get('reason', 'unknown reason')})"
    command = snapshot.get("command")
    parts = []
    for name, metric in snapshot["metrics"].items():
        value, target, mode = metric["value"], metric.get("target"), metric["mode"]
        stage = metric.get("stage")
        value_text = f"{value:g}%"
        target_text = "n/a" if target is None else f"{target:g}%"
        # Value + target + stage + the producer command, on every line, so an
        # agent reading any single surface knows where the number came from
        # (GR-143) without opening the artifact.
        parts.append(
            f"{name}={value_text} (target {target_text}, {mode}, stage {stage}, via {command})"
        )
    lines = ["quality: " + "; ".join(parts)]
    scope = snapshot.get("diff_scope")
    if isinstance(scope, dict):
        status = scope.get("status")
        if status == "available":
            surfaces = scope.get("surfaces", {})
            lines.append(
                f"diff-scope: {scope.get('diff_files', 0)} changed files, "
                f"{len(surfaces)} surfaces touched"
            )
            for surface_id, row in surfaces.items():
                raw_classes = row.get("classes")
                classes_text = "unknown" if raw_classes is None else ",".join(raw_classes) or "none"
                lines.append(
                    f"  surface {surface_id}: test_links={row.get('link_status', 'unknown')}, "
                    f"classes={classes_text}, "
                    f"missing_classes={','.join(row.get('missing_classes', [])) or 'none'}"
                )
                if row.get("link_status") == "none":
                    lines.append(f"  WARNING: surface {surface_id} has no test link")
        elif status == "unavailable":
            lines.append(f"diff-scope: unavailable ({scope.get('reason', 'unknown reason')})")
        else:
            lines.append("diff-scope: no artifact surfaces touched")
    return "\n".join(lines)
