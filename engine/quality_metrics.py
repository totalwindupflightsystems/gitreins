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

#: (abs workdir, artifact_path) → snapshot for the latest successful read in
#: this process. One entry per producer; a later surface never re-runs it.
_snapshot_cache: dict[tuple[str, str], dict[str, Any]] = {}


def read_quality_snapshot(workdir: str, config) -> dict[str, Any]:
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
        completed = subprocess.run(
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
    }
    # Published only after a complete read: a later surface either sees this
    # full snapshot or runs the producer itself — never a partial state.
    _snapshot_cache[cache_key] = snapshot
    return snapshot


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


def _apply_modes(snapshot: dict[str, Any], config) -> dict[str, Any]:
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
    return "quality: " + "; ".join(parts)
