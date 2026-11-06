"""
Disk-backed background evaluation job store (DF-006).

Async judge jobs survive MCP server restarts and CLI process exits. Each
job is a JSON file in a shared jobs directory (default
``~/.local/share/gitreins/jobs/``, override with ``GITREINS_JOB_DIR``),
written atomically (tmp file + ``os.replace``).

Both the MCP server and the CLI async worker share this store, so a job
started by ``gitreins judge --async`` can be polled by ``judge.status``
on the MCP server and vice versa. A job record carries its own workdir,
so a single global store works for cross-repo jobs.

Record shape::

    {
        "id": "job-<hex>",
        "status": "running" | "complete" | "error",
        "running": bool,          # additive: True while dispatched/running,
                                  # False once terminal (complete | error);
                                  # records from older builds lack the key
                                  # and are treated as NOT running
        "task_id": str,
        "workdir": str,
        "result": dict | None,    # judge result dict (see engine.judge.judge_result_to_dict)
        "error": str | None,
        "started_at": float,
        "finished_at": float | None,
        "pid": int | None,        # process owning the run (None/0 = unknown/dead)
        "worker_pid": int | None, # process actually running the evaluation
        "worker_kind": str,       # "process" (detached worker) | "thread"
        "worker_thread": str,     # thread name, threaded workers only
        "worker_started_at": float,
        "heartbeat_at": float,    # refreshed by the worker while it evaluates
        "caps": dict | None,      # captured EvalCap primitives (None = config-driven)
    }

``worker_pid`` is the EVALUATION WORKER's pid, not the dispatcher's: for
``judge --async`` it is the detached ``judge --run-job`` child, and it is
never evidence that a job is alive on its own. Liveness is the heartbeat —
see :func:`worker_liveness` (JUDGE-JOB-001).
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from typing import TYPE_CHECKING, Any, cast

logger = logging.getLogger("gitreins.job_store")

DEFAULT_JOB_DIR = os.path.expanduser("~/.local/share/gitreins/jobs")
JobRecord = dict[str, Any]


def job_dir(directory: str | None = None) -> str:
    """Return the shared jobs directory, creating it if needed.

    Callers that dispatch work asynchronously may pass the directory captured
    at dispatch time so a later process-global environment change cannot move
    terminal writes to another store.
    """
    d = directory if directory is not None else os.environ.get("GITREINS_JOB_DIR", DEFAULT_JOB_DIR)
    os.makedirs(d, exist_ok=True)
    return d


def job_path(job_id: str, directory: str | None = None) -> str:
    return os.path.join(job_dir(directory), f"{job_id}.json")


def job_log_path(job_id: str, directory: str | None = None) -> str:
    """Path to the worker process log for a job (stdout/stderr of the run)."""
    return os.path.join(job_dir(directory), f"{job_id}.log")


def new_job_id() -> str:
    return f"job-{uuid.uuid4().hex}"


def make_job(task_id: str, workdir: str, caps: JobRecord | None = None) -> JobRecord:
    """Create a fresh running job record (not yet persisted)."""
    return {
        "id": new_job_id(),
        "status": "running",
        "running": True,
        "task_id": task_id,
        "workdir": os.path.abspath(workdir),
        "result": None,
        "error": None,
        "started_at": time.time(),
        "finished_at": None,
        "pid": None,
        "caps": caps,
    }


def save_job(job: JobRecord, directory: str | None = None) -> None:
    """Persist a job record atomically (tmp file + os.replace)."""
    path = job_path(job["id"], directory)
    tmp = f"{path}.tmp-{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(job, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def load_job(job_id: str, directory: str | None = None) -> JobRecord | None:
    """Load a job record from disk, or None if it doesn't exist."""
    path = job_path(job_id, directory)
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return cast(JobRecord, json.load(f))
    except (json.JSONDecodeError, OSError) as e:
        # A torn write would only happen if the process was killed mid-
        # replace — treat as missing rather than crashing status polls.
        logger.warning("job %s unreadable: %s", job_id, e)
        return None


def list_jobs(directory: str | None = None) -> list[JobRecord]:
    """Return all job records on disk (newest first)."""
    jobs: list[JobRecord] = []
    d = job_dir(directory)
    if not os.path.isdir(d):
        return jobs
    for name in os.listdir(d):
        if not name.endswith(".json"):
            continue
        job = load_job(name[:-5], directory)
        if job:
            jobs.append(job)
    jobs.sort(key=lambda j: j.get("started_at", 0.0), reverse=True)
    return jobs


def delete_job(job_id: str, directory: str | None = None) -> bool:
    """Delete a job record; returns True if it existed."""
    path = job_path(job_id, directory)
    if os.path.exists(path):
        os.remove(path)
        return True
    return False


def find_running_job(
    task_id: str,
    workdir: str,
    directory: str | None = None,
    *,
    reap: bool = True,
) -> JobRecord | None:
    """Return the newest LIVE running job record for (task_id, workdir), if any.

    Single-flight key (GR-GAP-046): at most ONE in-flight evaluation per
    (task_id, workdir) across ALL processes sharing the disk store. Any
    record still in ``running`` state blocks a new dispatch — a completed
    or error job for the same task is superseded (new run), a running one
    is reused. Dispatching a second job for a live key would multiply
    evaluations (the exact bug this guards against).

    Liveness is the WORKER's, never the dispatcher's (JUDGE-JOB-001): a
    ``running`` record whose worker pid is gone, or whose heartbeat stopped
    longer ago than the stale cap, is an orphan — nothing will ever write a
    terminal status for it, so it is reaped to ``error`` HERE (under the
    per-job resume lease) and the key is free for the next dispatch. Before
    this, a record written by the MCP server carried the SERVER's pid: the
    server stayed alive, so ``judge <id>`` refused with "Evaluation already
    in progress" forever against a job nobody was running.
    """
    wd = os.path.abspath(workdir)
    for job in list_jobs(directory):
        if job.get("task_id") != task_id:
            continue
        if os.path.abspath(job.get("workdir", "")) != wd:
            continue
        if job.get("status") != "running":
            continue
        live, why = worker_liveness(job)
        if live:
            return job
        logger.warning("job %s for task %s is orphaned (%s) — reaping", job["id"], task_id, why)
        if reap:
            reap_stale_job(job["id"], directory=directory, reason=why)
        # Re-read: another poller may have reaped it first; either way the
        # record is terminal now and must not block this key.
    return None


def acquire_resume_lease(job_id: str, directory: str | None = None) -> int | None:
    """Atomically claim the right to resume a job (cross-process).

    An exclusive ``fcntl.flock`` on a per-job lock file (``<job_id>.lock``
    next to the job record) makes the resume check-then-act atomic: the
    claim (re-read → set pid → save) happens while holding the lease, so
    two server instances polling the same orphaned job cannot both
    re-dispatch a worker. Non-blocking: returns the open fd (pass to
    ``release_resume_lease``) or None if another process/thread holds the
    lease right now.
    """
    import fcntl

    lock_path = os.path.join(job_dir(directory), f"{job_id}.lock")
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


def release_resume_lease(fd: int) -> None:
    """Release a lease acquired by ``acquire_resume_lease``."""
    import fcntl

    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


# ── EvalCap (de)serialization ───────────────────────────────────────────────

if TYPE_CHECKING:
    from engine.eval_cap import EvalCap

_CAP_FIELDS = (
    "max_iterations",
    "max_seconds",
    "max_input_tokens",
    "max_output_tokens",
    "tool_call_weight",
)


def cap_to_dict(cap: EvalCap | None) -> JobRecord | None:
    """Serialize an EvalCap to its primitive fields (None → None)."""
    if cap is None:
        return None
    return {f: getattr(cap, f) for f in _CAP_FIELDS}


def cap_from_dict(d: JobRecord | None) -> EvalCap | None:
    """Rebuild an EvalCap from a cap_to_dict payload (None → None)."""
    if not d:
        return None
    from engine.eval_cap import EvalCap

    return EvalCap(
        max_iterations=float(d.get("max_iterations", -1.0)),
        max_seconds=float(d.get("max_seconds", -1.0)),
        max_input_tokens=int(d.get("max_input_tokens", -1)),
        max_output_tokens=int(d.get("max_output_tokens", -1)),
        tool_call_weight=float(d.get("tool_call_weight", 0.1)),
    )


def pid_alive(pid: int | None) -> bool:
    """True if pid looks like a live process (None/0/invalid → False)."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by another user
    except OSError:
        return False


# ── Worker identity and liveness (JUDGE-JOB-001) ────────────────────────────
#
# A job record used to be "alive" whenever ``pid`` was alive, but the pid the
# MCP dispatcher wrote was the SERVER's: the evaluation could be gone and the
# record still looked running, so ``gitreins judge <id>`` refused with
# "Evaluation already in progress" against a job nobody was running, and
# ``judge --status`` never reported the death.
#
# Liveness belongs to the WORKER:
#
#   pid                    owning process (kept for older readers)
#   worker_pid             process running the evaluation
#   worker_kind            "process" (detached judge worker) | "thread"
#   worker_thread          thread name, threaded workers only
#   worker_started_at      when the worker claimed the job
#   heartbeat_at           refreshed by the worker every
#                          ``heartbeat_interval()`` seconds while it evaluates
#
# A record is live only while that worker keeps reporting. A dead worker pid,
# or a heartbeat older than ``heartbeat_stale_seconds()``, is an orphan and is
# reaped to a terminal ``error`` record naming the death (and why no verdict
# will appear) on the next :func:`find_running_job` / status poll.

WORKER_KIND_PROCESS = "process"
WORKER_KIND_THREAD = "thread"

DEFAULT_HEARTBEAT_SECONDS = 15.0
DEFAULT_HEARTBEAT_STALE_SECONDS = 180.0


def heartbeat_interval(seconds: float | None = None) -> float:
    """Seconds between worker heartbeats (env GITREINS_JOB_HEARTBEAT_SECONDS)."""
    if seconds is not None:
        return max(0.05, float(seconds))
    raw = os.environ.get("GITREINS_JOB_HEARTBEAT_SECONDS")
    if raw:
        try:
            return max(0.05, float(raw))
        except ValueError:
            pass
    return DEFAULT_HEARTBEAT_SECONDS


def heartbeat_stale_seconds(seconds: float | None = None) -> float:
    """Age after which a silent heartbeat is an orphan (env override for tests)."""
    if seconds is not None:
        return max(0.0, float(seconds))
    raw = os.environ.get("GITREINS_JOB_HEARTBEAT_STALE_SECONDS")
    if raw:
        try:
            return max(0.0, float(raw))
        except ValueError:
            pass
    return DEFAULT_HEARTBEAT_STALE_SECONDS


def worker_identity(kind: str = WORKER_KIND_PROCESS, pid: int | None = None) -> JobRecord:
    """Worker-identity fields for the CALLING process/thread.

    Stamped on the job record when a worker claims it, so the record names the
    evaluation worker instead of the process that dispatched it.
    """
    now = time.time()
    worker_pid = os.getpid() if pid is None else int(pid)
    ident: JobRecord = {
        "worker_pid": worker_pid,
        "worker_kind": kind,
        "worker_started_at": now,
        "heartbeat_at": now,
    }
    if kind == WORKER_KIND_THREAD:
        ident["worker_thread"] = threading.current_thread().name
    return ident


def worker_liveness(job: JobRecord, now: float | None = None) -> tuple[bool, str]:
    """Return ``(live, reason)`` for a job record's evaluation worker.

    Live means the record is still ``running`` AND its worker is reporting:
    the worker pid is alive (when the record names one) and the heartbeat is
    no older than the stale cap. The dispatcher's pid is never evidence of
    life (JUDGE-JOB-001). Records written before worker identity existed keep
    the historical pid-only reading — a missing pid is not proof of death.
    """
    now = time.time() if now is None else now
    status = str(job.get("status", ""))
    if status != "running":
        return False, f"job is {status or 'unknown'}, not running"

    worker_pid = job.get("worker_pid", job.get("pid"))
    if isinstance(worker_pid, int) and not isinstance(worker_pid, bool) and worker_pid > 0:
        if not pid_alive(worker_pid):
            return False, (
                f"worker process died before completing (pid {worker_pid}); no verdict was written"
            )

    heartbeat = job.get("heartbeat_at")
    if isinstance(heartbeat, (int, float)) and not isinstance(heartbeat, bool):
        age = max(0.0, now - float(heartbeat))
        cap = heartbeat_stale_seconds()
        if age > cap:
            return False, (
                f"worker heartbeat is stale: last update {age:.0f}s ago "
                f"(cap {cap:.0f}s), worker {worker_pid or 'unknown'} stopped "
                "reporting (no verdict was written)"
            )
        return True, f"worker {worker_pid or 'unknown'} alive, heartbeat {age:.0f}s old"

    if isinstance(worker_pid, int) and worker_pid > 0:
        return True, f"legacy record, pid {worker_pid} alive (no heartbeat recorded)"
    return True, "legacy record with no worker identity (no heartbeat recorded)"


def worker_identified(job: JobRecord) -> bool:
    """True when the record names a worker (a pid or a heartbeat).

    Records written before worker identity existed have neither: liveness
    cannot be verified for them, and they are neither proof of life nor proof
    of death. Observability counts them as unverified; the reap path does not
    flip them (there is nothing to attest that the worker died).
    """
    if isinstance(job.get("heartbeat_at"), (int, float)) and not isinstance(
        job.get("heartbeat_at"), bool
    ):
        return True
    pid = job.get("worker_pid", job.get("pid"))
    return isinstance(pid, int) and not isinstance(pid, bool) and pid > 0


def reap_stale_job(
    job_id: str,
    directory: str | None = None,
    reason: str | None = None,
) -> JobRecord | None:
    """Flip an orphaned ``running`` record to a terminal ``error`` record.

    The flip happens under the per-job resume lease, the same cross-process
    lock the resume path uses, so a poll, a dispatch and a live worker's
    heartbeat cannot interleave: a heartbeat that arrives first keeps the job
    live, a reap that arrives first ends it. Returns the resulting record
    (terminal, or still running when another poller owns the lease), or None
    when the record does not exist.
    """
    lease = acquire_resume_lease(job_id, directory=directory)
    if lease is None:
        return load_job(job_id, directory=directory)
    try:
        current = load_job(job_id, directory=directory)
        if current is None or current.get("status") != "running":
            return current
        live, why = worker_liveness(current)
        if live:
            return current
        current["status"] = "error"
        current["running"] = False
        current["error"] = reason or why
        current["error_kind"] = "orphaned_worker"
        current["finished_at"] = time.time()
        save_job(current, directory=directory)
        logger.warning("reaped orphaned job %s: %s", job_id, current["error"])
        try:
            # OBS-GITREINS-2: the closed-set reason ``orphaned`` has exactly
            # this producer — the reap. Best effort, like the CLI path.
            from engine.cli_metrics import note_orphan_reap

            note_orphan_reap()
        except Exception:  # noqa: BLE001 — metrics must never fail a reap
            pass
        return current
    finally:
        release_resume_lease(lease)


class JobHeartbeat:
    """Keep a running job record's heartbeat fresh while its worker evaluates.

    One daemon thread per running job; each beat re-reads the record under the
    resume lease (so it can never resurrect a record a reaper just ended),
    stamps ``heartbeat_at`` and stops as soon as the record stops being
    ``running``. ``stop()`` joins the thread, so the worker's terminal write
    always lands AFTER the last beat.
    """

    def __init__(
        self,
        job_id: str,
        directory: str | None = None,
        interval: float | None = None,
        extra: JobRecord | None = None,
    ) -> None:
        self.job_id = job_id
        self.directory = directory
        self.interval = heartbeat_interval(interval)
        self.extra = dict(extra or {})
        self.beats = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def beat(self) -> bool:
        """Write one heartbeat; returns True when the record was stamped."""
        lease = acquire_resume_lease(self.job_id, directory=self.directory)
        if lease is None:
            return False
        try:
            job = load_job(self.job_id, directory=self.directory)
            if job is None or job.get("status") != "running":
                return False
            job["heartbeat_at"] = time.time()
            job.update(self.extra)
            try:
                save_job(job, directory=self.directory)
            except OSError as e:  # pragma: no cover — disk full/unwritable store
                logger.warning("heartbeat save failed for %s: %s", self.job_id, e)
                return False
            return True
        finally:
            release_resume_lease(lease)

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            if self.beat():
                self.beats += 1

    def start(self) -> "JobHeartbeat":
        if self._thread is None:
            self._thread = threading.Thread(
                target=self._run,
                name=f"job-heartbeat-{self.job_id}",
                daemon=True,
            )
            self._thread.start()
        return self

    def stop(self, timeout: float = 10.0) -> None:
        """Stop beating and wait for the last beat to land (best effort).

        ``join`` is looked up defensively: test harnesses substitute their own
        thread objects, and a missing join must never turn a terminal job write
        into an exception.
        """
        self._stop.set()
        thread = self._thread
        self._thread = None
        if thread is None or thread is threading.current_thread():
            return
        join = getattr(thread, "join", None)
        if callable(join):
            try:
                join(timeout)
            except RuntimeError:  # pragma: no cover — thread never started
                pass

    def __enter__(self) -> "JobHeartbeat":
        return self.start()

    def __exit__(self, *_exc: object) -> None:
        self.stop()
