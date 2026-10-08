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
import threading
import types
import yaml

import pytest

from engine.task_manager import TaskManager
from pathlib import Path

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


def _complete_worker(workdir: str, task_id: str, ready: object, go: bool) -> None:
    """Complete one task in THIS process (a real separate process under fork)."""
    from engine.task_manager import TaskManager as _TM

    ready.set()
    go.wait()
    _TM(workdir).complete(task_id)


class TestConcurrentComplete:
    """Two concurrent processes completing DIFFERENT tasks — both must land."""

    def test_two_processes_complete_different_tasks(self, tmp_path: Path) -> None:
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

    def test_lock_file_created_next_to_store(self, tmp_path: Path) -> None:
        workdir = str(tmp_path)
        _write_store(workdir, ["solo"])
        TaskManager(workdir).complete("solo")
        assert os.path.exists(os.path.join(workdir, ".gitreins", "tasks.yaml.lock"))

    def test_many_processes_stress(self, tmp_path: Path) -> None:
        """4 processes x 5 sequential completes each (20 mutations, 4 tasks)."""
        workdir = str(tmp_path)
        ids = ["t1", "t2", "t3", "t4"]
        _write_store(workdir, ids)

        ctx = multiprocessing.get_context("fork")

        def repeated_worker(tid: str) -> None:
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

    def test_serial_create_start_complete_roundtrip(self, tmp_path: Path) -> None:
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

    def test_complete_missing_task_still_raises(self, tmp_path: Path) -> None:
        with pytest.raises(KeyError):
            TaskManager(str(tmp_path)).complete("does-not-exist")


class TestReloadGetWindow:
    """QA-GITR-003: ``reload()`` must publish the rebuilt view ATOMICALLY.

    The MCP server's ``_task_manager_for()`` calls ``reload()`` on EVERY
    task-touching tool call, so two concurrent ``judge.evaluate`` calls for one
    task used to clear each other's view: the previous ``self._tasks = {}``
    followed by an in-place ``_load()`` left an empty window, and a reader
    landing in it got a transient "Task not found" — an error dict with no
    ``status`` key, which surfaced as a bare ``KeyError`` in
    ``tests/test_mcp_server.py::TestJudgeAsyncPersistence::...single_flight``.

    The hook below parks EVERY reader thread inside the store's load window —
    deterministically, with no sleeps — and asserts that each still sees a
    complete view. On the unfixed code the readers observe an empty dict here;
    on the fixed code they observe the previous complete view until the single
    reference swap publishes the new one.
    """

    def test_get_inside_load_window_never_sees_not_found(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> object:
        workdir = str(tmp_path)
        _write_store(workdir, ["race-target", "other-task"])
        tm = TaskManager(workdir)
        assert tm.get("race-target") is not None, "fixture did not load"

        readers = 8
        in_window = threading.Event()
        reads_done = threading.Event()
        remaining = [readers]
        counts_lock = threading.Lock()
        seen: list[object] = [None] * readers
        real_load = TaskManager._load

        def hooked_load(self: object, into: object = None) -> object:
            # We are now INSIDE reload()'s rebuild. Unfixed code had already
            # cleared the live dict by this point; fixed code still holds the
            # previous complete view (the swap has not happened yet).
            in_window.set()
            assert reads_done.wait(timeout=30), "reader threads never completed"
            if into is None:
                return real_load(self)
            return real_load(self, into)

        def reader(i: object) -> None:
            assert in_window.wait(timeout=30), "reload never entered the load window"
            seen[i] = tm.get("race-target")
            with counts_lock:
                remaining[0] -= 1
                if remaining[0] == 0:
                    reads_done.set()

        threads = [threading.Thread(target=reader, args=(i,)) for i in range(readers)]
        for t in threads:
            t.start()

        monkeypatch.setattr(tm, "_load", types.MethodType(hooked_load, tm))
        tm.reload()

        for t in threads:
            t.join(30)
            assert not t.is_alive(), "reader thread hung"

        misses = [i for i, task in enumerate(seen) if task is None]
        assert misses == [], (
            f"{len(misses)}/{readers} readers observed a transient not-found while "
            "inside reload()'s load window — the view must be swapped atomically"
        )
        assert all(task.id == "race-target" for task in seen)

    def test_concurrent_create_with_reload_readers(self, tmp_path: Path) -> None:
        """Reader threads shaped like the MCP server (reload() then get()) must
        never miss a pre-existing task while another thread creates new ones.

        Bounded iteration counts on both sides: a tight reload() loop can starve
        an EX-flock writer (readers keep re-taking LOCK_SH), so an unbounded
        reader would hang the test rather than exercise it.
        """
        workdir = str(tmp_path)
        _write_store(workdir, ["stable"])
        tm = TaskManager(workdir)
        assert tm.get("stable") is not None

        readers = 4
        reader_iters = 400
        creates = 25
        start = threading.Barrier(readers + 1)
        misses: list[int] = []
        lock = threading.Lock()

        def reader(i: object) -> None:
            start.wait()
            for _ in range(reader_iters):
                tm.reload()
                if tm.get("stable") is None:
                    with lock:
                        misses.append(i)

        def creator() -> None:
            start.wait()
            for n in range(creates):
                tm.create(f"new-{n}", "t", ["c"])

        threads = [threading.Thread(target=reader, args=(i,), daemon=True) for i in range(readers)]
        for t in threads:
            t.start()
        writer = threading.Thread(target=creator, daemon=True)
        writer.start()
        writer.join(120)
        assert not writer.is_alive(), "creator thread hung"
        for t in threads:
            t.join(120)
            assert not t.is_alive(), "reader thread hung"

        assert misses == [], f"readers saw a transient not-found: {misses}"
        # Every created task landed, and the stable one is still there.
        final = TaskManager(workdir)
        assert final.get("stable") is not None
        for n in range(creates):
            assert final.get(f"new-{n}") is not None, f"lost created task new-{n}"
