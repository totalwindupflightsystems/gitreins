"""Disposable Git worktrees for hermetic QA, dogfood, and repro runs.

Disposable trees are detached from a named branch and live under a separate
``.disposable`` directory, so a verification run cannot collide with a task
worktree or alter the canonical checkout.  Registry writes are atomic and
locked because a repro farm may create and reap several trees concurrently.

Every registry entry carries a **status** so a kept tree is never
indistinguishable from a run that is still in flight (DF-GITREINS-POC-80):

``running``
    The entry was created and the run is believed to be in flight.  A
    ``running`` entry whose process is gone (or which never recorded a pid
    inside :data:`RUN_REGISTRATION_GRACE_SECONDS`) was killed before it could
    finalize itself — an *interrupted* run, reaped by the next reap pass and
    reported as such.
``kept``
    The run finished with exit code 0 and its tree was retained because the
    caller asked for it (``--keep``, or a ``--keep-failures`` pass that keeps
    every tree and reaps the passing ones afterwards).
``kept-failure``
    The run finished non-zero and its tree was retained for inspection
    (``--keep`` on a failing command, or ``--keep-failures``).  This is the
    status a failure artifact carries; :attr:`DisposableRecord.exit_code`
    holds the command's exit code.

``gitreins worktree clean`` reaps kept and kept-failure trees and *reports*
every entry it could not reap (a live run, a tree that cannot be removed)
instead of silently skipping it.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Iterator
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from engine.config import load_defaults
from engine.evidence_bounds import MAX_EVIDENCE_CHARS, bound_evidence
from engine.worktree_manager import WorktreeError, WorktreeManager, _git

DISPOSABLE_FILE = "disposable.json"
DISPOSABLE_LOCK = "disposable.lock"

#: Registry status values (DF-GITREINS-POC-80).  ``STATUS_RUNNING`` means the
#: run has not finalized itself yet; the two ``kept`` values are terminal and
#: say whether the retained tree holds a success or a failure artifact.
STATUS_RUNNING = "running"
STATUS_KEPT = "kept"
STATUS_KEPT_FAILURE = "kept-failure"
STATUSES = frozenset({STATUS_RUNNING, STATUS_KEPT, STATUS_KEPT_FAILURE})

#: How long a ``running`` entry may stay without a recorded pid before the
#: reap pass treats it as interrupted.  ``create`` registers the entry and
#: only then starts the child, so a fresh entry legitimately has no pid for a
#: few milliseconds; a window this wide covers the handoff on a loaded host
#: while still guaranteeing that a killed run's entry is reaped by a later
#: ``worktree clean`` rather than pinning the tree forever.
RUN_REGISTRATION_GRACE_SECONDS = 300.0

logger = logging.getLogger("gitreins.worktree_disposable")


def _finished_status(exit_code: int) -> str:
    """Terminal status for a retained run that ended with ``exit_code``."""
    return STATUS_KEPT if exit_code == 0 else STATUS_KEPT_FAILURE


@dataclass
class DisposableRecord:
    """Durable metadata for one disposable worktree run."""

    run_id: str
    path: str
    created_at: float
    command: str
    pid: int | None = None
    keep: bool = False
    status: str = STATUS_RUNNING
    exit_code: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "path": self.path,
            "created_at": self.created_at,
            "command": self.command,
            "pid": self.pid,
            "keep": self.keep,
            "status": self.status,
            "exit_code": self.exit_code,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DisposableRecord:
        return cls(
            run_id=str(data["run_id"]),
            path=str(data["path"]),
            created_at=float(data["created_at"]),
            command=str(data.get("command", "")),
            pid=data.get("pid"),
            keep=bool(data.get("keep", False)),
            # Entries written before the status field existed (or by a foreign
            # writer) infer one: a retained entry is a kept tree, anything else
            # is still believed to be in flight.
            status=_coerced_status(data.get("status"), keep=bool(data.get("keep", False))),
            exit_code=_coerced_exit_code(data.get("exit_code")),
        )


def _coerced_status(value: Any, *, keep: bool) -> str:
    """Normalize a stored status, inferring one for legacy entries."""
    if isinstance(value, str) and value in STATUSES:
        return value
    return STATUS_KEPT if keep else STATUS_RUNNING


def _coerced_exit_code(value: Any) -> int | None:
    """Normalize a stored exit code; anything unexpected reads as unknown."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


@dataclass(frozen=True)
class DisposableReapReport:
    """Per-entry outcome of one reap pass (DF-GITREINS-POC-80).

    A reap pass used to return only the ids it removed, so an entry it
    *skipped* (a live run, a tree that could not be removed) vanished from
    every view — the user saw "Nothing to reap." while a kept tree sat on
    disk.  Every entry now lands in exactly one bucket:

    ``reaped``
        Trees removed by this pass; ``abandoned`` names the subset that were
        interrupted runs (a ``running`` entry whose process is gone).
    ``forgotten``
        Entries dropped from the registry because their tree directory no
        longer exists (git metadata pruned).
    ``kept``
        ``(run_id, status, reason)`` for entries left registered on purpose.
    """

    reaped: list[str] = field(default_factory=list)
    forgotten: list[str] = field(default_factory=list)
    abandoned: list[str] = field(default_factory=list)
    kept: list[tuple[str, str, str]] = field(default_factory=list)

    def removed(self) -> list[str]:
        """Run ids dropped from the registry (reaped trees + forgotten entries)."""
        return [*self.reaped, *self.forgotten]


@dataclass(frozen=True)
class _ReapDecision:
    """What one reap pass should do with one registry entry."""

    action: str  # "reap" | "forget" | "keep"
    reason: str = ""
    interrupted: bool = False


def _decide_reap(
    record: DisposableRecord,
    *,
    now: float,
    tree_exists: bool,
    run_live: bool,
) -> _ReapDecision:
    """Classify one registry entry for a reap pass (pure: all facts passed in).

    Order matters.  A missing tree is forgotten whatever the entry says — the
    tree can never come back and a permanent registry ghost is the defect this
    pass exists to prevent.  A live run is protected.  A ``running`` entry
    whose process is gone (or whose stale pid now belongs to another process)
    is an *interrupted* run: it is reaped like any other unfinished artifact
    and reported as abandoned, instead of keeping its tree — and its registry
    entry — alive forever.
    """
    if not tree_exists:
        return _ReapDecision("forget", "registered tree directory no longer exists")
    if run_live:
        return _ReapDecision("keep", f"run is still active (pid {record.pid})")
    if record.status == STATUS_RUNNING:
        if record.pid is None and now - record.created_at <= RUN_REGISTRATION_GRACE_SECONDS:
            return _ReapDecision(
                "keep",
                "run started less than "
                f"{int(RUN_REGISTRATION_GRACE_SECONDS)}s ago and has not recorded its pid yet",
            )
        return _ReapDecision(
            "reap", "interrupted run: the recorded process is gone", interrupted=True
        )
    return _ReapDecision("reap", f"{record.status} tree")


def _evidence(output: str) -> str:
    """Bound command evidence on LINE boundaries, keeping BOTH ends.

    DF-GITREINS-POC-19: the same defect DF-GITREINS-POC-5 fixed for pipeline
    steps was live here — a head-only slice at a raw character offset
    (``output[:MAX_EVIDENCE_CHARS - 40]``), so a repro/dogfood run's recorded
    evidence ended in a half-written line and dropped the tail where pytest's
    short test summary lives. It now delegates to the shared bounder
    (``engine.evidence_bounds``) used by every other evidence surface.
    """
    return bound_evidence(output.strip(), cap=MAX_EVIDENCE_CHARS)


# ── Child environment contract (DF-GITREINS-POC-71) ─────────────────────────
#
# Every child started inside a disposable tree — the `sh -c` command of
# `worktree fresh`/`repro` and the harness-CLI steps of `worktree dogfood` —
# gets a NORMALIZED environment, never the consumer session's raw one:
#
# 1. TOOLCHAIN: PATH leads with the tree's linked venv ``bin`` (the symlink
#    ``WorktreeManager._link_venv`` created from ``worktree_venv_source``),
#    so ``pytest``, ``git``, and every other tool resolve to the toolchain
#    captured at the tree's HEAD — not to whatever binary the consumer
#    session happened to have first on PATH. Without this, a kept failure
#    tree re-runs the SESSION's pytest and the captured failure is not
#    reproducible in the artifact ``--keep``/``--keep-failures`` exists to
#    preserve (dogfood run 15, 2026-09-26). When the tree's venv link is
#    missing the harness checkout's own source venv bin is used, then the
#    session PATH as a last resort — resolution degrades in a documented
#    order instead of silently picking up the session toolchain.
# 2. HARNESS CLI: the dogfood step runner invokes
#    ``<harness_root>/gitreins/cli.py`` with an interpreter that can import
#    the harness package (the tree's venv python, else ``sys.executable``),
#    and pins ``PYTHONPATH`` to the harness root so the import succeeds
#    regardless of the parent interpreter's provenance (a foreign runtime
#    python otherwise dies with ``ModuleNotFoundError: No module named
#    'engine'``). ``PYTHONPATH`` is REPLACED, not prepended: a hostile
#    session value must not shadow the harness's own ``engine`` package.
#
# ``_child_command_env`` and ``_child_cli_env`` are the only two builders;
# subprocess sites pass their ``env=`` through one of them.


def _tree_venv_bin(
    tree_path: Path, manager: WorktreeManager | DisposableWorktreeManager
) -> Path | None:
    """Best toolchain ``bin`` dir for a disposable tree, or None.

    Prefers the tree's linked venv, then the harness checkout's source venv
    (the same source ``_link_venv`` links from), so a missing link degrades
    to the checkout toolchain instead of the session PATH. Accepts either
    manager surface; ``DisposableWorktreeManager`` delegates to its inner
    ``WorktreeManager``.
    """
    manager = cast(WorktreeManager, getattr(manager, "manager", manager))
    destination, source = manager._venv_paths(tree_path)
    for candidate in (destination / "bin", None if source is None else source / "bin"):
        if candidate is not None and candidate.is_dir():
            return candidate
    return None


def _child_command_env(
    tree_path: Path,
    manager: WorktreeManager | DisposableWorktreeManager,
    *,
    base: dict[str, str] | None = None,
) -> dict[str, str]:
    """Env for a child COMMAND run inside ``tree_path`` (fresh/repro)."""
    env = dict(os.environ if base is None else base)
    venv_bin = _tree_venv_bin(tree_path, manager)
    if venv_bin is not None:
        env["PATH"] = os.pathsep.join([str(venv_bin), env.get("PATH", "")])
    return env


def _child_cli_env(
    tree_path: Path,
    manager: WorktreeManager | DisposableWorktreeManager,
    *,
    base: dict[str, str] | None = None,
) -> dict[str, str]:
    """Env for a child HARNESS-CLI step run inside ``tree_path`` (dogfood)."""
    env = _child_command_env(tree_path, manager, base=base)
    harness_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env["PYTHONPATH"] = harness_root
    python_override = env.get("GITREINS_CHILD_PYTHON")
    if python_override:
        env["PYTHONPATH"] = os.pathsep.join(
            [harness_root] + [part for part in env["PYTHONPATH"].split(os.pathsep) if part]
        )
    return env


def _child_python(tree_path: Path, manager: WorktreeManager | DisposableWorktreeManager) -> str:
    """Interpreter that can import the harness package for CLI steps.

    The tree's venv python (its ``.pth`` makes ``engine`` importable, and it
    is the interpreter the tree's toolchain was built for), else the parent
    ``sys.executable`` — which the ``PYTHONPATH`` pin from
    ``_child_cli_env`` makes importable regardless of provenance.
    """
    manager = cast(WorktreeManager, getattr(manager, "manager", manager))
    destination, _source = manager._venv_paths(tree_path)
    candidate = destination / "bin" / "python"
    if candidate.is_file():
        return os.fspath(candidate)
    return sys.executable


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


def _is_run_leader(pid: int | None) -> bool:
    """True when ``pid`` is alive AND is its own process-group leader.

    Every disposable child is started with ``start_new_session=True``, so its
    process-group id equals its pid.  Requiring that identity turns a bare
    liveness probe into a weak *ownership* probe: a recycled pid (the killed
    run's number, now belonging to an unrelated process) is normally not a
    session leader, so its entry is correctly treated as interrupted instead
    of staying registered — and unreapable — forever.  A pid we cannot
    inspect (``PermissionError``) is not ours either.
    """
    if not _pid_alive(pid):
        return False
    try:
        return os.getpgid(pid) == pid  # type: ignore[arg-type]
    except ProcessLookupError:
        return False
    except PermissionError:
        return False
    except OSError:
        return False


def _run_is_live(record: DisposableRecord) -> bool:
    """True when the record's own run process still exists.

    Deliberately status-blind: a live session-leader pid means the tree may be
    in use, and protecting it costs only a reported "kept" line, while ripping
    a tree out from under a running command is unrecoverable.  A finished entry
    always finalizes with ``pid = None``, so this cannot pin a completed run.
    """
    return _is_run_leader(record.pid)


def _coerce_ceiling_mb(value: object) -> int:  # config-sourced, validated below
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
    for disposable_record in disposable_records:
        current += _tree_size(Path(disposable_record.path))
    projected = _tree_size(manager.main_root) if requested_bytes is None else requested_bytes
    projected = max(0, projected)

    if current + projected <= ceiling:
        return

    # Disposable trees are ordered independently from task trees as required
    # by the feature: a retained failure is inspectable until the next cap hit.
    candidates: list[tuple[float, str, str, Any]] = []
    for disposable_record in disposable_records:
        if _run_is_live(disposable_record):
            continue
        candidates.append(
            (
                disposable_record.created_at,
                "disposable",
                disposable_record.run_id,
                disposable_record,
            )
        )
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


def _worktree_listing(main_root: Path) -> set[str] | None:
    """Return the paths git currently tracks as worktrees (None = unreadable)."""
    result = _git(main_root, "worktree", "list", "--porcelain", check=False)
    if result.returncode != 0:
        return None
    paths: set[str] = set()
    for line in result.stdout.splitlines():
        if not line.startswith("worktree "):
            continue
        raw = line[len("worktree ") :].strip()
        if raw:
            paths.add(_resolved(raw))
    return paths


def _resolved(path: str | os.PathLike[str]) -> str:
    """Normalize a path for comparison without requiring it to exist."""
    try:
        return str(Path(path).resolve())
    except OSError:  # pragma: no cover - resolve() is non-strict
        return str(path)


def _discard_leftover_directory(path: Path) -> bool:
    """Delete a tree directory git no longer tracks; True when it is gone."""
    if not path.exists():
        return True
    try:
        shutil.rmtree(path)
    except OSError:
        return not path.exists()
    return not path.exists()


def _prune_missing_tree_metadata(main_root: Path) -> None:
    """Drop Git metadata for worktrees whose directory is already gone.

    A registry entry can outlive its tree (a manual ``rm -rf``, a host
    cleanup).  Git keeps the admin metadata for a missing worktree until
    ``prune`` runs, and ``git worktree remove`` then fails — so the reap pass
    prunes first and forgets the entry instead of reporting an infrastructure
    failure for a tree that no longer exists.  Best-effort: the entry is
    dropped either way, and a failing prune is logged rather than raised (the
    metadata it would have removed is invisible to users).
    """
    prune = _git(main_root, "worktree", "prune", check=False)
    if prune.returncode != 0:
        detail = prune.stderr.strip() or prune.stdout.strip() or "no diagnostic output"
        logger.warning("disposable worktree prune failed for a missing tree: %s", detail)


def _remove_disposable_tree(main_root: Path, path: Path) -> None:
    """Idempotently reap a disposable tree and prune Git metadata.

    The caller MUST hold the disposable registry lock
    (:meth:`DisposableWorktreeManager._exclusive_lock`) so that the git
    metadata mutations below cannot interleave with another reap in the same
    checkout.

    INT-CI-11: `git worktree remove` exits non-zero for *benign stale* states
    that a parallel repro farm creates by design.  Each of the ``k`` runs
    reaps its own tree and then runs a repo-wide ``git worktree prune``; a
    concurrent reap can delete another run's admin metadata
    (``<git-common-dir>/worktrees/<run-id>``, including
    ``delete_worktrees_dir_if_empty`` removing ``worktrees/`` itself) between
    a command's worktree-list snapshot and its own path resolution, and git
    then aborts with a message naming an admin path that no longer exists
    (``fatal: Invalid path '<common>/worktrees/<run-id>': No such file or
    directory`` — ``strbuf_realpath`` with ``REALPATH_DIE_ON_ERROR``,
    ``abspath.c``).  Reading *git's exit code* as "the reap failed" turned
    that into an infrastructure failure and failed the repro run, so the
    decision is now made from the registry state: a tree git no longer tracks
    is already reaped — remove any directory left behind and continue — and
    only a tree that is still registered (or a directory that cannot be
    deleted) is a real failure.
    """
    result = _git(main_root, "worktree", "remove", "--force", str(path), check=False)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "no diagnostic output"
        registered = _worktree_listing(main_root)
        if registered is None or _resolved(path) in registered:
            raise WorktreeError(f"could not reap disposable worktree {path}: {detail}")
        if not _discard_leftover_directory(path):
            raise WorktreeError(
                f"could not reap disposable worktree {path}: git reported {detail!r} "
                "and the leftover directory could not be deleted"
            )
    prune = _git(main_root, "worktree", "prune", check=False)
    if prune.returncode != 0:
        detail = prune.stderr.strip() or prune.stdout.strip() or "no diagnostic output"
        registered = _worktree_listing(main_root)
        if registered is None or _resolved(path) in registered:
            raise WorktreeError(f"could not prune disposable worktree metadata: {detail}")
        logger.warning(
            "disposable worktree prune reported %r for an already-reaped tree %s — "
            "treating as metadata noise from a concurrent reap",
            detail,
            path,
        )


class DisposableWorktreeManager:
    """Create, execute, and reap detached verification worktrees."""

    def __init__(self, workdir: str | os.PathLike[str] | None = None) -> None:
        self.manager = WorktreeManager(workdir)
        self.main_root = self.manager.main_root
        self.registry_path = self.main_root / ".gitreins" / DISPOSABLE_FILE
        self.lock_path = self.main_root / ".gitreins" / DISPOSABLE_LOCK
        self._lock_state = threading.local()
        defaults = load_defaults(str(self.main_root))
        self.disk_ceiling_mb = _coerce_ceiling_mb(defaults.worktree_disk_ceiling_mb)
        self.manager.worktree_disk_ceiling_mb = self.disk_ceiling_mb

    @contextmanager
    def _exclusive_lock(self) -> Iterator[None]:
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
                # Registered before the child exists: `running` is what the
                # reap pass must see until the run finalizes itself.
                status=STATUS_RUNNING,
            )
            # WORKTREE-007: `enforce_disk_ceiling` above REWRITES the registry
            # when it reaps an older run to make room, so the list read at the
            # top of this method is stale by the time this run registers.
            # Appending to it would resurrect the reaped run as a ghost entry —
            # a registered path that no longer exists, counted by accounting and
            # `--keep` reporting and only cleared by `worktree clean`. Re-read
            # the registry under the same lock (held for the whole create) and
            # register against what is actually on disk.
            records = self._load()
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

    def _reap_and_forget(self, record: DisposableRecord) -> None:
        """Reap one run's tree and drop it from the registry under one lock.

        Holding the registry lock across the git metadata mutations
        serializes removes and prunes inside one checkout — the source of the
        INT-CI-11 reap failure (a parallel repro farm reaping ``k`` trees at
        once).  ``_exclusive_lock`` is re-entrant per thread, so callers that
        already hold it (``reap``, ``create``, ``enforce_disk_ceiling``) can
        call this safely.
        """
        with self._exclusive_lock():
            _remove_disposable_tree(self.main_root, Path(record.path))
            self._save([item for item in self._load() if item.run_id != record.run_id])

    def reap(self, *, run_id: str | None = None) -> list[str]:
        """Reap disposable records, optionally selecting one run id.

        Returns the run ids dropped from the registry (trees removed now, plus
        entries whose tree was already gone).  The strict contract is
        preserved: a registered tree that git still tracks and that cannot be
        removed raises :class:`WorktreeError` rather than being silently
        forgotten.  Use :meth:`reap_report` when a single unreapable tree must
        not abort the whole pass.
        """
        with self._exclusive_lock():
            return self._reap_locked(run_id=run_id, tolerate_failures=False).removed()

    def reap_report(self, *, run_id: str | None = None) -> DisposableReapReport:
        """Reap disposable records and report EVERY outcome (DF-GITREINS-POC-80).

        Same decisions as :meth:`reap`, but nothing is silent and nothing is
        fatal: a tree that cannot be removed is reported and left registered,
        a live run is reported as kept, an interrupted run is reaped and
        reported as abandoned.  This is what ``worktree clean`` uses — a kept
        tree can no longer sit in the registry while clean prints only
        "Nothing to reap."
        """
        with self._exclusive_lock():
            return self._reap_locked(run_id=run_id, tolerate_failures=True)

    def _reap_locked(self, *, run_id: str | None, tolerate_failures: bool) -> DisposableReapReport:
        """Shared reap pass; the caller holds the disposable registry lock."""
        records = self._load()
        if run_id is not None and not any(record.run_id == run_id for record in records):
            return DisposableReapReport()
        now = time.time()
        report = DisposableReapReport()
        remaining: list[DisposableRecord] = []
        for record in records:
            if run_id is not None and record.run_id != run_id:
                remaining.append(record)
                continue
            decision = _decide_reap(
                record,
                now=now,
                tree_exists=Path(record.path).is_dir(),
                run_live=_run_is_live(record),
            )
            if decision.action == "keep":
                remaining.append(record)
                report.kept.append((record.run_id, record.status, decision.reason))
                continue
            if decision.action == "forget":
                _prune_missing_tree_metadata(self.main_root)
                report.forgotten.append(record.run_id)
                continue
            try:
                _remove_disposable_tree(self.main_root, Path(record.path))
            except WorktreeError as exc:
                if not tolerate_failures:
                    # Strict mode: leave the registry untouched on disk (as it
                    # was before this pass) and fail loudly.
                    raise
                remaining.append(record)
                report.kept.append((record.run_id, record.status, str(exc)))
                continue
            report.reaped.append(record.run_id)
            if decision.interrupted:
                report.abandoned.append(record.run_id)
        self._save(remaining)
        return report

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
                    ["sh", "-c", command],  # noqa: S607 - sh -c wrapper; command from disposable-worktree record
                    cwd=record.path,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    start_new_session=True,
                    env=_child_command_env(Path(record.path), self),
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
            if keep:
                # Finalize the entry with its outcome.  A kept tree that a
                # later `worktree clean` reads is now distinguishable from a
                # run that is still in flight, and the failure it preserves
                # carries its own exit code (DF-GITREINS-POC-80).  `exit_code`
                # stays -1 for a run that never started or timed out: an
                # unfinished artifact, recorded as a kept failure.
                record.status = _finished_status(exit_code)
                record.exit_code = exit_code
            record.pid = None
            if not keep:
                self._reap_and_forget(record)
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
        with an interpreter that can import the harness package, rather than
        relying on a console script or a ``gitreins.__main__`` module that may
        not exist in a source checkout.

        DF-GITREINS-POC-71: each step runs in a NORMALIZED child environment
        (see the module's child-environment contract) — the step runner
        prefers the tree's own venv python over ``sys.executable`` so the
        interpreter matches the toolchain captured at the tree's HEAD, and
        pins ``PYTHONPATH`` to the harness root (replacing any session value)
        so the CLI boots regardless of what interpreter or ``PYTHONPATH`` the
        consumer session carries. The raw parent environment is never
        forwarded: a foreign runtime python on the session PATH would
        otherwise die with ``ModuleNotFoundError: No module named 'engine'``
        and the whole dogfood run would report init failed.
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
                # own CLI script. The interpreter comes from the tree's venv
                # when one is linked (sys.executable otherwise), and the env
                # carries the pinned PYTHONPATH so either can import the
                # harness package from the tree's CWD (DF-GITREINS-POC-71).
                cli_entry = Path(__file__).resolve().parents[1] / "gitreins" / "cli.py"
                result = subprocess.run(
                    [_child_python(Path(record.path), self), os.fspath(cli_entry), *arguments],
                    cwd=record.path,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    check=False,
                    env=_child_cli_env(Path(record.path), self),
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
            if keep:
                # Same finalization contract as `run` (DF-GITREINS-POC-80): a
                # retained dogfood tree records its outcome and status.
                record.status = _finished_status(1 if failed else 0)
                record.exit_code = 1 if failed else 0
            record.pid = None
            if not keep:
                self._reap_and_forget(record)
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
