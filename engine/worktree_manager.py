"""Task-owned Git worktree lifecycle with a durable, reconciled registry.

One command turns a task into an isolated workspace: ``git worktree add``
creates ``../<repo>-wt/<task-id>`` on branch ``gitreins/task/<task-id>``, and
the creation is recorded in the main checkout's ``.gitreins/worktrees.json``
so the mapping survives process restarts.  Every lifecycle entry point
reconciles the registry against real ``git worktree`` metadata before acting:
work is never destroyed silently — merged and failed trees are the only thing
removal reaps without an explicit confirmation flag (a failed tree that still
holds uncommitted files is kept and the reason is reported), and stale/orphan
trees are removed only with ``--confirm-stale-orphan``.

State semantics (deterministic, derived — never stored as truth):

- ``running``  — the registered tree exists and its branch still has commits
                 not reachable from the main checkout's HEAD.
- ``merged``   — the branch tip is fully merged into HEAD (an idle tree at the
                 branch point counts as merged).
- ``guarding`` / ``judging`` — recorded in the registry when a guard/judge run
                 touches the entry (via :meth:`mark_phase`); reconcile keeps
                 them while the tree exists and the branch is unmerged.
- ``failed``   — the last fleet lane run exited non-zero (recorded via
                 :meth:`mark_lane`).  Terminal: there is no retry path, so
                 :meth:`clean` reaps a failed tree like a merged one and a
                 fleet re-run starts from a fresh tree.
- ``stale``    — tree exists, branch unmerged, and no heartbeat for longer
                 than ``STALE_AFTER_SECONDS`` (default 24h).
- ``orphan``   — the recorded tree is missing from git's worktree metadata, or
                 the tree directory no longer resolves as a worktree.

The registry file lives in the canonical main checkout's ``.gitreins/`` so
every worktree of the same repository reads and writes one truth.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path

# Task IDs become path components and branch names.  Keep the shape strict:
# letters, digits, hyphen, underscore, dot — no separators, no traversal.
TASK_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
BRANCH_PREFIX = "gitreins/task/"
WORKTREES_FILE = "worktrees.json"
STALE_AFTER_SECONDS = 24 * 60 * 60

LIVE_STATES = ("running", "guarding", "judging")
LANE_STATES = ("running", "guarding", "judging", "completed", "failed")
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
    lane_phase: str | None = None
    lane_result: dict | None = None
    lane_command: list[str] | None = None
    exit_code: int | None = None
    output: str | None = None
    started_at: float | None = None
    finished_at: float | None = None
    error: str | None = None

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
        if self.lane_phase is not None:
            data["lane_phase"] = self.lane_phase
        if self.lane_result is not None:
            data["lane_result"] = self.lane_result
        if self.lane_command is not None:
            data["lane_command"] = list(self.lane_command)
        if self.exit_code is not None:
            data["exit_code"] = self.exit_code
        if self.output is not None:
            data["output"] = self.output
        if self.started_at is not None:
            data["started_at"] = self.started_at
        if self.finished_at is not None:
            data["finished_at"] = self.finished_at
        if self.error is not None:
            data["error"] = self.error
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
            lane_phase=data.get("lane_phase"),
            lane_result=data.get("lane_result"),
            lane_command=list(data["lane_command"])
            if data.get("lane_command") is not None
            else None,
            exit_code=data.get("exit_code"),
            output=data.get("output"),
            started_at=data.get("started_at"),
            finished_at=data.get("finished_at"),
            error=data.get("error"),
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


def _tier1_skipped_steps(verdict: dict) -> list[str]:
    """Skipped Tier 1 step ids recorded in a persisted verdict (TRUST-001).

    ``verdict.json`` carries ``stages.tier1.{degraded,skipped_steps}``: either
    declared by the stage plan (an undetectable tree) or picked up at runtime
    from a step's ``GITREINS_SKIP:`` marker (linter absent on this machine).
    A judge-gated merge refuses any PASS whose record carries skips — the gate
    that would have caught a defect never ran.
    """
    if not isinstance(verdict, dict):
        return []
    tier1 = (verdict.get("stages") or {}).get("tier1") or {}
    if not isinstance(tier1, dict):
        return []
    skipped = tier1.get("skipped_steps")
    if isinstance(skipped, (list, tuple)):
        return [str(step) for step in skipped if str(step).strip()]
    return ["unknown"] if tier1.get("degraded") else []


def _default_tree_root(main_root: Path, task_id: str) -> Path:
    repo_name = main_root.name or "repo"
    return main_root.parent / f"{repo_name}-wt" / task_id


# ── runtime artifact exemptions (DF-GITREINS-POC-47) ─────────────────────
#
# GitReins writes state into whatever checkout it runs in.  Those files are
# never uncommitted user work, but a consumer whose .gitignore predates them
# (install/init only writes gitreins.cli.GITREINS_GITIGNORE_ENTRIES) sees them
# in `git status`, and merge() refuses to touch main or a task worktree that is
# not clean — so one ordinary run used to make the whole fleet unmergeable
# with "canonical main has uncommitted changes; refusing merge".
#
# Explicit names (never a blanket `.gitreins/` exemption — real user work can
# live there), plus lock files under the GitReins store: a lock carries no
# information (two agents holding one at once produce a tracked conflict —
# REVIEW-006) and every writer that takes one recreates it on the next run.
RUNTIME_ARTIFACT_FILES = frozenset(
    {
        ".gitreins/worktrees.json",  # this module's worktree registry
        ".gitreins/worktrees.lock",  # ... and its flock sidecar
        ".gitreins/disposable.json",  # disposable verifier registry
        ".gitreins/disposable.lock",  # ... and its flock
        ".gitreins/tasks.yaml.lock",  # task store flock (engine/task_manager.py)
        ".coding-hermes/board/events.jsonl",  # fleet board event log
    }
)
# Runtime artifact DIRECTORIES written by GitReins itself: a guard run inside a
# task worktree writes its run log (DF-018) into .gitreins/logs/, and judge
# verdicts land in .gitreins/history/.
RUNTIME_ARTIFACT_PREFIXES = (".gitreins/history/", ".gitreins/logs/")
RUNTIME_ARTIFACT_LOCK_ROOT = ".gitreins/"
# The files `uv run <guard>` regenerates beside the venv it links into a task
# worktree.  Exempt only while UNTRACKED: a tracked lockfile that differs from
# HEAD is real repo state the consumer should decide about.
WORKTREE_VENV_LOCKFILES = ("uv.lock", ".uv.lock")


def _is_runtime_artifact(path: str) -> bool:
    """True when ``path`` (repo-relative) is a file GitReins itself wrote."""
    if path in RUNTIME_ARTIFACT_FILES or path.startswith(RUNTIME_ARTIFACT_PREFIXES):
        return True
    return path.startswith(RUNTIME_ARTIFACT_LOCK_ROOT) and path.endswith(".lock")


def _matches_exemption(path: str, names: tuple[str, ...]) -> bool:
    """Match a bare name both as a file and as an expanded directory.

    ``git status --untracked-files=all`` reports a symlinked venv as one entry
    (``.venv``) but a regenerated real directory as one entry per file
    (``.venv/bin/python``), so both shapes have to be recognised.
    """
    for name in names:
        if path == name or path.startswith(f"{name}/"):
            return True
    return False


def _exclusive_operation(method):
    """Serialize registry read-modify-write operations across processes."""

    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with self._exclusive_lock():
            return method(self, *args, **kwargs)

    return wrapped


class WorktreeManager:
    """Create, register, reconcile, and clean task-owned worktrees.

    The registry lives in the canonical main checkout's ``.gitreins/``
    directory so every linked worktree shares one mapping.  All mutations
    rewrite the file atomically (same-directory temp file + ``os.replace``).
    """

    def __init__(
        self,
        workdir: str | os.PathLike[str] | None = None,
        clock=time.time,
        *,
        venv_source: str | os.PathLike[str] | None = None,
        venv_name: str | None = None,
    ):
        from engine.repo_paths import resolve_worktree_paths

        self._clock = clock
        paths = resolve_worktree_paths(workdir)
        self.main_root = paths.canonical_main_root
        self._gitreins_dir = self.main_root / ".gitreins"
        self._registry_file = self._gitreins_dir / WORKTREES_FILE
        self._lock_state = threading.local()
        from engine.config import load_defaults

        defaults = load_defaults(str(self.main_root))
        if venv_source is None:
            venv_source = defaults.worktree_venv_source
        if venv_name is None:
            venv_name = defaults.worktree_venv_name
        self.worktree_disk_ceiling_mb = defaults.worktree_disk_ceiling_mb
        self.venv_source = str(venv_source)
        self.venv_name = str(venv_name)
        self._validate_venv_name()

    def _validate_venv_name(self) -> None:
        name = Path(self.venv_name)
        if name.is_absolute() or name.name != self.venv_name or self.venv_name in {"", ".", ".."}:
            raise WorktreeValidationError(
                f"invalid worktree venv_name {self.venv_name!r}: must be a direct child name"
            )

    @contextmanager
    def _exclusive_lock(self):
        """Hold one advisory lock for nested registry and merge operations."""
        depth = getattr(self._lock_state, "depth", 0)
        if depth:
            self._lock_state.depth = depth + 1
            try:
                yield
            finally:
                self._lock_state.depth = depth
            return

        import fcntl

        self._gitreins_dir.mkdir(parents=True, exist_ok=True)
        lock_path = self._gitreins_dir / "worktrees.lock"
        with open(lock_path, "a+", encoding="utf-8") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            self._lock_state.depth = 1
            try:
                yield
            finally:
                self._lock_state.depth = 0
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

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
                elif record.state in LANE_STATES:
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

    @_exclusive_operation
    def reconcile_and_persist(self) -> dict[str, WorktreeRecord]:
        """Boot-time equivalent: reconcile, persist, return."""
        records = self.reconcile()
        self.save_reconciled(records)
        return records

    # ── create ──────────────────────────────────────────────────────

    def _venv_paths(self, tree_path: Path) -> tuple[Path, Path | None]:
        source = Path(self.venv_source).expanduser()
        if not source.is_absolute():
            source = self.main_root / source
        if not source.exists() and not source.is_symlink():
            return tree_path / self.venv_name, None
        return tree_path / self.venv_name, source

    def _link_venv(self, tree_path: Path) -> None:
        destination, source = self._venv_paths(tree_path)
        if source is None:
            return
        if destination.exists() or destination.is_symlink():
            raise WorktreeError(
                f"worktree venv destination {destination} already exists; refusing to overwrite"
            )
        try:
            os.symlink(source, destination, target_is_directory=True)
        except OSError as exc:
            raise WorktreeError(
                f"could not symlink shared venv {source} to {destination}: {exc}"
            ) from exc

    def _remove_new_tree(self, tree_path: Path, branch: str) -> None:
        """Best-effort rollback for a failed first-time create."""
        _git(self.main_root, "worktree", "remove", "--force", str(tree_path), check=False)
        _git(self.main_root, "worktree", "prune", check=False)
        _git(self.main_root, "branch", "-D", branch, check=False)

    @_exclusive_operation
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
                raise WorktreeError(f"path {tree_path} is already registered to task {other_id!r}")

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
        venv_destination, venv_source = self._venv_paths(tree_path)
        if venv_source is not None and (venv_destination.exists() or venv_destination.is_symlink()):
            raise WorktreeError(
                "worktree venv destination "
                f"{venv_destination} already exists; refusing to overwrite"
            )

        # The disposable verifier owns the shared byte-counting policy.  Keep
        # this import lazy so WorktreeManager remains usable without loading
        # the CLI-facing disposable surface.
        from engine.worktree_disposable import enforce_disk_ceiling

        enforce_disk_ceiling(self, requested_path=tree_path)
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
        try:
            self._link_venv(tree_path)
        except WorktreeError:
            self._remove_new_tree(tree_path, branch)
            raise

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
                + self._reconcile_hint(record)
            )
        refusal = self._failed_lane_refusal(record)
        if refusal is not None:
            # A failed lane's tree sits at whatever HEAD the failed run left and
            # nothing resets it, so reusing it silently would run the next lane
            # against a pre-feature tree.  Refusing (rather than auto-resetting)
            # keeps whatever evidence the tree holds: the caller reaps it with
            # `clean` and re-runs from a fresh tree.
            raise WorktreeError(refusal)
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

    def _failed_lane_refusal(self, record: WorktreeRecord) -> str | None:
        """The message refusing reuse of a failed lane's stale tree, or ``None``.

        ``record.state`` is the *reconciled* state, and reconcile reclassifies a
        failure older than :data:`STALE_AFTER_SECONDS` as ``stale``.
        ``record.lane_phase`` (written by :meth:`mark_lane`) remembers the
        terminal lane outcome across that reclassification, so a failed-then-aged
        tree is not silently reused at its old HEAD either.
        """
        if record.state != "failed" and record.lane_phase != "failed":
            return None
        if record.state in PROTECTED_STATES:
            return (
                f"lane {record.task_id!r} has a FAILED worktree at a stale HEAD: run "
                "`gitreins worktree clean --confirm-stale-orphan` to reap it and start fresh"
            )
        return (
            f"lane {record.task_id!r} has a FAILED worktree at a stale HEAD: run "
            "`gitreins worktree clean` (failed lanes are reaped by clean) to start fresh"
        )

    @staticmethod
    def _reconcile_hint(record: WorktreeRecord) -> str:
        """The hint naming the command that ACTUALLY resolves this mismatch.

        The hint must fit the problem class: plain ``clean`` reaps merged and
        failed entries, a stale/orphan mismatch is protected behind
        ``--confirm-stale-orphan``, and a live entry (running/guarding/judging)
        is not reaped by anything, so it names the manual escalation.  Pointing
        every class at plain ``clean`` is what left consumers re-running a
        command that could not fix the state.
        """
        if record.state in PROTECTED_STATES:
            return (
                " — run `gitreins worktree clean --confirm-stale-orphan` to reconcile first "
                "(stale/orphan entries are only reaped with the confirmation flag)"
            )
        if record.state in ("merged", "failed"):
            return (
                " — run `gitreins worktree clean` to reconcile first "
                "(clean reaps merged and failed entries)"
            )
        return (
            " — run `gitreins worktree clean` first; if `clean` keeps this entry, "
            "remove the tree by hand (`git worktree remove --force` then "
            "`git branch -D`) once its work is accounted for"
        )

    # ── heartbeat / phase ───────────────────────────────────────────

    def _touch_heartbeat(self, record: WorktreeRecord, persist: bool = True) -> None:
        record.last_heartbeat = self._clock()
        if persist:
            records = self._load_registry()
            records[record.task_id] = record
            self._save_registry(records)

    @_exclusive_operation
    def heartbeat(self, task_id: str) -> WorktreeRecord:
        """Refresh the liveness heartbeat for a task's worktree entry."""
        records = self._load_registry()
        record = records.get(task_id)
        if record is None:
            raise KeyError(f"no worktree registered for task: {task_id}")
        self._touch_heartbeat(record)
        return record

    @_exclusive_operation
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

    @_exclusive_operation
    def mark_lane(
        self,
        task_id: str,
        phase: str,
        *,
        command: list[str] | None = None,
        result: dict | None = None,
        exit_code: int | None = None,
        output: str | None = None,
        error: str | None = None,
    ) -> WorktreeRecord:
        """Atomically record fleet phase, heartbeat, and latest lane evidence."""
        if phase not in LANE_STATES:
            raise ValueError(f"unknown lane phase {phase!r}: expected one of {LANE_STATES}")
        records = self._load_registry()
        record = records.get(task_id)
        if record is None:
            raise KeyError(f"no worktree registered for task: {task_id}")
        now = self._clock()
        record.state = phase
        record.lane_phase = phase
        record.updated_at = now
        record.last_heartbeat = now
        if command is not None:
            record.lane_command = list(command)
        if result is not None:
            record.lane_result = result
        if exit_code is not None:
            record.exit_code = exit_code
        if output is not None:
            record.output = output
        if error is not None:
            record.error = error
        if phase == "running" and record.started_at is None:
            record.started_at = now
        if phase in ("completed", "failed"):
            record.finished_at = now
        self._save_registry(records)
        return record

    # ── listing ─────────────────────────────────────────────────────

    @_exclusive_operation
    def list_records(self) -> list[WorktreeRecord]:
        """Reconciled, registry-ordered listing (truthful by construction)."""
        records = self.reconcile_and_persist()
        return sorted(records.values(), key=lambda r: r.created_at)

    # ── judge-gated merge-back ──────────────────────────────────────

    @_exclusive_operation
    def merge(
        self,
        task_id: str,
        *,
        force: bool = False,
        actor: str | None = None,
        reason: str = "explicit judge-gate override",
        guard_runner=None,
        judge_runner=None,
    ) -> dict:
        """Apply a task worktree to canonical main only after safety gates."""
        task_id = validate_task_id(task_id)
        if force and (not isinstance(actor, str) or not actor.strip()):
            raise WorktreeError("--force requires a non-empty actor identity via --actor")

        records = self.reconcile_and_persist()
        record = records.get(task_id)
        if record is None:
            raise WorktreeError(f"task {task_id!r} is not registered with a worktree")
        tree = Path(record.path).resolve()
        if not tree.is_dir():
            raise WorktreeError(f"task {task_id!r} worktree is unavailable: {tree}")
        if record.main_root and Path(record.main_root).resolve() != self.main_root.resolve():
            raise WorktreeError("worktree registry points at a different canonical main checkout")
        expected_branch = _branch_for(task_id)
        expected_tree = _default_tree_root(self.main_root, task_id).resolve()
        if record.branch != expected_branch or tree != expected_tree:
            raise WorktreeError(
                "registered task worktree has an unsafe branch or path; refusing merge"
            )

        trees = self._git_worktrees()
        tree_meta = trees.get(str(tree))
        if tree_meta is None or tree_meta.get("branch") != f"refs/heads/{record.branch}":
            raise WorktreeError("registered task worktree does not match Git branch metadata")
        branch_result = _git(
            self.main_root, "symbolic-ref", "--quiet", "--short", "HEAD", check=False
        )
        if branch_result.returncode != 0 or not branch_result.stdout.strip():
            raise WorktreeError("canonical main is detached; refusing merge")
        destination_branch = branch_result.stdout.strip()
        if not self._is_clean(self.main_root):
            raise WorktreeError("canonical main has uncommitted changes; refusing merge")
        if not self._is_clean(tree):
            raise WorktreeError("task worktree has uncommitted changes; refusing merge")

        main_head = self._head(self.main_root)
        source_head = self._branch_tip(record.branch)
        if main_head is None or source_head is None:
            raise WorktreeError("canonical main or task branch has no resolvable commit")
        if not record.branch_point or not self._commit_exists(record.branch_point):
            raise WorktreeError("registered worktree has no valid branch_point; refusing merge")
        if source_head == record.branch_point:
            raise WorktreeError("task branch has no commits beyond its registered branch point")

        if not force:
            verdict = self._find_verdict(task_id, record, source_head)
            if verdict is None:
                raise WorktreeError(
                    "no PASS verdict for the exact task/worktree/branch commit; "
                    f"inspect {self._verdict_reference(task_id, record)}"
                )
            if verdict.get("passed") is not True:
                raise WorktreeError(
                    "HOLD: the matching verdict is FAIL; worktree and branch were preserved. "
                    f"Verdict: {self._verdict_reference(task_id, record, verdict)}"
                )
            skipped = _tier1_skipped_steps(verdict)
            if skipped:
                raise WorktreeError(
                    "HOLD: the matching verdict's Tier 1 carries skipped checks "
                    f"({', '.join(skipped)}) — a PASS whose gates never ran cannot merge. "
                    "Re-run the judge with those gates available (or pass --force with "
                    "--actor to override). Verdict: "
                    f"{self._verdict_reference(task_id, record, verdict)}"
                )

        rebased = False
        if main_head != record.branch_point:
            rebased = True
            rebase = _git(tree, "rebase", destination_branch, check=False)
            if rebase.returncode != 0:
                _git(tree, "rebase", "--abort", check=False)
                detail = rebase.stderr.strip() or rebase.stdout.strip() or "unknown conflict"
                raise WorktreeError(f"rebase failed; task worktree held: {detail}")
            source_head = self._branch_tip(record.branch)
            if source_head is None:
                raise WorktreeError("rebased task branch has no resolvable commit")
            if self._head(self.main_root) != main_head:
                raise WorktreeError("canonical main moved during rebase; task worktree held")

            runner = guard_runner or self._default_guard_runner
            try:
                guard_result = runner(tree)
            except Exception as exc:
                raise WorktreeError(f"HOLD: configured guard failed after rebase: {exc}") from exc
            if not self._runner_passed(guard_result):
                raise WorktreeError(
                    "HOLD: configured guard failed after rebase; task worktree preserved"
                )
            if not force:
                runner = judge_runner or self._default_judge_runner
                try:
                    fresh = runner(tree, task_id)
                except Exception as exc:
                    raise WorktreeError(f"HOLD: fresh judge failed after rebase: {exc}") from exc
                verdict = self._fresh_verdict(task_id, record, source_head, fresh)
                if verdict is None or verdict.get("passed") is not True:
                    raise WorktreeError(
                        "HOLD: no fresh PASS verdict for the rebased commit; "
                        f"inspect {self._verdict_reference(task_id, record, verdict)}"
                    )
                skipped = _tier1_skipped_steps(verdict)
                if skipped:
                    raise WorktreeError(
                        "HOLD: the fresh verdict's Tier 1 carries skipped checks "
                        f"({', '.join(skipped)}) — a PASS whose gates never ran cannot "
                        "merge; task worktree preserved."
                    )

        # Recheck all safety facts immediately before changing canonical main.
        if self._head(self.main_root) != main_head:
            raise WorktreeError("canonical main moved before merge; task worktree held")
        if not self._is_clean(self.main_root) or not self._is_clean(tree):
            raise WorktreeError("Git safety precondition changed before merge; task worktree held")
        if not self._is_ancestor(destination_branch, record.branch):
            raise WorktreeError("task branch is not fast-forwardable onto canonical main")
        source_head = self._branch_tip(record.branch)
        if source_head is None:
            raise WorktreeError("task branch disappeared before merge")

        if force:
            self._append_board_event(
                {
                    "event_type": "worktree_merge_override",
                    "task_id": task_id,
                    "actor": actor,
                    "source_branch": record.branch,
                    "source_commit": source_head,
                    "destination_commit": source_head,
                    "reason": reason,
                    "policy_state": "verdict_gate_bypassed",
                }
            )

        applied = _git(
            self.main_root, "merge", "--ff-only", "--no-edit", record.branch, check=False
        )
        if applied.returncode != 0 or self._head(self.main_root) != source_head:
            detail = (
                applied.stderr.strip() or applied.stdout.strip() or "fast-forward was not applied"
            )
            raise WorktreeError(f"merge was not applied; task worktree held: {detail}")

        self._append_board_event(
            {
                "event_type": "worktree_merged",
                "task_id": task_id,
                "actor": actor or "gitreins-worktree-merge",
                "source_branch": record.branch,
                "source_commit": source_head,
                "destination_branch": destination_branch,
                "destination_commit": source_head,
                "policy_state": "force" if force else ("rebased_pass" if rebased else "pass"),
            }
        )
        branches_deleted: list[str] = []
        self._remove_tree(record, branches_deleted)
        if tree.exists() or self._branch_tip(record.branch) is not None:
            raise WorktreeError(
                "merge applied but reaping was incomplete; task worktree was not forgotten"
            )
        del records[task_id]
        self._save_registry(records)
        return {
            "task_id": task_id,
            "mode": "rebased-fast-forward" if rebased else "fast-forward",
            "source_commit": source_head,
            "destination_commit": source_head,
            "branch": record.branch,
            "worktree": str(tree),
        }

    def _is_task_worktree(self, workdir: Path) -> bool:
        """True when ``workdir`` is a linked task worktree, not canonical main.

        The venv exemption below is scoped to task trees on purpose:
        :meth:`_link_venv` symlinks the configured venv into every tree it
        creates, so an untracked venv there is the harness's own artifact.  An
        untracked ``.venv`` in canonical main is the consumer's real
        uncommitted state (their repo never gitignored it) and must still hold
        the merge, so main never gets this exemption.
        """
        return Path(workdir).resolve() != self.main_root.resolve()

    def _is_clean(self, workdir: Path) -> bool:
        """True when ``workdir`` holds no uncommitted work of its own.

        GitReins' runtime artifacts (:data:`RUNTIME_ARTIFACT_FILES` /
        :data:`RUNTIME_ARTIFACT_PREFIXES`, plus any lock inside the store) are
        exempt everywhere — they are written by the harness, not by the user,
        and counting them as dirt made every fleet merge refuse on a stock
        install (DF-GITREINS-POC-47).

        Inside a TASK worktree the configured venv name (``self.venv_name``,
        default ``.venv``) is exempt too, plus an untracked ``uv.lock`` /
        ``.uv.lock`` next to it: :meth:`_link_venv` creates the one and the
        configured guard (``uv run pytest``) regenerates the other inside the
        tree.  Everything else — an untracked or modified file — is real work
        and keeps the gate closed.
        """
        status = _git(workdir, "status", "--porcelain", "--untracked-files=all").stdout
        in_task_tree = self._is_task_worktree(workdir)
        for line in status.splitlines():
            if len(line) < 4:
                return False
            code, path = line[:2], line[3:]
            if _is_runtime_artifact(path):
                continue
            if in_task_tree:
                exempt = (self.venv_name, *(WORKTREE_VENV_LOCKFILES if code == "??" else ()))
                if _matches_exemption(path, exempt):
                    continue
            return False
        return True

    def _head(self, workdir: Path) -> str | None:
        result = _git(workdir, "rev-parse", "--verify", "HEAD", check=False)
        return result.stdout.strip() if result.returncode == 0 and result.stdout.strip() else None

    def _commit_exists(self, commit: str) -> bool:
        result = _git(self.main_root, "cat-file", "-e", f"{commit}^{{commit}}", check=False)
        return result.returncode == 0

    def _is_ancestor(self, ancestor: str, descendant: str) -> bool:
        result = _git(
            self.main_root,
            "merge-base",
            "--is-ancestor",
            ancestor,
            descendant,
            check=False,
        )
        return result.returncode == 0

    def _verdict_reference(
        self, task_id: str, record: WorktreeRecord, verdict: dict | None = None
    ) -> str:
        from engine.persist import VerdictPersister

        # Verdicts are produced in the task checkout, not canonical main.  A
        # branch-backed verdict may no longer have a local copy, so identify
        # that source explicitly instead of pointing at an unrelated main
        # checkout history directory.
        task_tree = Path(record.path).resolve()
        persister = VerdictPersister(str(task_tree))
        if verdict and verdict.get("_date") and verdict.get("_hash"):
            local_path = (
                Path(persister.history_dir)
                / str(verdict["_date"])
                / str(verdict["_hash"])
                / "verdict.json"
            )
            if local_path.is_file():
                return str(local_path)
            branch_path = (
                f"{persister._branch_history_prefix().rstrip('/')}/"
                f"{verdict['_date']}/{verdict['_hash']}/verdict.json"
            )
            return f"gitreins:{branch_path} (from {task_tree})"
        return f"{persister.history_dir} (task {task_id})"

    def _matching_verdicts(self, task_id: str, record: WorktreeRecord) -> list[dict]:
        from engine.persist import VerdictPersister

        # _persist_result(str(tree), ...) stamps and writes the verdict in the
        # producing task worktree.  Reading canonical main would let unrelated
        # local history suppress the gitreins-branch fallback in
        # VerdictPersister.list_verdicts().
        task_tree = Path(record.path).resolve()
        persister = VerdictPersister(str(task_tree))
        try:
            entries = persister.list_verdicts(n=10000, task_id=task_id)
        except Exception as exc:
            raise WorktreeError(f"could not read verdict history: {exc}") from exc
        worktree = str(task_tree)
        return [
            entry
            for entry in entries
            if str(entry.get("worktree", "")) == worktree and entry.get("branch") == record.branch
        ]

    def _find_verdict(self, task_id: str, record: WorktreeRecord, source_head: str) -> dict | None:
        for entry in self._matching_verdicts(task_id, record):
            if entry.get("commit") == source_head or entry.get("source_commit") == source_head:
                return entry
        return None

    def _fresh_verdict(
        self, task_id: str, record: WorktreeRecord, source_head: str, fresh
    ) -> dict | None:
        if isinstance(fresh, dict) and fresh.get("passed") is True:
            if fresh.get("commit", fresh.get("source_commit")) == source_head:
                return fresh
        return self._find_verdict(task_id, record, source_head)

    @staticmethod
    def _runner_passed(result) -> bool:
        if isinstance(result, bool):
            return result
        return bool(getattr(result, "passed", False))

    def _default_guard_runner(self, tree: Path) -> bool:
        from engine.guard_manager import GuardManager
        from gitreins.cli import load_config

        result = GuardManager(str(tree), config=load_config(str(tree))).run_all()
        return result.passed

    def _default_judge_runner(self, tree: Path, task_id: str) -> dict:
        from engine.judge import Judge
        from engine.llm import LLMClient
        from engine.task_manager import TaskManager
        from gitreins.cli import _persist_result, load_config

        task = TaskManager(str(self.main_root)).get(task_id)
        if task is None:
            raise WorktreeError(f"task {task_id!r} is missing from the task store")
        result = Judge(LLMClient(), str(tree), guard_config=load_config(str(tree))).evaluate_task(
            task
        )
        _persist_result(str(tree), task, result)
        return {
            "passed": result.passed,
            "commit": self._head(tree),
            "worktree": str(tree.resolve()),
            "branch": _git(tree, "symbolic-ref", "--quiet", "--short", "HEAD").stdout.strip(),
        }

    def _append_board_event(self, event: dict) -> dict | None:
        """Append one valid event while allocating an id under an advisory lock.

        Returns the written entry, or ``None`` when this checkout has no fleet
        board.  ``.coding-hermes/board/`` is a Hermes scheduler artifact that
        ``gitreins install``/``init`` never create, so the board event is
        skipped silently rather than failing (and rather than creating a board
        directory nobody asked for): the merge or lane phase it describes did
        happen, and fleet bookkeeping must not be a precondition for it.
        """
        import fcntl

        from engine.repo_paths import board_file_path

        # Board events are the only board consumer in this manager; there is
        # nothing to record to when the board is not configured.
        if not (self.main_root / ".coding-hermes" / "board").is_dir():
            return None

        path = board_file_path(self.main_root, "events.jsonl")
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a+", encoding="utf-8") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            stream.seek(0)
            next_id = 1
            for line in stream:
                try:
                    existing = json.loads(line)
                except (json.JSONDecodeError, TypeError):
                    continue
                if isinstance(existing, dict) and isinstance(existing.get("id"), int):
                    next_id = max(next_id, existing["id"] + 1)
            entry = {
                "id": next_id,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                **event,
            }
            stream.seek(0, os.SEEK_END)
            stream.write(json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        return entry

    # ── cleanup ─────────────────────────────────────────────────────

    @_exclusive_operation
    def clean(
        self,
        *,
        confirm_stale_orphan: bool = False,
    ) -> dict[str, list[str]]:
        """Reap merged and failed trees immediately; stale/orphan when confirmed.

        ``failed`` is terminal — there is no retry path through ``clean`` (a
        fleet re-run is expected to create a fresh tree), so a failed lane's
        tree and branch are reaped exactly like a merged one: the tree goes, and
        the branch is deleted only when it is fully merged into HEAD
        (``branches_deleted`` names the ones removed; a branch still carrying
        unmerged commits survives, exactly as it does for a merged entry).  The
        one exception to removal is a failed tree that still holds uncommitted
        work of its own (:meth:`_is_clean`): that is evidence nobody has
        committed yet, so the tree is kept and the reason is reported — the same
        doctrine as ``worktree.sh reap``, which never touches a worktree with
        uncommitted files.

        Stale and orphan trees carry unverified work or unexplained absence —
        they are never removed without ``confirm_stale_orphan=True``.  Returns
        a report: ``{"removed": [...], "kept": [(task_id, state), ...],
        "branches_deleted": [...], "kept_reasons": {task_id: reason}}``.
        """
        records = self.reconcile_and_persist()
        removed: list[str] = []
        branches_deleted: list[str] = []
        kept: list[tuple[str, str]] = []
        kept_reasons: dict[str, str] = {}

        for task_id, record in list(records.items()):
            if record.state in ("merged", "failed"):
                reason = self._failed_reap_blocker(record) if record.state == "failed" else None
                if reason is None:
                    self._remove_tree(record, branches_deleted)
                    removed.append(task_id)
                    del records[task_id]
                else:
                    record.notes = [reason]
                    kept.append((task_id, record.state))
                    kept_reasons[task_id] = reason
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
        return {
            "removed": removed,
            "kept": kept,
            "branches_deleted": branches_deleted,
            "kept_reasons": kept_reasons,
        }

    def _failed_reap_blocker(self, record: WorktreeRecord) -> str | None:
        """Why a failed tree must be kept, or ``None`` when it can be reaped.

        Read-only and fail-closed: an unreadable tree is never removed on a
        guess.  A tree that is already gone has nothing left to destroy.
        """
        tree = Path(record.path)
        if not tree.is_dir():
            return None
        try:
            clean_tree = self._is_clean(tree)
        except WorktreeError:
            return (
                "worktree state could not be read; inspect it by hand before reaping "
                "(clean never removes a tree it cannot verify)"
            )
        if clean_tree:
            return None
        return (
            "worktree has uncommitted files; commit or discard them, then re-run "
            "`gitreins worktree clean`"
        )

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
