"""Task-owned Git worktree lifecycle with a durable, reconciled registry.

One command turns a task into an isolated workspace: ``git worktree add``
creates ``../<repo>-wt/<task-id>`` on branch ``gitreins/task/<task-id>``, and
the creation is recorded in the main checkout's ``.gitreins/worktrees.json``
so the mapping survives process restarts.  Every lifecycle entry point
reconciles the registry against real ``git worktree`` metadata before acting:
work is never destroyed silently — merged trees are the only thing removal
reaps without an explicit confirmation flag.

State semantics (deterministic, derived — never stored as truth):

- ``running``  — the registered tree exists and its branch still has commits
                 not reachable from the main checkout's HEAD.
- ``merged``   — the branch tip is fully merged into HEAD (an idle tree at the
                 branch point counts as merged).
- ``guarding`` / ``judging`` — recorded in the registry when a guard/judge run
                 touches the entry (via :meth:`mark_phase`); reconcile keeps
                 them while the tree exists and the branch is unmerged.
- ``stale``    — tree exists, branch unmerged, and no heartbeat for longer
                 than ``STALE_AFTER_SECONDS`` (default 24h).
- ``orphan``   — the recorded tree is missing from git's worktree metadata, or
                 the tree directory no longer resolves as a worktree.

The registry file lives in the canonical main checkout's ``.gitreins/`` so
every worktree of the same repository reads and writes one truth.
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

# Task IDs become path components and branch names.  Keep the shape strict:
# letters, digits, hyphen, underscore, dot — no separators, no traversal.
TASK_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
BRANCH_PREFIX = "gitreins/task/"
WORKTREES_FILE = "worktrees.json"
STALE_AFTER_SECONDS = 24 * 60 * 60

LIVE_STATES = ("running", "guarding", "judging")
# States whose trees may only be removed with an explicit confirmation flag.
PROTECTED_STATES = ("stale", "orphan")


class WorktreeError(RuntimeError):
    """Raised when a worktree lifecycle operation cannot be completed."""


class WorktreeValidationError(WorktreeError):
    """Raised when a task id or resulting path fails validation."""


@dataclass
class WorktreeRecord:
    """Registry entry tying a task to its isolated worktree."""

    task_id: str
    path: str
    branch: str
    created_at: float
    updated_at: float
    last_heartbeat: float
    state: str = "running"
    tick: str | None = None
    brief_path: str | None = None
    main_root: str | None = None
    branch_point: str | None = None
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        data: dict = {
            "task_id": self.task_id,
            "path": self.path,
            "branch": self.branch,
            "state": self.state,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_heartbeat": self.last_heartbeat,
        }
        if self.tick is not None:
            data["tick"] = self.tick
        if self.brief_path is not None:
            data["brief_path"] = self.brief_path
        if self.main_root is not None:
            data["main_root"] = self.main_root
        if self.branch_point is not None:
            data["branch_point"] = self.branch_point
        if self.notes:
            data["notes"] = list(self.notes)
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "WorktreeRecord":
        return cls(
            task_id=str(data["task_id"]),
            path=str(data["path"]),
            branch=str(data["branch"]),
            created_at=float(data["created_at"]),
            updated_at=float(data["updated_at"]),
            last_heartbeat=float(data["last_heartbeat"]),
            state=str(data.get("state", "running")),
            tick=data.get("tick"),
            brief_path=data.get("brief_path"),
            main_root=data.get("main_root"),
            branch_point=data.get("branch_point"),
            notes=list(data.get("notes", [])),
        )


def validate_task_id(task_id: str) -> str:
    """Validate a task id usable as a path component and branch segment."""
    if not task_id or not TASK_ID_PATTERN.match(task_id):
        raise WorktreeValidationError(
            f"invalid task id {task_id!r}: must match {TASK_ID_PATTERN.pattern} "
            "(letters, digits, dot, hyphen, underscore; max 64 chars)"
        )
    if task_id in {".", ".."} or "/" in task_id:
        raise WorktreeValidationError(
            f"invalid task id {task_id!r}: path separators and dot names are not allowed"
        )
    return task_id


def _git(
    workdir: str | os.PathLike[str],
    *args: str,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run git with an argument list (never a shell) in ``workdir``."""
    command = ["git", "-C", str(workdir), *args]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=60, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise WorktreeError(f"could not run {' '.join(command)!r}: {exc}") from exc
    if check and result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "no diagnostic output"
        raise WorktreeError(f"git {' '.join(args)} failed in {workdir}: {detail}")
    return result


def _branch_for(task_id: str) -> str:
    return BRANCH_PREFIX + task_id


def _default_tree_root(main_root: Path, task_id: str) -> Path:
    repo_name = main_root.name or "repo"
    return main_root.parent / f"{repo_name}-wt" / task_id


class WorktreeManager:
    """Create, register, reconcile, and clean task-owned worktrees.

    The registry lives in the canonical main checkout's ``.gitreins/``
    directory so every linked worktree shares one mapping.  All mutations
    rewrite the file atomically (same-directory temp file + ``os.replace``).
    """

    def __init__(self, workdir: str | os.PathLike[str] | None = None, clock=time.time):
        from engine.repo_paths import resolve_worktree_paths

        self._clock = clock
        paths = resolve_worktree_paths(workdir)
        self.main_root = paths.canonical_main_root
        self._gitreins_dir = self.main_root / ".gitreins"
        self._registry_file = self._gitreins_dir / WORKTREES_FILE

    # ── registry storage ────────────────────────────────────────────

    def _load_registry(self) -> dict[str, WorktreeRecord]:
        if not self._registry_file.is_file():
            return {}
        import json

        try:
            data = json.loads(self._registry_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        records: dict[str, WorktreeRecord] = {}
        for item in data.get("worktrees", []):
            try:
                record = WorktreeRecord.from_dict(item)
            except (KeyError, TypeError, ValueError):
                continue  # tolerate a torn/partial entry rather than losing the rest
            records[record.task_id] = record
        return records

    def _save_registry(self, records: dict[str, WorktreeRecord]) -> None:
        import json

        self._gitreins_dir.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "worktrees": [r.to_dict() for r in records.values()]}
        tmp_file = self._registry_file.with_name(self._registry_file.name + ".tmp")
        tmp_file.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp_file, self._registry_file)

    # ── git facts ───────────────────────────────────────────────────

    def _git_worktrees(self) -> dict[str, dict[str, str]]:
        """Parse ``git worktree list --porcelain`` keyed by resolved path."""
        result = _git(self.main_root, "worktree", "list", "--porcelain")
        trees: dict[str, dict[str, str]] = {}
        current: dict[str, str] = {}
        for line in result.stdout.splitlines():
            if not line.strip():
                if current:
                    trees[str(Path(current["worktree"]).resolve())] = current
                current = {}
                continue
            key, _, value = line.partition(" ")
            current[key] = value
        if current:
            trees[str(Path(current["worktree"]).resolve())] = current
        return trees

    def _branch_tip(self, branch: str) -> str | None:
        result = _git(
            self.main_root,
            "rev-parse",
            "--verify",
            "--quiet",
            f"refs/heads/{branch}",
            check=False,
        )
        if result.returncode != 0:
            return None
        return result.stdout.strip() or None

    def _branch_is_merged(self, record: WorktreeRecord) -> bool:
        """True when the branch has real commits fully merged into HEAD.

        An idle tree sitting at its branch point does NOT count as merged —
        an empty branch is trivially an ancestor of HEAD, and treating that
        as merged would let ``clean`` reap a freshly created workspace.  The
        merge counts only when the branch tip differs from the recorded
        branch point and that tip is reachable from HEAD.
        """
        tip = self._branch_tip(record.branch)
        if tip is None:
            return False
        if record.branch_point and tip == record.branch_point:
            return False
        result = _git(
            self.main_root,
            "merge-base",
            "--is-ancestor",
            record.branch,
            "HEAD",
            check=False,
        )
        return result.returncode == 0

    def _heartbeat_age(self, record: WorktreeRecord, now: float | None = None) -> float:
        current = self._clock() if now is None else now
        return max(0.0, current - record.last_heartbeat)

    # ── reconcile ───────────────────────────────────────────────────

    def reconcile(
        self, records: dict[str, WorktreeRecord] | None = None
    ) -> dict[str, WorktreeRecord]:
        """Recompute each record's state from registry + git ground truth.

        Classifies entries whose tree vanished (or was never registered in
        git) as ``orphan``.  Never deletes anything — classification only.
        Returns the reconciled records; callers persist via
        :meth:`save_reconciled` when they accept the result.
        """
        if records is None:
            records = self._load_registry()
        trees = self._git_worktrees()
        now = self._clock()
        for record in records.values():
            tree_meta = trees.get(str(Path(record.path).resolve()))
            notes: list[str] = []
            if tree_meta is None:
                state = "orphan"
                notes.append("tree missing from git worktree metadata")
            elif not Path(record.path).is_dir():
                state = "orphan"
                notes.append("tree directory no longer exists")
            else:
                if self._branch_is_merged(record):
                    state = "merged"
                elif self._heartbeat_age(record, now) > STALE_AFTER_SECONDS:
                    state = "stale"
                    notes.append("no heartbeat for more than 24h")
                elif record.state in ("guarding", "judging"):
                    state = record.state
                else:
                    state = "running"
            record.state = state
            record.updated_at = now
            if notes:
                record.notes = notes
            else:
                record.notes = []
        return records

    def save_reconciled(self, records: dict[str, WorktreeRecord]) -> None:
        self._save_registry(records)

    def reconcile_and_persist(self) -> dict[str, WorktreeRecord]:
        """Boot-time equivalent: reconcile, persist, return."""
        records = self.reconcile()
        self.save_reconciled(records)
        return records

    # ── create ──────────────────────────────────────────────────────

    def create(
        self,
        task_id: str,
        *,
        brief_path: str | None = None,
        tick: str | None = None,
        reuse: bool = True,
    ) -> tuple[WorktreeRecord, bool]:
        """Create (or idempotently reuse) the task's isolated worktree.

        Returns ``(record, created)``.  Reuse only happens when registry and
        git agree — the tree exists as a registered worktree on the expected
        branch.  Any other collision (branch exists without a tree, path
        occupied by something else, another task registered at the same
        path) fails with a clear error instead of mutating anything.
        """
        validate_task_id(task_id)
        branch = _branch_for(task_id)
        tree_path = _default_tree_root(self.main_root, task_id)

        records = self.reconcile_and_persist()
        existing = records.get(task_id)

        # Path occupied by a different task's registration?
        for other_id, other in records.items():
            if other_id != task_id and str(Path(other.path).resolve()) == str(tree_path):
                raise WorktreeError(
                    f"path {tree_path} is already registered to task {other_id!r}"
                )

        if existing is not None:
            if not reuse:
                raise WorktreeError(f"task {task_id!r} already has a worktree: {existing.path}")
            return self._reuse_existing(existing, task_id, branch, brief_path, tick)

        # No registry entry — create from scratch, refusing real collisions.
        if self._branch_tip(branch) is not None:
            raise WorktreeError(
                f"branch {branch!r} already exists but is not registered to task "
                f"{task_id!r}; remove or rename it first (registry and git disagree)"
            )
        if tree_path.exists() and any(tree_path.iterdir()):
            raise WorktreeError(
                f"worktree path {tree_path} already exists and is not empty; "
                "registry and git disagree about this task"
            )

        result = _git(
            self.main_root,
            "worktree",
            "add",
            "-b",
            branch,
            str(tree_path),
        )
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or "no diagnostic output"
            raise WorktreeError(f"git worktree add failed for {task_id}: {detail}")

        now = self._clock()
        branch_point = _git(self.main_root, "rev-parse", "HEAD").stdout.strip() or None
        record = WorktreeRecord(
            task_id=task_id,
            path=str(tree_path),
            branch=branch,
            created_at=now,
            updated_at=now,
            last_heartbeat=now,
            state="running",
            tick=tick,
            brief_path=brief_path,
            main_root=str(self.main_root),
            branch_point=branch_point,
        )
        records[task_id] = record
        self._save_registry(records)
        return record, True

    def _reuse_existing(
        self,
        record: WorktreeRecord,
        task_id: str,
        branch: str,
        brief_path: str | None,
        tick: str | None,
    ) -> tuple[WorktreeRecord, bool]:
        """Reuse a registry entry only when git agrees; otherwise fail clearly."""
        trees = self._git_worktrees()
        tree_meta = trees.get(str(Path(record.path).resolve()))
        problems: list[str] = []
        if tree_meta is None:
            problems.append("git has no worktree registered at " + record.path)
        elif not Path(record.path).is_dir():
            problems.append(f"tree directory {record.path} no longer exists")
        else:
            expected = f"refs/heads/{branch}"
            if tree_meta.get("branch") != expected:
                problems.append(
                    f"worktree is on {tree_meta.get('branch', 'a detached HEAD')!r}, "
                    f"expected {expected!r}"
                )
        if problems:
            raise WorktreeError(
                f"registry entry for task {task_id!r} does not match git reality: "
                + "; ".join(problems)
                + " — run `gitreins worktree clean` to reconcile first"
            )
        changed = False
        if brief_path is not None and record.brief_path != brief_path:
            record.brief_path = brief_path
            changed = True
        if tick is not None and record.tick != tick:
            record.tick = tick
            changed = True
        if record.state in PROTECTED_STATES or record.state == "merged":
            record.state = "running"
            changed = True
        if changed:
            record.updated_at = self._clock()
            self._touch_heartbeat(record, persist=False)
            records = self._load_registry()
            records[task_id] = record
            self._save_registry(records)
        return record, False

    # ── heartbeat / phase ───────────────────────────────────────────

    def _touch_heartbeat(self, record: WorktreeRecord, persist: bool = True) -> None:
        record.last_heartbeat = self._clock()
        if persist:
            records = self._load_registry()
            records[record.task_id] = record
            self._save_registry(records)

    def heartbeat(self, task_id: str) -> WorktreeRecord:
        """Refresh the liveness heartbeat for a task's worktree entry."""
        records = self._load_registry()
        record = records.get(task_id)
        if record is None:
            raise KeyError(f"no worktree registered for task: {task_id}")
        self._touch_heartbeat(record)
        return record

    def mark_phase(self, task_id: str, phase: str) -> WorktreeRecord:
        """Record a lifecycle phase (guarding/judging) and refresh the heartbeat."""
        if phase not in ("guarding", "judging"):
            raise ValueError(f"unknown phase {phase!r}: expected 'guarding' or 'judging'")
        records = self._load_registry()
        record = records.get(task_id)
        if record is None:
            raise KeyError(f"no worktree registered for task: {task_id}")
        record.state = phase
        self._touch_heartbeat(record)
        return record

    # ── listing ─────────────────────────────────────────────────────

    def list_records(self) -> list[WorktreeRecord]:
        """Reconciled, registry-ordered listing (truthful by construction)."""
        records = self.reconcile_and_persist()
        return sorted(records.values(), key=lambda r: r.created_at)

    # ── cleanup ─────────────────────────────────────────────────────

    def clean(
        self,
        *,
        confirm_stale_orphan: bool = False,
    ) -> dict[str, list[str]]:
        """Reap merged trees immediately; stale/orphan only when confirmed.

        Stale and orphan trees carry unverified work or unexplained absence —
        they are never removed without ``confirm_stale_orphan=True``.  Returns
        a report: ``{"removed": [...], "kept": [(task_id, state), ...],
        "branches_deleted": [...]}``.
        """
        records = self.reconcile_and_persist()
        removed: list[str] = []
        branches_deleted: list[str] = []
        kept: list[tuple[str, str]] = []

        for task_id, record in list(records.items()):
            if record.state == "merged":
                self._remove_tree(record, branches_deleted)
                removed.append(task_id)
                del records[task_id]
            elif record.state in PROTECTED_STATES:
                if confirm_stale_orphan:
                    self._remove_tree(record, branches_deleted)
                    removed.append(task_id)
                    del records[task_id]
                else:
                    kept.append((task_id, record.state))
            else:
                kept.append((task_id, record.state))

        self._save_registry(records)
        return {"removed": removed, "kept": kept, "branches_deleted": branches_deleted}

    def _remove_tree(self, record: WorktreeRecord, branches_deleted: list[str]) -> None:
        """Remove the git worktree (never touching the branch) plus its branch."""
        _git(self.main_root, "worktree", "remove", "--force", str(record.path), check=False)
        # If the tree directory is gone already, prune so git forgets it.
        _git(self.main_root, "worktree", "prune", check=False)
        if self._branch_tip(record.branch) is not None:
            result = _git(
                self.main_root,
                "branch",
                "-d",
                record.branch,
                check=False,
            )
            if result.returncode == 0:
                branches_deleted.append(record.branch)

    # ── worker brief ────────────────────────────────────────────────

    def worker_brief_path(self, task_id: str) -> Path:
        """Deterministic worker-brief path for a task inside its isolated worktree.

        Computed from the task id (never the registry) so the CLI can resolve
        it before the worktree exists and stamp it on the new record.
        """
        validate_task_id(task_id)
        return _default_tree_root(self.main_root, task_id) / ".gitreins" / "worker-brief.md"
