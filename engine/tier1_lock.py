"""Per-repo tier1 tests lock (INT-FLAKE-6).

Two or more tier1 runs (concurrent judges) in the SAME workdir used to run the
full suite against each other: the async-job tests red on a tree that passes
alone (verdict 8435b664 recorded two contention failures against a tree whose
solo full-suite run was 2487 passed / 0 failed). ``tests/conftest.py`` already
flock-serializes only the ``live``-marked tests; this module extends the same
shape to the WHOLE tier1 suite.

Mechanics:
- One lock FILE outside the repo (/tmp), keyed by a digest of the repo root, so
  different repos never contend and a run never writes an in-tree file.
- Acquired BLOCKING around the tier1 tests step: the loser waits for the
  winner, so every concurrent run grades the tree to completion instead of
  failing on contention. ``judge --async`` is detached and tier2 runs after
  tier1, so a delayed tests step cannot expire an evaluation's own budget
  (EvalCap.max_seconds applies from the eval's start, and the tests step is
  the first thing it does).
- ``LOCK_WAIT_BUDGET_S`` bounds the wait as a safety net (90 min): a hung
  winner must not wedge a loser forever. On expiry the step still runs —
  degraded, not silently dropped.
- Non-tier1 callers (``gitreins guard``, manual pytest) do NOT take the lock:
  the guard is hook-latency-sensitive and concurrent guard runs of DIFFERENT
  repos must never queue behind each other; two guards of the same repo are a
  human action, not a judge fleet.

Attribution: the acquisition result is stamped on the step's data
(``tier1_tests_lock``: acquired|waited|wait-expired) so a verdict shows the
run serialized behind another tier1 instead of silently hiding it.
"""

from __future__ import annotations

import fcntl
import hashlib
import os
import tempfile
import time

from engine.pipeline import TIER1_ENV_VAR

#: Wait bound for the lock (safety net against a hung winner; 90 min).
LOCK_WAIT_BUDGET_S = 90.0 * 60

#: fd of the held lock (module-level because acquire/release are separate calls).
_LOCK_FD: int | None = None

#: Last acquisition result, for callers that stamp it into step data.
LAST_ACQUIRE_RESULT: str | None = None


def tier1_tests_lock_path() -> str:
    """Per-repo lock file, OUTSIDE the repo (a run never writes an in-tree file).

    Keyed by the repo root so two checkouts of different repos never contend,
    while two concurrent tier1 runs of THIS repo do (same shape as
    ``tests/conftest._live_lock_path`` — one digest function, two callers).
    """
    repo_root = os.environ.get("GITREINS_TIER1_REPO_ROOT") or os.getcwd()
    digest = hashlib.sha1(os.path.abspath(repo_root).encode("utf-8")).hexdigest()[:12]
    return os.path.join(tempfile.gettempdir(), f"gitreins-tier1-{digest}.lock")


def in_tier1() -> bool:
    """True when this process is a judge's tier1 tests step (env-stamped)."""
    return os.environ.get(TIER1_ENV_VAR) == "1"


def acquire() -> str | None:
    """Take the per-repo tier1 tests lock, blocking until acquired.

    Returns the acquisition result for attribution — ``"acquired"`` when the
    lock was free, ``"waited"`` when another tier1 run held it and released,
    ``"wait-expired"`` when the safety budget ran out (the step still runs) —
    or None when the caller is not a tier1 tests step (no lock taken). Never
    raises: a locking failure must not fail a gate that would otherwise run.
    """
    global _LOCK_FD, LAST_ACQUIRE_RESULT
    if not in_tier1():
        return None
    try:
        path = tier1_tests_lock_path()
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        deadline = time.monotonic() + LOCK_WAIT_BUDGET_S
        waited = False
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                waited = True
                if time.monotonic() >= deadline:
                    os.close(fd)
                    LAST_ACQUIRE_RESULT = "wait-expired"
                    return LAST_ACQUIRE_RESULT
                time.sleep(0.5)
        _LOCK_FD = fd
        LAST_ACQUIRE_RESULT = "waited" if waited else "acquired"
        return LAST_ACQUIRE_RESULT
    except OSError:
        LAST_ACQUIRE_RESULT = None
        return None


def release() -> None:
    """Release the lock taken by :func:`acquire` (idempotent, never raises)."""
    global _LOCK_FD, LAST_ACQUIRE_RESULT
    if _LOCK_FD is None:
        return
    fd, _LOCK_FD = _LOCK_FD, None
    LAST_ACQUIRE_RESULT = None
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
