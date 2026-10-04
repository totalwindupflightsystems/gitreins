"""Consume repo-produced quality metrics without computing them."""

from __future__ import annotations

import json
import os
import subprocess
from typing import Any


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
    return {
        "status": "available",
        "metrics": metrics,
        "produced_at": artifact.get("produced_at"),
        "command": artifact.get("command", config.command),
        "targets_source": config.targets_source or None,
    }


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
    parts = []
    for name, metric in snapshot["metrics"].items():
        value, target, mode = metric["value"], metric.get("target"), metric["mode"]
        value_text = f"{value:g}%" if name.endswith("_pct") else f"{value:g}"
        target_text = (
            "n/a" if target is None else f"{target:g}%" if name.endswith("_pct") else f"{target:g}"
        )
        parts.append(f"{name}={value_text} (target {target_text}, {mode})")
    return "quality: " + "; ".join(parts)
