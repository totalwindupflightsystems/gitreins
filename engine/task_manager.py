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

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import yaml


@dataclass
class Task:
    id: str
    title: str
    criteria: list[str] = field(default_factory=list)
    status: str = "pending"  # pending | in_progress | complete
    created_at: str = ""
    completed_at: str | None = None


class TaskManager:
    """Manage tasks stored in .gitreins/tasks.yaml."""

    def __init__(self, workdir: str = "."):
        self.workdir = os.path.abspath(workdir)
        self._config_dir = os.path.join(self.workdir, ".gitreins")
        self._tasks_file = os.path.join(self._config_dir, "tasks.yaml")
        self._tasks: dict[str, Task] = {}
        self._load()

    def _load(self) -> None:
        """Load tasks from YAML file."""
        if not os.path.exists(self._tasks_file):
            return
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
                )
                self._tasks[task.id] = task
        except Exception as e:
            print(f"Warning: failed to load tasks: {e}")

    def _save(self) -> None:
        """Save tasks to YAML file."""
        os.makedirs(self._config_dir, exist_ok=True)
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
            tasks_list.append(entry)
        with open(self._tasks_file, "w") as f:
            yaml.dump({"tasks": tasks_list}, f, default_flow_style=False, sort_keys=False)

    def create(self, id: str, title: str, criteria: list[str]) -> Task:
        """Create a new task."""
        now = datetime.now(timezone.utc).isoformat()
        task = Task(
            id=id,
            title=title,
            criteria=criteria,
            status="pending",
            created_at=now,
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

    def complete(self, id: str) -> Task:
        """Mark a task as complete."""
        task = self._tasks.get(id)
        if not task:
            raise KeyError(f"Task not found: {id}")
        task.status = "complete"
        task.completed_at = datetime.now(timezone.utc).isoformat()
        self._save()
        return task

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
        return {
            "id": task.id,
            "title": task.title,
            "criteria": task.criteria,
            "status": task.status,
            "created_at": task.created_at,
            "completed_at": task.completed_at,
        }
