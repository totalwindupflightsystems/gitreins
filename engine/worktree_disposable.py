"""Disposable Git worktrees for hermetic QA, dogfood, and repro runs.

Disposable trees are detached from a named branch and live under a separate
``.disposable`` directory, so a verification run cannot collide with a task
worktree or alter the canonical checkout.  Registry writes are atomic and
locked because a repro farm may create and reap several trees concurrently.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from engine.config import load_defaults
from engine.worktree_manager import WorktreeError, WorktreeManager, _git

DISPOSABLE_FILE = "disposable.json"
DISPOSABLE_LOCK = "disposable.lock"
MAX_EVIDENCE_CHARS = 4000


@dataclass
class DisposableRecord:
    """Durable metadata for one disposable worktree run."""

    run_id: str
    path: str
    created_at: float
    command: str
    pid: int | None = None
    keep: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "path": self.path,
            "created_at": self.created_at,
            "command": self.command,
            "pid": self.pid,
            "keep": self.keep,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DisposableRecord":
        return cls(
            run_id=str(data["run_id"]),
            path=str(data["path"]),
            created_at=float(data["created_at"]),
            command=str(data.get("command", "")),
            pid=data.get("pid"),
            keep=bool(data.get("keep", False)),
        )


def _evidence(output: str) -> str:
    """Bound command evidence to the same limit used by the worktree fleet."""
    output = output.strip()
    if len(output) <= MAX_EVIDENCE_CHARS:
        return output
    return output[: MAX_EVIDENCE_CHARS - 40] + "\n… [output truncated]"


def _tree_size(path: Path) -> int:
    """Return recursive regular-file bytes, ignoring symlinks and Git objects."""
    total = 0
    if not path.is_dir():
        return 0
    try:
        entries = list(os.scandir(path))
    except OSError:
        return 0
    for entry in entries:
        if entry.is_symlink() or entry.name == ".git":
            continue
        try:
            if entry.is_file(follow_symlinks=False):
                total += entry.stat(follow_symlinks=False).st_size
            elif entry.is_dir(follow_symlinks=False):
                total += _tree_size(Path(entry.path))
        except OSError:
            continue
    return total


def _pid_alive(pid: int | None) -> bool:
    """Return whether a valid non-init process appears to be alive."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 1:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _coerce_ceiling_mb(value: Any) -> int:
    """Validate the disk ceiling while allowing zero/negative unlimited values."""
    if isinstance(value, bool):
        raise ValueError("disk_ceiling_mb must be an integer, not boolean")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    raise ValueError(f"disk_ceiling_mb must be an integer, got {value!r}")


def _ceiling_bytes(ceiling_mb: int) -> int | None:
    return None if ceiling_mb <= 0 else ceiling_mb * 1024 * 1024


def enforce_disk_ceiling(
    manager: WorktreeManager,
    *,
    requested_path: Path,
    requested_bytes: int | None = None,
) -> None:
    """Reap oldest managed trees until a new tree fits the configured ceiling.

    Disposable runs are preferred for reaping.  Completed/merged task trees
    are next, while live task trees and fresh heartbeats are protected.  The
    caller holds the manager's registry lock when this is invoked.
    """
    ceiling_mb = _coerce_ceiling_mb(manager.worktree_disk_ceiling_mb)
    ceiling = _ceiling_bytes(ceiling_mb)
    if ceiling is None:
        return

    from engine.worktree_manager import LIVE_STATES, STALE_AFTER_SECONDS

    task_records = manager._load_registry()
    disposable_path = manager.main_root / ".gitreins" / DISPOSABLE_FILE
    disposable_records = _load_disposable_file(disposable_path)
    current = 0
    for record in task_records.values():
        current += _tree_size(Path(record.path))
    for record in disposable_records:
        current += _tree_size(Path(record.path))
    projected = _tree_size(manager.main_root) if requested_bytes is None else requested_bytes
    projected = max(0, projected)

    if current + projected <= ceiling:
        return

    # Disposable trees are ordered independently from task trees as required
    # by the feature: a retained failure is inspectable until the next cap hit.
    candidates: list[tuple[float, str, str, Any]] = []
    for record in disposable_records:
        if _pid_alive(record.pid):
            continue
        candidates.append((record.created_at, "disposable", record.run_id, record))
    for task_id, record in task_records.items():
        if record.state in LIVE_STATES:
            continue
        if record.state not in {"merged", "completed"}:
            continue
        if manager._heartbeat_age(record) <= STALE_AFTER_SECONDS:
            continue
        candidates.append((record.created_at, "task", task_id, record))
    candidates.sort(key=lambda item: (0 if item[1] == "disposable" else 1, item[0], item[2]))

    reaped: list[str] = []
    for _created_at, kind, identifier, record in candidates:
        if current + projected <= ceiling:
            break
        path = Path(record.path)
        size = _tree_size(path)
        if kind == "disposable":
            _remove_disposable_tree(manager.main_root, path)
            disposable_records = [item for item in disposable_records if item.run_id != identifier]
        else:
            branches_deleted: list[str] = []
            manager._remove_tree(record, branches_deleted)
            task_records.pop(identifier, None)
        current -= size
        reaped.append(identifier)

    if reaped:
        manager._save_registry(task_records)
        _save_disposable_file(disposable_path, disposable_records)
    if current + projected > ceiling:
        raise WorktreeError(
            f"worktree disk ceiling {ceiling_mb} MB cannot be satisfied: "
            f"current usage is {current / (1024 * 1024):.2f} MB and the new tree "
            f"would require about {projected / (1024 * 1024):.2f} MB"
        )


def _load_disposable_file(path: Path) -> list[DisposableRecord]:
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return []
    records: list[DisposableRecord] = []
    for item in data.get("runs", []) if isinstance(data, dict) else []:
        try:
            records.append(DisposableRecord.from_dict(item))
        except (KeyError, TypeError, ValueError):
            continue
    return records


def _save_disposable_file(path: Path, records: list[DisposableRecord]) -> None:
    """Atomically save the main-checkout disposable registry."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        json.dumps({"version": 1, "runs": [record.to_dict() for record in records]}, indent=2)
        + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, path)


def _remove_disposable_tree(main_root: Path, path: Path) -> None:
    """Reap a disposable tree and prune Git metadata, failing on Git errors."""
    result = _git(main_root, "worktree", "remove", "--force", str(path), check=False)
    if result.returncode != 0 and path.exists():
        detail = result.stderr.strip() or result.stdout.strip() or "no diagnostic output"
        raise WorktreeError(f"could not reap disposable worktree {path}: {detail}")
    prune = _git(main_root, "worktree", "prune", check=False)
    if prune.returncode != 0:
        detail = prune.stderr.strip() or prune.stdout.strip() or "no diagnostic output"
        raise WorktreeError(f"could not prune disposable worktree metadata: {detail}")


class DisposableWorktreeManager:
    """Create, execute, and reap detached verification worktrees."""

    def __init__(self, workdir: str | os.PathLike[str] | None = None):
        self.manager = WorktreeManager(workdir)
        self.main_root = self.manager.main_root
        self.registry_path = self.main_root / ".gitreins" / DISPOSABLE_FILE
        self.lock_path = self.main_root / ".gitreins" / DISPOSABLE_LOCK
        self._lock_state = threading.local()
        defaults = load_defaults(str(self.main_root))
        self.disk_ceiling_mb = _coerce_ceiling_mb(defaults.worktree_disk_ceiling_mb)
        self.manager.worktree_disk_ceiling_mb = self.disk_ceiling_mb

    @contextmanager
    def _exclusive_lock(self):
        """Serialize disposable registry changes across processes and threads."""
        depth = getattr(self._lock_state, "depth", 0)
        if depth:
            self._lock_state.depth = depth + 1
            try:
                yield
            finally:
                self._lock_state.depth = depth
            return
        import fcntl

        self.registry_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.lock_path, "a+", encoding="utf-8") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            self._lock_state.depth = 1
            try:
                yield
            finally:
                self._lock_state.depth = 0
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def _load(self) -> list[DisposableRecord]:
        return _load_disposable_file(self.registry_path)

    def _save(self, records: list[DisposableRecord]) -> None:
        _save_disposable_file(self.registry_path, records)

    def _tree_path(self, run_id: str) -> Path:
        return self.main_root.parent / f"{self.main_root.name}-wt" / ".disposable" / run_id

    def create(
        self, command: str, *, run_id: str | None = None, keep: bool = False
    ) -> DisposableRecord:
        """Create one detached tree at the current main ``HEAD``."""
        run_id = run_id or f"run-{uuid.uuid4().hex}"
        if not run_id or Path(run_id).name != run_id or run_id in {".", ".."}:
            raise WorktreeError(f"invalid disposable run id: {run_id!r}")
        path = self._tree_path(run_id)
        with self._exclusive_lock():
            records = self._load()
            if any(record.path == str(path) for record in records):
                raise WorktreeError(f"disposable run {run_id!r} is already registered")
            if path.exists() and any(path.iterdir()):
                raise WorktreeError(
                    f"disposable worktree path already exists and is not empty: {path}"
                )
            enforce_disk_ceiling(self.manager, requested_path=path)
            head = _git(self.main_root, "rev-parse", "--verify", "HEAD").stdout.strip()
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                _git(self.main_root, "worktree", "add", "--detach", str(path), head)
                self.manager._link_venv(path)
            except Exception:
                _remove_disposable_tree(self.main_root, path)
                raise
            record = DisposableRecord(
                run_id=run_id,
                path=str(path),
                created_at=time.time(),
                command=command,
                keep=keep,
            )
            records.append(record)
            self._save(records)
            return record

    def _update(self, record: DisposableRecord) -> None:
        with self._exclusive_lock():
            records = self._load()
            for index, existing in enumerate(records):
                if existing.run_id == record.run_id:
                    records[index] = record
                    break
            self._save(records)

    def reap(self, *, run_id: str | None = None) -> list[str]:
        """Reap disposable records, optionally selecting one run id."""
        with self._exclusive_lock():
            records = self._load()
            selected = [record for record in records if run_id is None or record.run_id == run_id]
            if run_id is not None and not selected:
                return []
            removed: list[str] = []
            remaining: list[DisposableRecord] = []
            for record in records:
                if record not in selected:
                    remaining.append(record)
                    continue
                if _pid_alive(record.pid):
                    remaining.append(record)
                    continue
                _remove_disposable_tree(self.main_root, Path(record.path))
                removed.append(record.run_id)
            self._save(remaining)
            return removed

    def run(
        self,
        command: str,
        *,
        timeout: float | None = None,
        keep: bool = False,
        run_id: str | None = None,
        index: int | None = None,
    ) -> dict[str, Any]:
        """Run ``command`` through ``sh -c`` and reap it in ``finally``."""
        if not isinstance(command, str) or not command.strip():
            raise WorktreeError("disposable command must be a non-empty shell command string")
        if timeout is not None and (isinstance(timeout, bool) or timeout <= 0):
            raise WorktreeError("disposable timeout must be positive")
        started_at = time.time()
        record = self.create(command, run_id=run_id, keep=keep)
        process: subprocess.Popen[str] | None = None
        exit_code = -1
        output = ""
        started = time.monotonic()
        try:
            try:
                process = subprocess.Popen(
                    ["sh", "-c", command],
                    cwd=record.path,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    start_new_session=True,
                )
            except OSError as exc:
                output = str(exc)
            else:
                record.pid = process.pid
                self._update(record)
                try:
                    timed_out = False
                    while True:
                        try:
                            process.wait(timeout=0.25)
                            break
                        except subprocess.TimeoutExpired:
                            if timeout is not None and time.monotonic() - started > timeout:
                                self._terminate(process)
                                output = process.communicate()[0] or ""
                                output += f"\ncommand timed out after {timeout}s"
                                timed_out = True
                                break
                    if process.returncode is not None:
                        exit_code = -1 if timed_out else process.returncode
                    if not timed_out:
                        output = process.communicate()[0] or ""
                except Exception:
                    self._terminate(process)
                    process.communicate()
                    raise
        finally:
            record.pid = None
            if not keep:
                _remove_disposable_tree(self.main_root, Path(record.path))
                with self._exclusive_lock():
                    self._save([item for item in self._load() if item.run_id != record.run_id])
            else:
                self._update(record)
        finished_at = time.time()
        result: dict[str, Any] = {
            "index": index,
            "run_id": record.run_id,
            "exit_code": exit_code,
            "duration_s": round(finished_at - started_at, 6),
            "tree": record.path,
            "kept": keep,
            "started_at": started_at,
            "finished_at": finished_at,
            "output": _evidence(output),
        }
        return {key: value for key, value in result.items() if value is not None}

    def dogfood(
        self,
        *,
        keep: bool = False,
        skip_judge: bool = False,
        test_command: str | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Exercise the CLI lifecycle in one throwaway tree.

        The child uses the source checkout's ``gitreins/cli.py`` entry point
        with this interpreter, rather than relying on a console script or a
        ``gitreins.__main__`` module that may not exist in a source checkout.
        """
        run_id = f"dogfood-{uuid.uuid4().hex}"
        record = self.create("dogfood", run_id=run_id, keep=keep)
        started_at = time.time()
        steps: list[dict[str, Any]] = []
        task_id = f"DOGFOOD-{uuid.uuid4().hex[:12]}"
        failed = False

        def cli_step(name: str, arguments: list[str]) -> dict[str, Any]:
            step_started = time.time()
            try:
                # The source checkout has no gitreins.__main__; invoke its
                # own CLI script with this interpreter from the tree's CWD.
                cli_entry = Path(__file__).resolve().parents[1] / "gitreins" / "cli.py"
                result = subprocess.run(
                    [sys.executable, os.fspath(cli_entry), *arguments],
                    cwd=record.path,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    check=False,
                )
                output = _evidence((result.stdout or "") + (result.stderr or ""))
                return {
                    "name": name,
                    "status": "passed" if result.returncode == 0 else "failed",
                    "exit_code": result.returncode,
                    "duration_s": round(time.time() - step_started, 6),
                    "output": output,
                }
            except subprocess.TimeoutExpired as exc:
                return {
                    "name": name,
                    "status": "failed",
                    "exit_code": -1,
                    "duration_s": round(time.time() - step_started, 6),
                    "output": _evidence(f"command timed out after {timeout}s: {exc}"),
                }

        try:
            step = cli_step("init", ["init"])
            steps.append(step)
            if step["status"] != "passed":
                failed = True

            if not failed:
                step = cli_step(
                    "task",
                    [
                        "task",
                        "create",
                        task_id,
                        "Disposable dogfood task",
                        "CLI lifecycle completes in a throwaway tree",
                    ],
                )
                steps.append(step)
                if step["status"] == "passed":
                    step = cli_step("task-start", ["task", "start", task_id])
                    steps[-1]["output"] = _evidence(steps[-1]["output"] + "\n" + step["output"])
                    steps[-1]["duration_s"] = round(steps[-1]["duration_s"] + step["duration_s"], 6)
                    steps[-1]["exit_code"] = step["exit_code"]
                    steps[-1]["status"] = step["status"]
                if steps[-1]["status"] != "passed":
                    failed = True

            if not failed and test_command is not None:
                config_path = Path(record.path) / ".gitreins" / "config.yaml"
                try:
                    import yaml

                    config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
                    config.setdefault("guards", {})["test_command"] = test_command
                    config_path.write_text(
                        yaml.safe_dump(config, sort_keys=False), encoding="utf-8"
                    )
                except (OSError, ValueError, TypeError) as exc:
                    raise WorktreeError(f"could not apply dogfood test command: {exc}") from exc

            if not failed:
                step = cli_step("guard", ["guard"])
                steps.append(step)
                if step["status"] != "passed":
                    failed = True

            key_names = (
                "GITREINS_LLM_API_KEY",
                "NEURALWATT_API_KEY",
                "OPENAI_API_KEY",
                "ANTHROPIC_API_KEY",
                "DEEPSEEK_API_KEY",
                "KIMI_API_KEY",
                "GROQ_API_KEY",
                "OPENROUTER_API_KEY",
            )
            has_llm = any(os.environ.get(name) for name in key_names)
            if skip_judge:
                judge = {
                    "status": "skipped",
                    "reason": "--skip-judge",
                    "exit_code": None,
                    "duration_s": 0.0,
                }
            elif not has_llm:
                judge = {
                    "status": "skipped",
                    "reason": "no LLM configured",
                    "exit_code": None,
                    "duration_s": 0.0,
                }
            elif failed:
                judge = {
                    "status": "skipped",
                    "reason": "previous dogfood step failed",
                    "exit_code": None,
                    "duration_s": 0.0,
                }
            else:
                step = cli_step("judge", ["task", "complete", task_id])
                steps.append(step)
                judge = {
                    "status": "passed" if step["status"] == "passed" else "failed",
                    "exit_code": step["exit_code"],
                    "duration_s": step["duration_s"],
                }
                if step["status"] != "passed":
                    failed = True
            if not any(step.get("name") == "judge" for step in steps):
                steps.append({"name": "judge", **judge})
        finally:
            record.pid = None
            if not keep:
                _remove_disposable_tree(self.main_root, Path(record.path))
                with self._exclusive_lock():
                    self._save([item for item in self._load() if item.run_id != record.run_id])
            else:
                self._update(record)

        return {
            "command": "gitreins dogfood",
            "tree": record.path,
            "kept": keep,
            "steps": steps,
            "judge": judge,
            "exit_code": 1 if failed else 0,
            "started_at": started_at,
            "finished_at": time.time(),
        }

    @staticmethod
    def _terminate(process: subprocess.Popen[str]) -> None:
        """Kill a command process group after validating its PID."""
        pid = process.pid
        if isinstance(pid, int) and not isinstance(pid, bool) and pid > 1:
            try:
                os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except OSError:
                process.kill()
        else:
            process.kill()
        process.wait()

    def repro(
        self,
        command: str,
        k: int,
        *,
        concurrency: int | None = None,
        timeout: float | None = None,
        keep_failures: bool = False,
    ) -> dict[str, Any]:
        """Run ``k`` independent copies of a command from one captured HEAD."""
        if isinstance(k, bool) or not isinstance(k, int) or k < 1:
            raise WorktreeError("repro -k must be a positive integer")
        if concurrency is None:
            concurrency = load_defaults(str(self.main_root)).max_concurrent_worktrees
        if isinstance(concurrency, bool) or not isinstance(concurrency, int) or concurrency < 1:
            raise WorktreeError("repro concurrency must be a positive integer")
        head = _git(self.main_root, "rev-parse", "--verify", "HEAD").stdout.strip()
        started_at = time.time()
        results: list[dict[str, Any]] = []

        def one(index: int) -> dict[str, Any]:
            # Each run captures the same canonical HEAD immediately before
            # creation; the main checkout is not mutated by this command.
            result = self.run(command, timeout=timeout, keep=keep_failures, index=index)
            if result["exit_code"] == 0 and keep_failures:
                result["kept"] = False
            return result

        with ThreadPoolExecutor(
            max_workers=min(concurrency, k), thread_name_prefix="gitreins-repro"
        ) as pool:
            futures: dict[Future[dict[str, Any]], int] = {
                pool.submit(one, index): index for index in range(1, k + 1)
            }
            for future in as_completed(futures):
                results.append(future.result())

        results.sort(key=lambda item: item["index"])
        if keep_failures:
            # Successful runs were created with keep=True so their finally
            # block retained them; reap those now and leave failures inspectable.
            for result in results:
                if result["exit_code"] == 0:
                    result["kept"] = False
                    self.reap(run_id=result["run_id"])
            for result in results:
                if result["exit_code"] != 0:
                    result["kept"] = True
        passes = sum(result["exit_code"] == 0 for result in results)
        failures = k - passes
        return {
            "command": command,
            "k": k,
            "concurrency": concurrency,
            "head": head,
            "passes": passes,
            "failures": failures,
            "pass_rate": passes / k,
            "runs": [
                {
                    "index": result["index"],
                    "exit_code": result["exit_code"],
                    "duration_s": result["duration_s"],
                    "tree": result["tree"],
                    "kept": result["kept"],
                }
                for result in results
            ],
            "started_at": started_at,
            "finished_at": time.time(),
        }


WorktreeDisposable = DisposableWorktreeManager
DisposableWorktree = DisposableWorktreeManager
