"""
Task Manager — YAML-backed task lifecycle.

Tasks are stored in .gitreins/tasks.yaml inside the repo.
Format:

tasks:
  - id: "login-endpoint"
    title: "Implement POST /login endpoint"
    criteria:
      - "Accepts email+password as JSON body"
      - "Returns JWT token on success"
      - "Returns 401 on invalid credentials"
      - "Has tests for happy path and error cases"
    status: pending  # pending | in_progress | complete
"""

import hashlib
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import yaml


class DependencyError(Exception):
    """Raised when a task cannot complete because its dependencies are not met."""

    pass


class TaskStateCorruptError(RuntimeError):
    """Raised instead of overwriting a task-state file that could not be read.

    QA-GITREINS-POC-6: a truncated/unparseable ``.gitreins/tasks.yaml`` used to
    print ``Warning: failed to load tasks: ...`` and then let the next write
    replace the file with an empty-but-valid one — the corrupted bytes (which
    are often a partially recoverable audit trail) were destroyed with no copy
    anywhere. The manager now copies the unreadable file aside on load and
    refuses to write when even that copy could not be made.
    """

    pass


CORRUPT_STATE_SUFFIX = ".corrupt-"


@dataclass
class Task:
    id: str
    title: str
    criteria: list[str] = field(default_factory=list)
    status: str = "pending"  # pending | in_progress | complete
    created_at: str = ""
    completed_at: str | None = None
    depends_on: list[str] = field(default_factory=list)  # task IDs that must complete first


class TaskManager:
    """Manage tasks stored in .gitreins/tasks.yaml."""

    def __init__(self, workdir: str = "."):
        self.workdir = os.path.abspath(workdir)
        self._config_dir = os.path.join(self.workdir, ".gitreins")
        self._tasks_file = os.path.join(self._config_dir, "tasks.yaml")
        self._tasks: dict[str, Task] = {}
        self._load_error: str | None = None
        self._preserved_state: str | None = None
        self._load()

    def _load(self) -> None:
        """Load tasks from YAML file."""
        if not os.path.exists(self._tasks_file):
            return
        # DF-GITREINS-POC-22: a structurally truncated store (crash mid-write,
        # full disk, kill -9 during _save) can still parse as a *smaller but
        # valid* YAML doc — silently serving a fraction of the task list with
        # rc=0. yaml.dump always terminates the document with a newline, so a
        # store that does NOT end in one was cut short: say so loudly and
        # preserve the raw bytes (same mechanism as POC-6) while still serving
        # whatever parsed, rather than pretending the list is whole.
        try:
            with open(self._tasks_file, "rb") as f:
                f.seek(-1, os.SEEK_END)
                if f.read(1) != b"\n":
                    self._load_error = "file appears truncated (no document-terminating newline)"
                    print(
                        f"Warning: {self._tasks_file} looks truncated — the task list "
                        "below may be PARTIAL; a preserved copy is being made",
                        file=sys.stderr,
                    )
                    self._preserve_unreadable_state()
        except OSError:
            # Unreadable at all: the parse below will report the real failure;
            # stay silent here so one broken file produces one clean line.
            pass
        try:
            with open(self._tasks_file, "r") as f:
                data = yaml.safe_load(f) or {}
            for item in data.get("tasks", []):
                task = Task(
                    id=item["id"],
                    title=item.get("title", ""),
                    criteria=item.get("criteria", []),
                    status=item.get("status", "pending"),
                    created_at=item.get("created_at", ""),
                    completed_at=item.get("completed_at"),
                    depends_on=item.get("depends_on", []),
                )
                self._tasks[task.id] = task
        except Exception as e:
            # QA-GITREINS-POC-6: the file is unreadable, so the loaded task set is
            # incomplete by definition. Preserve the raw bytes NOW — the next write
            # must not be able to destroy them — and say so loudly.
            self._load_error = str(e)
            print(f"Warning: failed to load tasks: {e}", file=sys.stderr)
            self._preserve_unreadable_state()

    def _preserve_unreadable_state(self) -> str | None:
        """Copy the unreadable task state aside; return the sidecar path (None on failure).

        The sidecar name carries the content hash, so repeated loads of the same
        broken file are idempotent (no sidecar churn) while a second distinct
        corruption gets its own preserved copy.
        """
        if self._preserved_state and os.path.exists(self._preserved_state):
            return self._preserved_state
        try:
            with open(self._tasks_file, "rb") as f:
                raw = f.read()
        except OSError as exc:
            print(f"Warning: cannot read {self._tasks_file} to preserve it: {exc}", file=sys.stderr)
            return None
        dest = self._tasks_file + CORRUPT_STATE_SUFFIX + hashlib.sha256(raw).hexdigest()[:12]
        if os.path.exists(dest):
            self._preserved_state = dest
            return dest
        try:
            with open(dest, "wb") as f:
                f.write(raw)
        except OSError as exc:
            print(
                f"Warning: cannot preserve the unreadable task state as {dest}: {exc}",
                file=sys.stderr,
            )
            return None
        self._preserved_state = dest
        print(
            f"Warning: preserved the unreadable task state as {dest} "
            "— copy it back to recover the tasks it still holds",
            file=sys.stderr,
        )
        return dest

    def _save(self) -> None:
        """Save tasks to YAML file."""
        os.makedirs(self._config_dir, exist_ok=True)
        if self._load_error is not None and os.path.exists(self._tasks_file):
            # Never replace state we could not read without a preserved copy.
            if self._preserve_unreadable_state() is None:
                raise TaskStateCorruptError(
                    f"{self._tasks_file} could not be read ({self._load_error}) and a copy "
                    "could not be preserved — refusing to overwrite it"
                )
            self._load_error = None
        tasks_list = []
        for task in self._tasks.values():
            entry: dict[str, Any] = {
                "id": task.id,
                "title": task.title,
                "criteria": task.criteria,
                "status": task.status,
                "created_at": task.created_at,
            }
            if task.completed_at:
                entry["completed_at"] = task.completed_at
            if task.depends_on:
                entry["depends_on"] = task.depends_on
            tasks_list.append(entry)
        with open(self._tasks_file, "w") as f:
            yaml.dump({"tasks": tasks_list}, f, default_flow_style=False, sort_keys=False)

    def create(
        self, id: str, title: str, criteria: list[str], depends_on: list[str] | None = None
    ) -> Task:
        """Create a new task. Optional depends_on lists task IDs that must complete first."""
        now = datetime.now(timezone.utc).isoformat()
        task = Task(
            id=id,
            title=title,
            criteria=criteria,
            status="pending",
            created_at=now,
            depends_on=depends_on or [],
        )
        self._tasks[id] = task
        self._save()
        return task

    def start(self, id: str) -> Task:
        """Mark a task as in progress."""
        task = self._tasks.get(id)
        if not task:
            raise KeyError(f"Task not found: {id}")
        task.status = "in_progress"
        self._save()
        return task

    def complete(self, id: str, force: bool = False) -> Task:
        """Mark a task as complete."""
        task = self._tasks.get(id)
        if not task:
            raise KeyError(f"Task not found: {id}")

        # Check dependencies (skip if forced)
        if not force:
            blocked = self.check_dependencies(id)
            if blocked:
                raise DependencyError(
                    f"Task '{id}' depends on incomplete tasks: {', '.join(blocked)}. "
                    f"Complete those first or use --force to skip."
                )

        task.status = "complete"
        task.completed_at = datetime.now(timezone.utc).isoformat()
        self._save()
        return task

    def check_dependencies(self, id: str) -> list[str]:
        """Return list of dependency task IDs that are not yet complete."""
        task = self._tasks.get(id)
        if not task:
            return []
        blocked = []
        for dep_id in task.depends_on:
            dep = self._tasks.get(dep_id)
            if not dep or dep.status != "complete":
                blocked.append(dep_id)
        return blocked

    def get(self, id: str) -> Task | None:
        """Get a task by ID."""
        return self._tasks.get(id)

    def list_tasks(self, status: str | None = None) -> list["Task"]:
        """List tasks, optionally filtered by status."""
        tasks = list(self._tasks.values())
        if status:
            tasks = [t for t in tasks if t.status == status]
        return tasks

    def all_tasks(self) -> list["Task"]:
        """Return all tasks."""
        return list(self._tasks.values())

    def delete(self, id: str) -> None:
        """Delete a task by ID."""
        if id not in self._tasks:
            raise KeyError(f"Task not found: {id}")
        del self._tasks[id]
        self._save()

    def to_dict(self, task: Task) -> dict:
        """Convert a Task to a plain dict (for MCP/serialization)."""
        result = {
            "id": task.id,
            "title": task.title,
            "criteria": task.criteria,
            "status": task.status,
            "created_at": task.created_at,
            "completed_at": task.completed_at,
        }
        if task.depends_on:
            result["depends_on"] = task.depends_on
        return result
