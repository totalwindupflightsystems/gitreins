"""Bounded concurrent execution of task commands in isolated worktrees.

The fleet is deliberately scheduler-agnostic: callers provide an explicit
manifest of lane commands, while GitReins owns worktree isolation, durable
phase evidence, and serialized optional merge-back.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

from engine.config import load_defaults
from engine.evidence_bounds import MAX_EVIDENCE_CHARS, bound_evidence
from engine.worktree_manager import WorktreeError, WorktreeManager, validate_task_id


class FleetValidationError(WorktreeError):
    """Raised when a fleet manifest or concurrency cap is invalid."""


def _command(value, field: str, task_id: str) -> tuple[str, ...] | None:
    if value is None:
        return None
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise FleetValidationError(f"lane {task_id!r} {field} must be an argv list")
    result = tuple(value)
    if not result or any(not isinstance(item, str) or not item for item in result):
        raise FleetValidationError(f"lane {task_id!r} {field} must contain non-empty strings")
    return result


@dataclass(frozen=True)
class FleetLane:
    """One independently executable task lane.

    ``command`` is the implementation command.  Optional ``guard`` and
    ``judge`` commands run in the same tree after it and make the durable
    guarding/judging phases explicit without coupling to a particular runner.
    """

    task_id: str
    command: tuple[str, ...]
    guard: tuple[str, ...] | None = None
    judge: tuple[str, ...] | None = None
    priority: int = 0
    brief_path: str | None = None
    timeout_seconds: float | None = None

    def __post_init__(self) -> None:
        validate_task_id(self.task_id)
        if (
            isinstance(self.command, (str, bytes))
            or not self.command
            or any(not isinstance(item, str) or not item for item in self.command)
        ):
            raise FleetValidationError(
                f"lane {self.task_id!r} command must be a non-empty argv list"
            )
        for field_name, command in (("guard", self.guard), ("judge", self.judge)):
            if command is not None and (
                isinstance(command, (str, bytes))
                or not command
                or any(not isinstance(item, str) or not item for item in command)
            ):
                raise FleetValidationError(
                    f"lane {self.task_id!r} {field_name} must be an argv list"
                )
        if not isinstance(self.priority, int) or isinstance(self.priority, bool):
            raise FleetValidationError(f"lane {self.task_id!r} priority must be an integer")
        if self.timeout_seconds is not None and (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or self.timeout_seconds <= 0
        ):
            raise FleetValidationError(f"lane {self.task_id!r} timeout_seconds must be positive")

    @classmethod
    def from_dict(cls, data: dict) -> "FleetLane":
        if not isinstance(data, dict):
            raise FleetValidationError("each fleet lane must be an object")
        task_id = data.get("task_id", data.get("id"))
        if not isinstance(task_id, str):
            raise FleetValidationError("each fleet lane requires a string task_id")
        command = _command(data.get("command"), "command", task_id)
        if command is None:
            raise FleetValidationError(f"lane {task_id!r} requires command")
        return cls(
            task_id=task_id,
            command=command,
            guard=_command(data.get("guard"), "guard", task_id),
            judge=_command(data.get("judge"), "judge", task_id),
            priority=data.get("priority", 0),
            brief_path=data.get("brief_path"),
            timeout_seconds=data.get("timeout_seconds"),
        )


def _validate_cap(cap) -> int:
    if isinstance(cap, bool) or not isinstance(cap, int) or cap < 1:
        raise FleetValidationError(
            f"max_concurrent_worktrees must be a positive integer, got {cap!r}"
        )
    return cap


def _evidence(output: str) -> str:
    """Bound command evidence on LINE boundaries, keeping BOTH ends.

    DF-GITREINS-POC-19: this used to be a head-only slice at a raw character
    offset (``output[:MAX_EVIDENCE_CHARS - 40]``), so the evidence a
    ``worktree fresh`` lane records — and the QA ledger row built from it —
    ended in a half-written line and threw away the tail, which is where
    pytest's short test summary names the failing test. It now delegates to
    the one bounder every other evidence surface uses
    (``engine.evidence_bounds``), so the rules cannot drift again.
    """
    return bound_evidence(output.strip(), cap=MAX_EVIDENCE_CHARS)


#: How much of a failing phase's output the lane-level error carries.  The lane
#: error is what a foreman reads in a tick report (and what the next actor gets
#: when a merge refuses), so it has to name WHY in one line — the phase, the
#: exit code and the failing criterion the judge printed — while staying short.
_FAILURE_DETAIL_LINES = 4
_FAILURE_DETAIL_CHARS = 400


def _failure_detail(output: str) -> str:
    """The tail of a failing phase's output, on line boundaries."""
    lines = [line.strip() for line in (output or "").splitlines() if line.strip()]
    if not lines:
        return ""
    return " | ".join(lines[-_FAILURE_DETAIL_LINES:])[:_FAILURE_DETAIL_CHARS]


def _phase_error(phase: str, exit_code: int, output: str) -> str:
    """WHY a lane phase failed — the lane result's own error field.

    DF-GITREINS-POC-48: a failed lane used to report ``passed: false`` with an
    EMPTY ``error`` (the registry had a terse one, the tick report had none) and
    no merge error, so a judge-phase failure and a merge refusal looked
    identical from the outside.  A judge failure in particular means there is
    no verdict for this commit, which is exactly why ``--merge`` will refuse
    the lane: say that, plus the line the judge actually printed.
    """
    message = f"{phase} phase failed: command exited {exit_code}"
    if phase == "judging":
        message += (
            " — no PASS verdict exists for this worktree's commit, so the "
            "judge-gated merge will refuse this lane"
        )
    if "Task not found" in (output or ""):
        message += (
            ". The judge command named a task that does not exist in this lane's "
            "store: `.gitreins/tasks.yaml` is per-checkout and untracked, so a "
            "fresh worktree starts empty — create the task in the canonical "
            "checkout before the run (the fleet copies it into the lane) or in "
            "the lane's own command phase"
        )
    detail = _failure_detail(output)
    if detail:
        message += f"; last output: {detail}"
    return message


class WorktreeFleet:
    """Run explicit lanes concurrently, bounded by a host-safe cap."""

    def __init__(
        self,
        workdir: str | os.PathLike[str] | None = None,
        *,
        manager: WorktreeManager | None = None,
        max_concurrent_worktrees: int | None = None,
    ):
        self.manager = manager or WorktreeManager(workdir)
        defaults = load_defaults(str(self.manager.main_root))
        configured_cap = defaults.max_concurrent_worktrees
        self.max_concurrent_worktrees = _validate_cap(
            configured_cap if max_concurrent_worktrees is None else max_concurrent_worktrees
        )

    @staticmethod
    def lanes_from_manifest(manifest) -> list[FleetLane]:
        if isinstance(manifest, dict):
            manifest = manifest.get("lanes")
        if not isinstance(manifest, list) or not manifest:
            raise FleetValidationError("fleet manifest must contain a non-empty lanes list")
        lanes = [FleetLane.from_dict(item) for item in manifest]
        ids = [lane.task_id for lane in lanes]
        if len(ids) != len(set(ids)):
            raise FleetValidationError("fleet manifest contains duplicate task_id values")
        return lanes

    def run(
        self,
        lanes: Iterable[FleetLane],
        *,
        tick: str | None = None,
        merge: bool = False,
        force_merge: bool = False,
        merge_actor: str | None = None,
    ) -> dict:
        """Execute lanes and return a stable, JSON-serializable tick report.

        Worktrees are created before execution, so every accepted lane has one
        deterministic task tree.  Lane execution is bounded by the configured
        cap.  If ``merge`` is true, successful lanes are applied in priority,
        task-id order through WorktreeManager's inter-process merge lock, and
        every lane that did NOT merge — a failed phase, or the verdict gate's
        refusal — is reported in ``merge_errors`` with its reason, so a tick
        report never shows a lane that silently went missing (DF-GITREINS-POC-48).
        """
        lanes = list(lanes)
        if not lanes:
            raise FleetValidationError("fleet run requires at least one lane")
        lanes = [FleetLane.from_dict(lane) if isinstance(lane, dict) else lane for lane in lanes]
        if any(not isinstance(lane, FleetLane) for lane in lanes):
            raise FleetValidationError("fleet run accepts FleetLane objects or lane dictionaries")
        ids = [lane.task_id for lane in lanes]
        if len(ids) != len(set(ids)):
            raise FleetValidationError("fleet run contains duplicate task_id values")
        if force_merge and not merge:
            raise FleetValidationError("force_merge requires merge=True")
        if force_merge and (not isinstance(merge_actor, str) or not merge_actor.strip()):
            raise FleetValidationError("force_merge requires a non-empty merge_actor")

        records = {}
        for lane in sorted(lanes, key=lambda item: (item.priority, item.task_id)):
            record, _created = self.manager.create(
                lane.task_id,
                brief_path=lane.brief_path,
                tick=tick,
            )
            records[lane.task_id] = record

        results: dict[str, dict] = {}
        with ThreadPoolExecutor(
            max_workers=self.max_concurrent_worktrees,
            thread_name_prefix="gitreins-fleet",
        ) as executor:
            futures: dict[Future, FleetLane] = {
                executor.submit(self._run_lane, lane, records[lane.task_id]): lane for lane in lanes
            }
            for future in as_completed(futures):
                lane = futures[future]
                try:
                    results[lane.task_id] = future.result()
                except Exception as exc:  # lane failures become truthful evidence, not lost futures
                    message = f"fleet lane failed before completion: {exc}"
                    self.manager.mark_lane(
                        lane.task_id, "failed", error=message, result={"passed": False}
                    )
                    results[lane.task_id] = {
                        "task_id": lane.task_id,
                        "state": "failed",
                        "passed": False,
                        "error": message,
                        "worktree": records[lane.task_id].path,
                    }

        merge_order: list[str] = []
        merge_errors: dict[str, str] = {}
        if merge:
            for lane in sorted(lanes, key=lambda item: (item.priority, item.task_id)):
                result = results[lane.task_id]
                if result.get("state") != "completed":
                    # DF-GITREINS-POC-48: a lane that failed its own phases never
                    # reaches the verdict gate, and this loop used to skip it
                    # silently — a tick report then showed a failed lane with no
                    # merge error at all.  Record WHY it was not merged, from the
                    # lane's own error.
                    merge_errors[lane.task_id] = (
                        f"not merged: {result.get('error') or 'lane did not complete'}"
                    )
                    result["merge"] = "not-merged"
                    continue
                try:
                    merged = self.manager.merge(
                        lane.task_id,
                        force=force_merge,
                        actor=merge_actor,
                    )
                except WorktreeError as exc:
                    merge_errors[lane.task_id] = str(exc)
                    self.manager.mark_lane(
                        lane.task_id,
                        "failed",
                        error=f"merge refused: {exc}",
                        result={"passed": False, "merge": "refused"},
                    )
                    result["state"] = "failed"
                    result["passed"] = False
                    result["error"] = f"merge refused: {exc}"
                else:
                    merge_order.append(lane.task_id)
                    result["state"] = "merged"
                    result["merge"] = merged

        ordered_results = [
            results[lane.task_id]
            for lane in sorted(lanes, key=lambda item: (item.priority, item.task_id))
        ]
        return {
            "cap": self.max_concurrent_worktrees,
            "tick": tick,
            "lanes": ordered_results,
            "merge_order": merge_order,
            "merge_errors": {key: merge_errors[key] for key in sorted(merge_errors)},
        }

    def _seed_judge_task(self, lane: FleetLane, tree: Path) -> bool:
        """Give the lane tree the task its judge phase will name.

        DF-GITREINS-POC-48: ``.gitreins/tasks.yaml`` is per-checkout and
        untracked by design, so a freshly created lane tree has no task store —
        a manifest lane whose judge phase names the lane id (``["gitreins",
        "judge", "API-1"]``, the pattern the README documents) died with the
        CLI's bare ``Task not found`` even though the task existed in canonical
        main.  The fleet knows both the lane id and the main store, so it seeds
        the lane's own copy *before* the judge phase runs.

        Only copies: the task is taken verbatim (title + criteria) from the
        canonical store, so nothing is invented and a manifest cannot smuggle
        in criteria the task does not have.  A lane that already created (or a
        previous phase that already seeded) its own task is left untouched, and
        a lane whose id names no task anywhere is left to fail loudly with the
        reason attached (:func:`_phase_error`).

        Returns True when this run wrote the lane's store.
        """
        from engine.task_manager import TaskManager

        task = TaskManager(str(self.manager.main_root)).get(lane.task_id)
        if task is None:
            return False
        lane_store = TaskManager(str(tree))
        if lane_store.get(lane.task_id) is not None:
            return False
        lane_store.create(
            lane.task_id,
            task.title,
            list(task.criteria),
            depends_on=list(task.depends_on),
        )
        return True

    def _run_lane(self, lane: FleetLane, record) -> dict:
        stages = [("running", lane.command)]
        if lane.guard is not None:
            stages.append(("guarding", lane.guard))
        if lane.judge is not None:
            stages.append(("judging", lane.judge))
        stage_results = []
        for phase, command in stages:
            if phase == "judging" and self._seed_judge_task(lane, Path(record.path)):
                # Not a stage of its own: seeding is bookkeeping for the judge
                # phase, so it is recorded as an event, never as a phase that
                # could silently absorb a failure.
                self.manager._append_board_event(
                    {
                        "event_type": "worktree_lane_task_seeded",
                        "task_id": lane.task_id,
                        "task": lane.task_id,
                    }
                )
            self.manager.mark_lane(lane.task_id, phase, command=list(command))
            self.manager._append_board_event(
                {"event_type": "worktree_lane_phase", "task_id": lane.task_id, "phase": phase}
            )
            stage = self._run_command(
                command, Path(record.path), lane.task_id, lane.timeout_seconds
            )
            stage_results.append({"phase": phase, **stage})
            if stage["exit_code"] != 0:
                error = _phase_error(phase, stage["exit_code"], stage["output"])
                result = {"passed": False, "stages": stage_results, "error": error}
                self.manager.mark_lane(
                    lane.task_id,
                    "failed",
                    result=result,
                    exit_code=stage["exit_code"],
                    output=stage["output"],
                    error=error,
                )
                return {
                    "task_id": lane.task_id,
                    "state": "failed",
                    "passed": False,
                    "exit_code": stage["exit_code"],
                    "output": stage["output"],
                    "error": error,
                    "stages": stage_results,
                    "worktree": record.path,
                    "branch": record.branch,
                }
        result = {"passed": True, "stages": stage_results}
        final = stage_results[-1]
        self.manager.mark_lane(
            lane.task_id,
            "completed",
            result=result,
            exit_code=final["exit_code"],
            output=final["output"],
        )
        self.manager._append_board_event(
            {"event_type": "worktree_lane_completed", "task_id": lane.task_id, "passed": True}
        )
        return {
            "task_id": lane.task_id,
            "state": "completed",
            "passed": True,
            "exit_code": final["exit_code"],
            "output": final["output"],
            "stages": stage_results,
            "worktree": record.path,
            "branch": record.branch,
        }

    def _run_command(
        self, command: tuple[str, ...], cwd: Path, task_id: str, timeout: float | None
    ) -> dict:
        started = time.monotonic()
        try:
            process = subprocess.Popen(
                list(command),
                cwd=str(cwd),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=True,
            )
        except OSError as exc:
            return {"exit_code": -1, "output": str(exc)}
        try:
            while True:
                try:
                    process.wait(timeout=0.25)
                    break
                except subprocess.TimeoutExpired:
                    if timeout is not None and time.monotonic() - started > timeout:
                        process.kill()
                        process.wait()
                        output = process.communicate()[0] or ""
                        return {
                            "exit_code": -1,
                            "output": _evidence(output + f"\ncommand timed out after {timeout}s"),
                        }
                    self.manager.heartbeat(task_id)
            output = process.communicate()[0] or ""
        except Exception:
            process.kill()
            process.wait()
            raise
        return {
            "exit_code": process.returncode,
            "output": _evidence(output),
        }


def load_fleet_manifest(path: str | os.PathLike[str]) -> list[FleetLane]:
    """Load a JSON or YAML fleet manifest from disk."""
    manifest_path = Path(path)
    try:
        raw = manifest_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise FleetValidationError(f"could not read fleet manifest {manifest_path}: {exc}") from exc
    try:
        if manifest_path.suffix.lower() in {".yaml", ".yml"}:
            import yaml

            data = yaml.safe_load(raw)
        else:
            data = json.loads(raw)
    except Exception as exc:
        raise FleetValidationError(
            f"could not parse fleet manifest {manifest_path}: {exc}"
        ) from exc
    return WorktreeFleet.lanes_from_manifest(data)


ParallelWorktreeFleet = WorktreeFleet
