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
        "task_id": str,
        "workdir": str,
        "result": dict | None,    # judge result dict (see engine.judge.judge_result_to_dict)
        "error": str | None,
        "started_at": float,
        "finished_at": float | None,
        "pid": int | None,        # process owning the run (None/0 = unknown/dead)
        "caps": dict | None,      # captured EvalCap primitives (None = config-driven)
    }
"""

import json
import logging
import os
import time
import uuid

logger = logging.getLogger("gitreins.job_store")

DEFAULT_JOB_DIR = os.path.expanduser("~/.local/share/gitreins/jobs")


def job_dir() -> str:
    """Return the shared jobs directory, creating it if needed."""
    d = os.environ.get("GITREINS_JOB_DIR", DEFAULT_JOB_DIR)
    os.makedirs(d, exist_ok=True)
    return d


def job_path(job_id: str) -> str:
    return os.path.join(job_dir(), f"{job_id}.json")


def job_log_path(job_id: str) -> str:
    """Path to the worker process log for a job (stdout/stderr of the run)."""
    return os.path.join(job_dir(), f"{job_id}.log")


def new_job_id() -> str:
    return f"job-{uuid.uuid4().hex}"


def make_job(task_id: str, workdir: str, caps: dict | None = None) -> dict:
    """Create a fresh running job record (not yet persisted)."""
    return {
        "id": new_job_id(),
        "status": "running",
        "task_id": task_id,
        "workdir": os.path.abspath(workdir),
        "result": None,
        "error": None,
        "started_at": time.time(),
        "finished_at": None,
        "pid": None,
        "caps": caps,
    }


def save_job(job: dict) -> None:
    """Persist a job record atomically (tmp file + os.replace)."""
    path = job_path(job["id"])
    tmp = f"{path}.tmp-{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(job, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def load_job(job_id: str) -> dict | None:
    """Load a job record from disk, or None if it doesn't exist."""
    path = job_path(job_id)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        # A torn write would only happen if the process was killed mid-
        # replace — treat as missing rather than crashing status polls.
        logger.warning("job %s unreadable: %s", job_id, e)
        return None


def list_jobs() -> list[dict]:
    """Return all job records on disk (newest first)."""
    jobs = []
    if not os.path.isdir(job_dir()):
        return jobs
    for name in os.listdir(job_dir()):
        if not name.endswith(".json"):
            continue
        job = load_job(name[:-5])
        if job:
            jobs.append(job)
    jobs.sort(key=lambda j: j.get("started_at", 0.0), reverse=True)
    return jobs


def delete_job(job_id: str) -> bool:
    """Delete a job record; returns True if it existed."""
    path = job_path(job_id)
    if os.path.exists(path):
        os.remove(path)
        return True
    return False


def find_running_job(task_id: str, workdir: str) -> dict | None:
    """Return the newest running job record for (task_id, workdir), if any.

    Single-flight key (GR-GAP-046): at most ONE in-flight evaluation per
    (task_id, workdir) across ALL processes sharing the disk store. Any
    record still in ``running`` state blocks a new dispatch — a completed
    or error job for the same task is superseded (new run), a running one
    is reused. ``pid`` is deliberately NOT consulted: a ``running`` record
    with a dead/None pid is an orphan that the resume path re-dispatches
    on the next poll, so dispatching a second job for the same key would
    multiply evaluations (the exact bug this guards against).
    """
    wd = os.path.abspath(workdir)
    for job in list_jobs():
        if job.get("task_id") != task_id:
            continue
        if os.path.abspath(job.get("workdir", "")) != wd:
            continue
        if job.get("status") == "running":
            return job
    return None


def acquire_resume_lease(job_id: str) -> int | None:
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

    lock_path = os.path.join(job_dir(), f"{job_id}.lock")
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

_CAP_FIELDS = (
    "max_iterations",
    "max_seconds",
    "max_input_tokens",
    "max_output_tokens",
    "tool_call_weight",
)


def cap_to_dict(cap) -> dict | None:
    """Serialize an EvalCap to its primitive fields (None → None)."""
    if cap is None:
        return None
    return {f: getattr(cap, f) for f in _CAP_FIELDS}


def cap_from_dict(d: dict | None):
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


def pid_alive(pid) -> bool:
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
