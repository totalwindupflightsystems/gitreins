"""
Unit tests for engine/job_store.py — the disk-backed job store (DF-006).
"""

import os

import pytest

from engine.eval_cap import EvalCap
from engine.job_store import (
    WORKER_KIND_PROCESS,
    WORKER_KIND_THREAD,
    JobHeartbeat,
    cap_from_dict,
    cap_to_dict,
    delete_job,
    find_running_job,
    heartbeat_stale_seconds,
    job_dir,
    list_jobs,
    load_job,
    make_job,
    pid_alive,
    reap_stale_job,
    save_job,
    worker_identity,
    worker_liveness,
)
from pathlib import Path


@pytest.fixture
def store_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> object:
    d = str(tmp_path / "jobs")
    monkeypatch.setenv("GITREINS_JOB_DIR", d)
    return d


def test_save_load_roundtrip(store_dir: object) -> None:
    job = make_job("task-1", "/tmp/some/repo")
    job["pid"] = 1234
    save_job(job)

    loaded = load_job(job["id"])
    assert loaded is not None
    assert loaded["id"] == job["id"]
    assert loaded["task_id"] == "task-1"
    assert loaded["workdir"] == "/tmp/some/repo"
    assert loaded["status"] == "running"
    assert loaded["running"] is True
    assert loaded["pid"] == 1234
    assert loaded["result"] is None
    assert loaded["caps"] is None


def test_load_missing_returns_none(store_dir: object) -> None:
    assert load_job("job-nope") is None


def test_list_jobs_roundtrip(store_dir: object) -> None:
    a = make_job("t1", "/r")
    b = make_job("t2", "/r")
    save_job(a)
    save_job(b)
    ids = {j["id"] for j in list_jobs()}
    assert ids == {a["id"], b["id"]}


def test_delete_job(store_dir: object) -> None:
    job = make_job("t1", "/r")
    save_job(job)
    assert delete_job(job["id"]) is True
    assert load_job(job["id"]) is None
    assert delete_job(job["id"]) is False


def test_save_is_atomic_no_tmp_leftovers(store_dir: object) -> None:
    job = make_job("t1", "/r")
    save_job(job)
    leftovers = [n for n in os.listdir(job_dir()) if ".tmp" in n]
    assert leftovers == []


def test_corrupt_job_file_returns_none(store_dir: object) -> None:
    job = make_job("t1", "/r")
    save_job(job)
    with open(os.path.join(job_dir(), job["id"] + ".json"), "w") as f:
        f.write("{not json")
    assert load_job(job["id"]) is None


def test_cap_roundtrip() -> None:
    cap = EvalCap(
        max_iterations=40.0,
        max_seconds=600.0,
        max_input_tokens=200_000,
        max_output_tokens=50_000,
        tool_call_weight=0.1,
    )
    rebuilt = cap_from_dict(cap_to_dict(cap))
    assert rebuilt is not None
    assert rebuilt.max_iterations == 40.0
    assert rebuilt.max_seconds == 600.0
    assert rebuilt.max_input_tokens == 200_000
    assert rebuilt.max_output_tokens == 50_000
    assert rebuilt.tool_call_weight == 0.1


def test_cap_none_roundtrip() -> None:
    assert cap_to_dict(None) is None
    assert cap_from_dict(None) is None
    assert cap_from_dict({}) is None


def test_pid_alive() -> None:
    assert pid_alive(None) is False
    assert pid_alive(0) is False
    assert pid_alive(-5) is False
    assert pid_alive(True) is False
    assert pid_alive(os.getpid()) is True
    # 99999999 is (practically) never a live pid
    assert pid_alive(99999999) is False


# ── GR-GAP-046: single-flight key lookup ─────────────────────────────────────


def test_find_running_job_matches_task_and_workdir(store_dir: object) -> None:
    """find_running_job returns a LIVE running job for the same (task, workdir)."""
    job = make_job("t1", "/r")
    job.update(worker_identity(WORKER_KIND_PROCESS))  # this pytest process: alive
    save_job(job)
    found = find_running_job("t1", "/r")
    assert found is not None
    assert found["id"] == job["id"]


def test_find_running_job_normalizes_workdir(store_dir: object) -> None:
    """Workdir comparison is abspath-normalized (trailing slash, symlinks)."""
    job = make_job("t1", "/r/sub")
    save_job(job)
    assert find_running_job("t1", "/r/sub/") is not None


def test_find_running_job_ignores_completed_and_error(store_dir: object) -> None:
    """A completed/error job for the same task is superseded (new run)."""
    done = make_job("t1", "/r")
    done["status"] = "complete"
    done["finished_at"] = 1.0
    save_job(done)
    failed = make_job("t1", "/r")
    failed["status"] = "error"
    failed["finished_at"] = 2.0
    save_job(failed)
    assert find_running_job("t1", "/r") is None


def test_find_running_job_scoped_to_task_and_workdir(store_dir: object) -> None:
    """Different task id or different workdir does not collide."""
    a = make_job("t1", "/r")
    save_job(a)
    b = make_job("t2", "/r")
    save_job(b)
    c = make_job("t1", "/other")
    save_job(c)
    found = find_running_job("t1", "/r")
    assert found is not None and found["id"] == a["id"]
    assert find_running_job("t2", "/r")["id"] == b["id"]
    assert find_running_job("t1", "/other")["id"] == c["id"]


def test_find_running_job_returns_newest_of_duplicates(store_dir: object) -> None:
    """If multiple running records exist (legacy), the newest wins."""
    old = make_job("t1", "/r")
    old["started_at"] = 1.0
    save_job(old)
    new = make_job("t1", "/r")
    new["started_at"] = 2.0
    save_job(new)
    found = find_running_job("t1", "/r")
    assert found["id"] == new["id"]


# ── JUDGE-JOB-001: worker identity, heartbeat, orphan reaping ────────────────
#
# The bug: the MCP dispatcher recorded ITS OWN pid on the job record, so a
# record whose evaluation was long gone still looked live (the server outlives
# the job) and `gitreins judge <id>` refused with "Evaluation already in
# progress" forever. Liveness now belongs to the worker: a dead worker pid or a
# heartbeat older than the stale cap is an orphan, reaped to terminal `error`.


def _dead_pid() -> int:
    """A pid that certainly does not exist (a reaped child)."""
    import subprocess
    import sys

    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def test_worker_identity_names_the_worker(store_dir: object) -> None:
    """worker_identity() stamps the worker pid/kind/heartbeat, not a dispatcher."""
    job = make_job("t1", "/r")
    job["pid"] = 4242  # dispatcher's pid, kept for older readers
    job.update(worker_identity(WORKER_KIND_PROCESS, pid=os.getpid()))
    assert job["worker_pid"] == os.getpid()
    assert job["worker_pid"] != job["pid"]
    assert job["worker_kind"] == WORKER_KIND_PROCESS
    assert job["heartbeat_at"] == job["worker_started_at"]
    threaded = worker_identity(WORKER_KIND_THREAD)
    assert threaded["worker_kind"] == WORKER_KIND_THREAD
    assert threaded["worker_thread"]


def test_worker_liveness_rejects_dead_worker(store_dir: object) -> None:
    """A running record whose worker pid is gone is NOT live, and says why."""
    job = make_job("t1", "/r")
    job.update(worker_identity(WORKER_KIND_PROCESS, pid=_dead_pid()))
    live, why = worker_liveness(job)
    assert live is False
    assert "died before completing" in why
    assert "no verdict was written" in why


def test_worker_liveness_rejects_stale_heartbeat_with_live_pid(store_dir: object) -> None:
    """The incident shape: pid alive (the server), worker gone (heartbeat stale)."""
    job = make_job("t1", "/r")
    job.update(worker_identity(WORKER_KIND_PROCESS, pid=os.getpid()))
    job["heartbeat_at"] = 1.0  # long ago
    live, why = worker_liveness(job)
    assert live is False
    assert "heartbeat is stale" in why
    assert "no verdict was written" in why


def test_worker_liveness_accepts_fresh_heartbeat(store_dir: object) -> None:
    job = make_job("t1", "/r")
    job.update(worker_identity(WORKER_KIND_PROCESS, pid=os.getpid()))
    live, why = worker_liveness(job)
    assert live is True
    assert "alive" in why


def test_worker_liveness_keeps_legacy_records_running(store_dir: object) -> None:
    """Records predating worker identity keep the historical pid-only reading."""
    legacy = make_job("t1", "/r")
    legacy["pid"] = os.getpid()
    assert worker_liveness(legacy)[0] is True
    assert worker_liveness(legacy)[1].startswith("legacy record")
    no_pid = make_job("t1", "/r")
    assert worker_liveness(no_pid)[0] is True


def test_heartbeat_stale_cap_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITREINS_JOB_HEARTBEAT_STALE_SECONDS", "7.5")
    assert heartbeat_stale_seconds() == 7.5
    monkeypatch.setenv("GITREINS_JOB_HEARTBEAT_STALE_SECONDS", "not-a-number")
    assert heartbeat_stale_seconds() == 180.0


def test_find_running_job_reaps_dead_worker(store_dir: object) -> None:
    """A running record with a dead worker must not block the key (AC-2)."""
    job = make_job("t1", "/r")
    job.update(worker_identity(WORKER_KIND_PROCESS, pid=_dead_pid()))
    save_job(job)

    assert find_running_job("t1", "/r") is None

    stored = load_job(job["id"])
    assert stored["status"] == "error"
    assert stored["running"] is False
    assert stored["error_kind"] == "orphaned_worker"
    assert "died before completing" in stored["error"]
    assert stored["finished_at"] is not None


def test_find_running_job_reaps_stale_heartbeat(store_dir: object) -> None:
    """The exact incident: server pid alive, evaluation gone."""
    job = make_job("t1", "/r")
    job.update(worker_identity(WORKER_KIND_PROCESS, pid=os.getpid()))
    job["heartbeat_at"] = 1.0
    save_job(job)

    assert find_running_job("t1", "/r") is None
    assert load_job(job["id"])["status"] == "error"
    assert "heartbeat is stale" in load_job(job["id"])["error"]


def test_find_running_job_reap_can_be_disabled(store_dir: object) -> None:
    """reap=False keeps the old read-only semantics for callers that need them."""
    job = make_job("t1", "/r")
    job["pid"] = _dead_pid()
    save_job(job)
    assert find_running_job("t1", "/r", reap=False) is None
    assert load_job(job["id"])["status"] == "running"


def test_reap_stale_job_leaves_live_job_alone(store_dir: object) -> None:
    job = make_job("t1", "/r")
    job.update(worker_identity(WORKER_KIND_PROCESS))
    save_job(job)
    assert reap_stale_job(job["id"])["status"] == "running"


def test_reap_stale_job_ignores_terminal_record(store_dir: object) -> None:
    job = make_job("t1", "/r")
    job["status"] = "complete"
    job["running"] = False
    save_job(job)
    assert reap_stale_job(job["id"])["status"] == "complete"
    assert reap_stale_job("job-does-not-exist") is None


def test_heartbeat_keeps_a_long_job_live(
    store_dir: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """While the worker evaluates, beats keep the record live past the cap.

    The stale cap is 0.4 s here and the fake evaluation runs 0.9 s: without a
    heartbeat the record would be an orphan halfway through, which is exactly
    how a 48-minute tier2 run must NOT be graded.
    """
    import time as _time

    # Load-window determinism: this test failed twice in CI and once in a
    # judged self-review run (SR-2, 2026-11-07) because under pytest-xdist on
    # a loaded host the daemon heartbeat thread can be GIL-starved past a
    # 0.4 s stale cap — a scheduler artifact, not a product defect. The
    # mechanism under test is the RATIO (beats refresh the record inside the
    # cap; a silenced record is orphaned by it), so size the window so
    # realistic jitter cannot win: stale cap 2.0 s, beats every 0.1 s, run
    # 2.6 s. The falsifier below is unchanged and still fails if beats stop.
    monkeypatch.setenv("GITREINS_JOB_HEARTBEAT_STALE_SECONDS", "2.0")
    job = make_job("t1", "/r")
    job.update(worker_identity(WORKER_KIND_THREAD))
    save_job(job)
    stale_cap = 2.0
    hb = JobHeartbeat(job["id"], interval=0.1).start()
    try:
        deadline = _time.monotonic() + 2.6
        while _time.monotonic() < deadline:
            _time.sleep(0.1)
            live, _why = worker_liveness(load_job(job["id"]))
            assert live is True, f"job looked orphaned mid-run after {hb.beats} beats"
    finally:
        hb.stop()
    assert hb.beats >= 3
    assert _time.time() - load_job(job["id"])["heartbeat_at"] < stale_cap

    # Falsifier: the same record with the heartbeat silenced (its beat is not
    # refreshed) IS orphaned inside that window — the cap, not the pid, decides.
    silent = make_job("t2", "/s")
    silent.update(worker_identity(WORKER_KIND_THREAD))
    silent["heartbeat_at"] = _time.time()
    save_job(silent)
    _time.sleep(stale_cap + 0.1)  # must exceed the cap to prove staleness
    live_now, why_now = worker_liveness(load_job(silent["id"]))
    assert live_now is False
    assert "heartbeat is stale" in why_now


def test_heartbeat_does_not_resurrect_a_terminal_record(store_dir: object) -> None:
    """stop() joins the thread, so a terminal write always wins."""
    import time as _time

    job = make_job("t1", "/r")
    job.update(worker_identity(WORKER_KIND_THREAD))
    save_job(job)
    hb = JobHeartbeat(job["id"], interval=0.02).start()
    _time.sleep(0.07)
    hb.stop()
    done = load_job(job["id"])
    done["status"] = "complete"
    done["running"] = False
    save_job(done)
    _time.sleep(0.1)
    assert load_job(job["id"])["status"] == "complete"
    assert JobHeartbeat(job["id"], interval=0.02).beat() is False
