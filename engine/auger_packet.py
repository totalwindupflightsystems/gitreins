"""Canonical boardctl/Auger task packet intake for GitReins judges (GR-155).

Auger owns XML parsing and packet assembly.  This module consumes the resulting
JSON-shaped packet, checks that its resolved references are internally
consistent, and runs its tracked test obligations through the repository-owned
pytest harness.  Worker prose is retained only as explicitly untrusted context.
"""

from __future__ import annotations

import copy
import os
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass
from typing import Any

from engine import command_hygiene
from engine.config import load_raw_config

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]*$")
_SAFE_SELECTOR = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:::[A-Za-z_][A-Za-z0-9_]*)*$")
_ACTIVE_STATES = {"active", "current", "approved"}


class PacketValidationError(ValueError):
    """The immutable packet cannot be used as judge authority."""

    def __init__(self, unresolved_ids: list[str]) -> None:
        self.unresolved_ids = list(dict.fromkeys(unresolved_ids))
        super().__init__("unresolved Auger packet references: " + ", ".join(self.unresolved_ids))


@dataclass(frozen=True)
class PacketTestResult:
    """Commit-bound result of one packet-declared tracked test."""

    id: str
    target: str
    passed: bool
    exit_code: int | None
    output: str
    evaluated_commit: str
    command: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "target": self.target,
            "passed": self.passed,
            "exit_code": self.exit_code,
            "output": self.output,
            "evaluated_commit": self.evaluated_commit,
            "command": list(self.command),
        }


class TaskPacketAdapter:
    """Translate one canonical board row plus an Auger packet for the judge."""

    def __init__(self, workdir: str) -> None:
        self.workdir = os.path.realpath(workdir)

    def prepare(
        self,
        board_task: dict[str, Any],
        packet: dict[str, Any],
        *,
        worker_summary: str | None = None,
    ) -> dict[str, Any]:
        """Validate and create the existing evaluator's task context.

        The packet is already assembled by Auger; no XML is parsed here.  The
        complete packet is deep-copied into the context so scope, test refs,
        source revisions, and digests survive without lossy translation.
        """
        unresolved = self._unresolved(board_task, packet)
        if unresolved:
            raise PacketValidationError(unresolved)

        board_id = str(board_task["id"])
        mapping = packet.get("id_mapping")
        task_id = board_id
        if isinstance(mapping, dict):
            task_id = str(mapping.get("gitreins_task_id") or mapping.get(board_id) or board_id)

        acceptance = copy.deepcopy(board_task["acceptance_criteria"])
        criteria = acceptance if isinstance(acceptance, list) else [acceptance]
        summary = worker_summary
        if summary is None:
            raw_summary = board_task.get("worker_summary")
            summary = str(raw_summary) if raw_summary is not None else None

        return {
            "id": task_id,
            "board_id": board_id,
            "board_id_mapping": copy.deepcopy(mapping),
            "title": str(board_task.get("title", "")),
            "criteria": copy.deepcopy(criteria),
            "board_acceptance_criteria": acceptance,
            "auger_packet": copy.deepcopy(packet),
            "worker_summary": {
                "value": summary,
                "trust": "untrusted",
                "authoritative": False,
            },
            "authority": {
                "acceptance_criteria": "boardctl",
                "spec_blocks": "auger_packet",
                "test_results": "gitreins_tier1",
                "worker_summary": "untrusted_context_only",
            },
        }

    def run_tests(self, context: dict[str, Any]) -> list[PacketTestResult]:
        """Run every tracked packet test via the configured pytest harness."""
        packet = context["auger_packet"]
        commit = self._evaluated_commit()
        test_refs = packet.get("test_refs") or []
        results: list[PacketTestResult] = []
        for ref in test_refs:
            if not self._ref_paths_match_head(ref):
                raise PacketValidationError([str(ref["id"])])
            target = str(ref["path"])
            selector = str(ref.get("selector", ""))
            if selector:
                target = f"{target}::{selector}"
            command = self._pytest_harness_argv(target)
            run = command_hygiene.run_bounded(
                command,
                cwd=self.workdir,
                timeout=self._test_timeout(),
                max_output=10_000_000,
                env=self._test_env(),
            )
            output = str(run.get("output") or run.get("error") or run.get("reason") or "")
            exit_code = run.get("exit_code")
            passed = not run.get("refused") and not run.get("timed_out") and exit_code == 0
            results.append(
                PacketTestResult(
                    id=str(ref["id"]),
                    target=target,
                    passed=passed,
                    exit_code=exit_code if isinstance(exit_code, int) else None,
                    output=output,
                    evaluated_commit=commit,
                    command=tuple(command),
                )
            )
        return results

    def _unresolved(self, board_task: dict[str, Any], packet: dict[str, Any]) -> list[str]:
        unresolved: list[str] = []
        board_id = board_task.get("id")
        if not isinstance(board_id, str) or not board_id:
            unresolved.append("board:id")
        if "acceptance_criteria" not in board_task:
            unresolved.append(f"board:{board_id or 'unknown'}:acceptance_criteria")

        packet_board_id = packet.get("board_id")
        mapping = packet.get("id_mapping")
        mapped_board_id = mapping.get("board_id") if isinstance(mapping, dict) else None
        if board_id and packet_board_id != board_id and mapped_board_id != board_id:
            unresolved.extend([str(board_id), str(packet_board_id or "packet:board_id")])

        trace = packet.get("ch_trace")
        if not isinstance(trace, dict):
            unresolved.append("ch:trace")
            trace = {}
        if board_id and trace.get("row") != board_id:
            unresolved.append(str(trace.get("row") or f"ch:trace:row:{board_id}"))

        raw_scope = packet.get("scope")
        scope: dict[str, Any] = raw_scope if isinstance(raw_scope, dict) else {}
        in_scope = self._id_list(scope.get("in_scope"))
        out_of_scope = set(self._id_list(scope.get("out_of_scope")))
        if not in_scope:
            unresolved.append("scope:in_scope")

        raw_spec_blocks = packet.get("spec_blocks")
        spec_blocks: list[Any] = raw_spec_blocks if isinstance(raw_spec_blocks, list) else []
        specs = self._reference_index(spec_blocks, unresolved)
        raw_tests = packet.get("test_refs")
        tests_raw: list[Any] = raw_tests if isinstance(raw_tests, list) else []
        tests = self._reference_index(tests_raw, unresolved)

        for ref_id in in_scope:
            if ref_id in out_of_scope or ref_id not in specs:
                unresolved.append(ref_id)
                continue
            if str(specs[ref_id].get("status", "")).lower() not in _ACTIVE_STATES:
                unresolved.append(ref_id)
            if not specs[ref_id].get("digest"):
                unresolved.append(ref_id)

        trace_specs = self._trace_ids(trace.get("spec"))
        if not trace_specs:
            unresolved.append("ch:trace:spec")
        for ref_id in trace_specs:
            if ref_id not in in_scope or ref_id in out_of_scope or ref_id not in specs:
                unresolved.append(ref_id)

        trace_tests = self._trace_ids(trace.get("test"))
        if tests and not trace_tests:
            unresolved.append("ch:trace:test")
        for ref_id in trace_tests:
            if ref_id in out_of_scope or ref_id not in tests:
                unresolved.append(ref_id)

        for ref_id, ref in tests.items():
            if str(ref.get("status", "")).lower() not in _ACTIVE_STATES:
                unresolved.append(ref_id)
            if not ref.get("digest"):
                unresolved.append(ref_id)
            if not self._valid_test_ref(ref):
                unresolved.append(ref_id)

        if not isinstance(packet.get("digests"), dict) or not packet.get("digests"):
            unresolved.append("packet:digests")
        return list(dict.fromkeys(unresolved))

    @staticmethod
    def _id_list(value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        ids: list[str] = []
        for item in value:
            if isinstance(item, str):
                ids.append(item)
            elif isinstance(item, dict) and item.get("id"):
                ids.append(str(item["id"]))
        return ids

    @staticmethod
    def _trace_ids(value: Any) -> list[str]:
        if isinstance(value, str):
            return [value]
        if isinstance(value, list):
            return [str(item) for item in value if item]
        return []

    @staticmethod
    def _reference_index(references: list[Any], unresolved: list[str]) -> dict[str, dict[str, Any]]:
        indexed: dict[str, dict[str, Any]] = {}
        for item in references:
            if not isinstance(item, dict) or not item.get("id"):
                unresolved.append("packet:reference:id")
                continue
            ref_id = str(item["id"])
            if ref_id in indexed:
                unresolved.append(ref_id)
                continue
            indexed[ref_id] = item
        return indexed

    def _valid_test_ref(self, ref: dict[str, Any]) -> bool:
        ref_id = str(ref.get("id", ""))
        path = str(ref.get("path", ""))
        selector = str(ref.get("selector", ""))
        runner = ref.get("runner")
        valid_runner = runner == "pytest" or (
            isinstance(runner, list) and len(runner) >= 2 and runner[-2:] == ["-m", "pytest"]
        )
        if not valid_runner:
            return False
        if not _SAFE_ID.fullmatch(ref_id) or not selector or not _SAFE_SELECTOR.fullmatch(selector):
            return False
        if not path or not self._valid_tracked_path(path):
            return False
        for field in ("migration", "fixture"):
            linked_path = ref.get(field)
            if linked_path and not self._valid_tracked_path(str(linked_path)):
                return False
        return True

    def _valid_tracked_path(self, path: str) -> bool:
        if not path or os.path.isabs(path) or ".." in path.split(os.sep):
            return False
        absolute = os.path.realpath(os.path.join(self.workdir, path))
        if os.path.commonpath([self.workdir, absolute]) != self.workdir or not os.path.isfile(
            absolute
        ):
            return False
        tracked = subprocess.run(
            ["git", "ls-files", "--error-unmatch", "--", path],
            cwd=self.workdir,
            capture_output=True,
            text=True,
            check=False,
        )
        return tracked.returncode == 0

    def _ref_paths_match_head(self, ref: dict[str, Any]) -> bool:
        paths = [str(ref["path"])]
        paths.extend(str(ref[field]) for field in ("migration", "fixture") if ref.get(field))
        result = subprocess.run(
            ["git", "diff", "--quiet", "HEAD", "--", *paths],
            cwd=self.workdir,
            capture_output=True,
            text=True,
            check=False,
        )
        return result.returncode == 0

    def _pytest_harness_argv(self, target: str) -> list[str]:
        config = load_raw_config(self.workdir)
        raw_guards = config.get("guards")
        guards: dict[str, Any] = raw_guards if isinstance(raw_guards, dict) else {}
        command = str(guards.get("test_command") or f"{sys.executable} -m pytest")
        try:
            words = shlex.split(command)
        except ValueError as exc:
            raise PacketValidationError(["guards:test_command"]) from exc

        segments: list[list[str]] = [[]]
        for token in words:
            if token in {"&&", ";", "||"}:
                segments.append([])
            else:
                segments[-1].append(token)
        pytest_segments = [segment for segment in segments if self._is_pytest_argv(segment)]
        if len(pytest_segments) != 1:
            raise PacketValidationError(["guards:test_command:pytest"])
        return [*pytest_segments[0], target]

    @staticmethod
    def _is_pytest_argv(argv: list[str]) -> bool:
        if not argv:
            return False
        names = [os.path.basename(token) for token in argv]
        if any(name == "pytest" for name in names):
            return True
        return any(argv[index : index + 2] == ["-m", "pytest"] for index in range(len(argv) - 1))

    def _test_timeout(self) -> float:
        config = load_raw_config(self.workdir)
        raw_guards = config.get("guards")
        guards: dict[str, Any] = raw_guards if isinstance(raw_guards, dict) else {}
        value = guards.get("test_timeout", 120)
        try:
            timeout = float(value)
        except (TypeError, ValueError):
            timeout = 120.0
        return timeout if timeout > 0 else 120.0

    @staticmethod
    def _test_env() -> dict[str, str]:
        return {
            key: value
            for key, value in os.environ.items()
            if key not in {"PYTEST_ADDOPTS", "GIT_INDEX_FILE"}
        }

    def _evaluated_commit(self) -> str:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=self.workdir,
            capture_output=True,
            text=True,
            check=False,
        )
        commit = result.stdout.strip()
        if result.returncode != 0 or not re.fullmatch(r"[0-9a-f]{40,64}", commit):
            raise PacketValidationError(["commit:HEAD"])
        return commit
