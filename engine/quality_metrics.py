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
from engine.guard_manager import _is_harness_state_path

#: (abs workdir, artifact_path) → snapshot for the latest successful read in
#: this process. One entry per producer; a later surface never re-runs it.
_snapshot_cache: dict[tuple[str, str], dict[str, Any]] = {}


def _load_target_ladder(
    workdir: str, targets_source: str
) -> tuple[dict[str, Any] | None, str | None]:
    """Read ``targets_source`` as a flat metric-name → number mapping (DF-GITREINS-POC-95).

    Returns (ladder, None) on success, or (None, reason) when the configured
    path cannot serve as a ladder (missing, unreadable, wrong shape,
    non-numeric entries). Fail-soft by design: a broken ladder must degrade
    to the artifact's own targets, never raise out of a snapshot read — but
    the reason is returned so the snapshot can surface it instead of
    silently ignoring the configuration.
    """
    path = os.path.normpath(os.path.join(workdir, targets_source))
    try:
        with open(path, encoding="utf-8") as ladder_file:
            ladder = json.load(ladder_file)
    except FileNotFoundError:
        return None, "file not found"
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return None, f"unreadable: {exc}"
    if not isinstance(ladder, dict):
        return None, f"expected a JSON object of metric-name -> number, got {type(ladder).__name__}"
    clean: dict[str, Any] = {}
    for name, target in ladder.items():
        if not isinstance(name, str):
            continue
        if isinstance(target, bool) or not isinstance(target, (int, float)):
            continue
        clean[name] = target
    if not clean:
        return None, "no numeric metric targets in the mapping"
    return clean, None


def _effective_target(
    artifact_target: float | None,
    ladder: dict[str, Any] | None,
    ladder_error: str | None,
    name: str,
) -> dict[str, Any]:
    """Effective target + provenance for one metric (DF-GITREINS-POC-95).

    The declared ladder is authoritative when it names the metric: the
    STRICTER of ladder and artifact wins, so a graded repository cannot pass
    a block gate by lowering (or dropping) the target inside the artifact it
    itself produces. ``target_source`` says which number won:
    ``ladder+artifact`` / ``ladder`` / ``artifact``, or
    ``ladder unavailable: <reason>`` when the configured ladder could not be
    read (fail-soft, never silent). Both raw numbers ride along as
    ``ladder_target`` / ``artifact_target`` so a reader can see WHICH won.
    """
    row: dict[str, Any] = {"target": artifact_target, "target_source": "artifact"}
    ladder_target: float | None = None
    if ladder is not None:
        candidate = ladder.get(name)
        if isinstance(candidate, (int, float)) and not isinstance(candidate, bool):
            ladder_target = candidate
    if ladder_target is not None and artifact_target is not None:
        row["ladder_target"] = ladder_target
        row["artifact_target"] = artifact_target
        row["target"] = max(ladder_target, artifact_target)
        row["target_source"] = "ladder+artifact"
    elif ladder_target is not None:
        row["ladder_target"] = ladder_target
        row["target"] = ladder_target
        row["target_source"] = "ladder"
    elif ladder_error is not None:
        row["target_source"] = f"ladder unavailable: {ladder_error}"
    return row


def _launch_producer_background(workdir: str, config: QualityConfig) -> None:
    """Start the configured producer detached so it outlives the guard process.

    PERF-1: the commit path must never wait on the measurement. stdout/stderr
    are appended to ``.gitreins/logs/quality-producer.log`` (never interleaved
    with guard output), and ``start_new_session=True`` detaches the child from
    the guard's process group so the guard returns immediately while the
    producer keeps writing its artifact in the background. A launch failure is
    swallowed here — the read-back reports ``artifact not yet written`` (or the
    last completed artifact) and never raises out of a snapshot read.
    """
    logs_dir = os.path.join(workdir, ".gitreins", "logs")
    try:
        os.makedirs(logs_dir, exist_ok=True)
        with open(os.path.join(logs_dir, "quality-producer.log"), "ab") as log_file:
            subprocess.Popen(  # noqa: S602 - user-configured producer command (trusted operator input)
                config.command,
                cwd=workdir,
                shell=True,
                stdout=log_file,
                stderr=log_file,
                start_new_session=True,
            )
    except OSError:
        pass


def read_quality_snapshot(
    workdir: str, config: QualityConfig, async_run: bool = False
) -> dict[str, Any]:
    """Run a configured producer once and read its JSON artifact.

    Values are supplied by the repository; the metric target is the stricter
    of the artifact's declared target and the configured ladder's
    (``targets_source``, DF-GITREINS-POC-95), so a graded repository cannot
    pass a block gate by lowering the target inside its own artifact.
    ``stage`` is descriptive only; only the metric target can cause an
    opt-in blocking failure.

    ``async_run`` (PERF-1) launches the producer detached and reads the LAST
    COMPLETED artifact instead of waiting for this run's — the commit path
    (pre-commit hook) never blocks on the measurement. The pre-push
    ``quality-ensure`` surface runs the producer to completion (sync) so fresh
    numbers exist before the refs leave the machine.
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
    if async_run:
        # PERF-1: fire-and-forget — start the producer detached, then read the
        # last completed artifact (the previous run's numbers) below. No wait.
        _launch_producer_background(workdir, config)
    else:
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
            return {
                "status": "unavailable",
                "reason": f"command could not run: {exc}",
                "metrics": {},
            }
        if completed.returncode:
            detail = completed.stderr.strip() or f"exit status {completed.returncode}"
            return {
                "status": "unavailable",
                "reason": f"command failed: {detail[:300]}",
                "metrics": {},
            }
    try:
        with open(artifact_path, encoding="utf-8") as artifact_file:
            artifact = json.load(artifact_file)
    except FileNotFoundError:
        reason = (
            "producer started in background — artifact not yet written"
            if async_run
            else f"artifact not found: {config.artifact_path}"
        )
        return {"status": "unavailable", "reason": reason, "metrics": {}}
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return {"status": "unavailable", "reason": f"artifact unreadable: {exc}", "metrics": {}}
    raw_metrics = artifact.get("metrics") if isinstance(artifact, dict) else None
    if not isinstance(raw_metrics, dict):
        return {"status": "unavailable", "reason": "artifact has no metrics object", "metrics": {}}
    # DF-GITREINS-POC-95: the declared ladder, when configured and readable,
    # supplies/overrides each metric's target (stricter of the two wins).
    # A configured ladder that cannot be read degrades to the artifact's
    # targets with the reason surfaced per metric — fail-soft, never silent.
    ladder: dict[str, Any] | None = None
    ladder_error: str | None = None
    if config.targets_source.strip():
        ladder, ladder_error = _load_target_ladder(workdir, config.targets_source.strip())
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
        row = {"value": value, **_effective_target(target, ladder, ladder_error, name)}
        row["stage"] = raw.get("stage")
        row["mode"] = mode
        if isinstance(row["target"], (int, float)) and not isinstance(row["target"], bool):
            row["met_target"] = value >= row["target"]
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
        "diff_scope": _diff_scope(workdir, artifact, artifact_path=config.artifact_path),
    }
    # Published only after a complete read: a later surface either sees this
    # full snapshot or runs the producer itself — never a partial state.
    _snapshot_cache[cache_key] = snapshot
    return snapshot


def _diff_scope(workdir: str, artifact: dict[str, Any], artifact_path: str = "") -> dict[str, Any]:
    """Match producer-owned surface metadata to the current git diff.

    ``artifact_path`` (workdir-relative) and everything under harness state
    (``.gitreins/**``) are excluded from the change set: the producer owns
    those files, so their per-run rewrites are never the user's change.
    """
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

    artifact_rel = os.path.normpath(artifact_path) if artifact_path else ""
    changed_files = [
        p for p in changed_files if p != artifact_rel and not _is_harness_state_path(p)
    ]

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
    DF-GITREINS-POC-95: re-derivation grades against the EFFECTIVE target
    (the cached row's already-resolved target, provenance included), never
    the artifact's raw number — the ladder would otherwise lose its
    authority on every cached re-read.
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
        # DF-GITREINS-POC-95: when the declared ladder and the artifact
        # disagree on the target, name the disagreement on the line so the
        # effective (stricter) number is never mistaken for the producer's
        # own claim. Agreement — or no ladder participation — renders
        # byte-identically to the pre-fix output.
        ladder_target = metric.get("ladder_target")
        artifact_target = metric.get("artifact_target")
        if (
            isinstance(ladder_target, (int, float))
            and not isinstance(ladder_target, bool)
            and isinstance(artifact_target, (int, float))
            and not isinstance(artifact_target, bool)
            and ladder_target != artifact_target
        ):
            if ladder_target > artifact_target:
                target_text += f" (ladder beats artifact target {artifact_target:g}%)"
            else:
                target_text += f" (artifact beats ladder target {ladder_target:g}%)"
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
