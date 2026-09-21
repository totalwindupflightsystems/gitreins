"""
Regression tests for INT-CI-14 — flock-serialized tasks.yaml read-modify-write.

Wave closure naturally runs N concurrent judge processes (`gitreins task
complete`), and their unsynchronized load-modify-save cycles against the shared
``.gitreins/tasks.yaml`` corrupted the file (truncated YAML, lost tasks, a
57KB unparsable sidecar). These tests spawn real OS processes against one
fixture store and assert the final file parses AND every task survived.

multiprocessing (fork start method) is mandatory here: threads share one fd
table and one flock, so they cannot prove cross-process serialization.
"""

import os

import multiprocessing
import yaml

import pytest

from engine.task_manager import TaskManager

pytestmark = pytest.mark.timeout(120)


def _write_store(workdir: str, task_ids: list[str]) -> None:
    """Seed a fixture .gitreins/tasks.yaml with pending tasks, bypassing the store."""
    cfg = os.path.join(workdir, ".gitreins")
    os.makedirs(cfg, exist_ok=True)
    tasks = [
        {
            "id": tid,
            "title": f"Task {tid}",
            "criteria": [f"c1 for {tid}"],
            "status": "pending",
            "created_at": "2026-01-01T00:00:00+00:00",
        }
        for tid in task_ids
    ]
    with open(os.path.join(cfg, "tasks.yaml"), "w") as f:
        yaml.dump({"tasks": tasks}, f, default_flow_style=False, sort_keys=False)


def _read_store(workdir: str) -> list[dict]:
    with open(os.path.join(workdir, ".gitreins", "tasks.yaml"), "r") as f:
        data = yaml.safe_load(f) or {}
    return list(data.get("tasks", []))


def _complete_worker(workdir: str, task_id: str, ready, go) -> None:
    """Complete one task in THIS process (a real separate process under fork)."""
    from engine.task_manager import TaskManager as _TM

    ready.set()
    go.wait()
    _TM(workdir).complete(task_id)


class TestConcurrentComplete:
    """Two concurrent processes completing DIFFERENT tasks — both must land."""

    def test_two_processes_complete_different_tasks(self, tmp_path):
        workdir = str(tmp_path)
        _write_store(workdir, ["alpha", "beta"])

        ctx = multiprocessing.get_context("fork")
        ready = ctx.Event()
        go = ctx.Event()
        procs = [
            ctx.Process(target=_complete_worker, args=(workdir, tid, ready, go))
            for tid in ("alpha", "beta")
        ]
        for p in procs:
            p.start()
        for p in procs:
            assert ready.wait(timeout=60), "worker never became ready"
        go.set()
        for p in procs:
            p.join(timeout=60)
            assert p.exitcode == 0, f"worker failed with exit code {p.exitcode}"

        # The final file must parse AND contain both tasks, both complete.
        tasks = {t["id"]: t for t in _read_store(workdir)}
        assert set(tasks) == {"alpha", "beta"}, f"lost tasks: {sorted(tasks)}"
        assert tasks["alpha"]["status"] == "complete"
        assert tasks["beta"]["status"] == "complete"
        assert tasks["alpha"].get("completed_at")
        assert tasks["beta"].get("completed_at")
        # No torn temp file left behind.
        assert not os.path.exists(os.path.join(workdir, ".gitreins", "tasks.yaml.tmp"))
        # No corruption sidecar was produced.
        leftovers = [n for n in os.listdir(os.path.join(workdir, ".gitreins")) if ".corrupt-" in n]
        assert leftovers == [], f"corruption sidecars appeared: {leftovers}"

    def test_lock_file_created_next_to_store(self, tmp_path):
        workdir = str(tmp_path)
        _write_store(workdir, ["solo"])
        TaskManager(workdir).complete("solo")
        assert os.path.exists(os.path.join(workdir, ".gitreins", "tasks.yaml.lock"))

    def test_many_processes_stress(self, tmp_path):
        """4 processes x 5 sequential completes each (20 mutations, 4 tasks)."""
        workdir = str(tmp_path)
        ids = ["t1", "t2", "t3", "t4"]
        _write_store(workdir, ids)

        ctx = multiprocessing.get_context("fork")

        def repeated_worker(tid: str):
            from engine.task_manager import TaskManager as _TM

            for _ in range(5):
                tm = _TM(workdir)
                tm.complete(tid, force=True)

        procs = [ctx.Process(target=repeated_worker, args=(tid,)) for tid in ids]
        for p in procs:
            p.start()
        for p in procs:
            p.join(timeout=120)
            assert p.exitcode == 0, f"worker failed with exit code {p.exitcode}"

        tasks = {t["id"]: t for t in _read_store(workdir)}
        assert set(tasks) == set(ids), f"lost tasks: {sorted(tasks)}"
        assert all(tasks[tid]["status"] == "complete" for tid in ids)


class TestSerialPathUnchanged:
    """The lock must not change single-process behaviour (suite covers more)."""

    def test_serial_create_start_complete_roundtrip(self, tmp_path):
        workdir = str(tmp_path)
        tm = TaskManager(workdir)
        tm.create("solo", "Solo Task", ["c1"])
        tm.start("solo")
        done = tm.complete("solo")
        assert done.status == "complete"
        assert done.completed_at

        reloaded = TaskManager(workdir)
        task = reloaded.get("solo")
        assert task is not None and task.status == "complete"

    def test_complete_missing_task_still_raises(self, tmp_path):
        with pytest.raises(KeyError):
            TaskManager(str(tmp_path)).complete("does-not-exist")
