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
from engine.worktree_manager import WorktreeError, WorktreeManager, validate_task_id

MAX_EVIDENCE_CHARS = 4000


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
    output = output.strip()
    if len(output) <= MAX_EVIDENCE_CHARS:
        return output
    return output[: MAX_EVIDENCE_CHARS - 40] + "\n… [output truncated]"


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
        task-id order through WorktreeManager's inter-process merge lock.
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

    def _run_lane(self, lane: FleetLane, record) -> dict:
        stages = [("running", lane.command)]
        if lane.guard is not None:
            stages.append(("guarding", lane.guard))
        if lane.judge is not None:
            stages.append(("judging", lane.judge))
        stage_results = []
        for phase, command in stages:
            self.manager.mark_lane(lane.task_id, phase, command=list(command))
            self.manager._append_board_event(
                {"event_type": "worktree_lane_phase", "task_id": lane.task_id, "phase": phase}
            )
            stage = self._run_command(
                command, Path(record.path), lane.task_id, lane.timeout_seconds
            )
            stage_results.append({"phase": phase, **stage})
            if stage["exit_code"] != 0:
                result = {"passed": False, "stages": stage_results}
                self.manager.mark_lane(
                    lane.task_id,
                    "failed",
                    result=result,
                    exit_code=stage["exit_code"],
                    output=stage["output"],
                    error=f"{phase} command exited {stage['exit_code']}",
                )
                return {
                    "task_id": lane.task_id,
                    "state": "failed",
                    "passed": False,
                    "exit_code": stage["exit_code"],
                    "output": stage["output"],
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
