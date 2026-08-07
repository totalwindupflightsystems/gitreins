"""
Unit tests for engine/job_store.py — the disk-backed job store (DF-006).
"""

import os

import pytest

from engine.eval_cap import EvalCap
from engine.job_store import (
    cap_from_dict,
    cap_to_dict,
    delete_job,
    job_dir,
    list_jobs,
    load_job,
    make_job,
    pid_alive,
    save_job,
)


@pytest.fixture
def store_dir(tmp_path, monkeypatch):
    d = str(tmp_path / "jobs")
    monkeypatch.setenv("GITREINS_JOB_DIR", d)
    return d


def test_save_load_roundtrip(store_dir):
    job = make_job("task-1", "/tmp/some/repo")
    job["pid"] = 1234
    save_job(job)

    loaded = load_job(job["id"])
    assert loaded is not None
    assert loaded["id"] == job["id"]
    assert loaded["task_id"] == "task-1"
    assert loaded["workdir"] == "/tmp/some/repo"
    assert loaded["status"] == "running"
    assert loaded["pid"] == 1234
    assert loaded["result"] is None
    assert loaded["caps"] is None


def test_load_missing_returns_none(store_dir):
    assert load_job("job-nope") is None


def test_list_jobs_roundtrip(store_dir):
    a = make_job("t1", "/r")
    b = make_job("t2", "/r")
    save_job(a)
    save_job(b)
    ids = {j["id"] for j in list_jobs()}
    assert ids == {a["id"], b["id"]}


def test_delete_job(store_dir):
    job = make_job("t1", "/r")
    save_job(job)
    assert delete_job(job["id"]) is True
    assert load_job(job["id"]) is None
    assert delete_job(job["id"]) is False


def test_save_is_atomic_no_tmp_leftovers(store_dir):
    job = make_job("t1", "/r")
    save_job(job)
    leftovers = [n for n in os.listdir(job_dir()) if ".tmp" in n]
    assert leftovers == []


def test_corrupt_job_file_returns_none(store_dir):
    job = make_job("t1", "/r")
    save_job(job)
    with open(os.path.join(job_dir(), job["id"] + ".json"), "w") as f:
        f.write("{not json")
    assert load_job(job["id"]) is None


def test_cap_roundtrip():
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


def test_cap_none_roundtrip():
    assert cap_to_dict(None) is None
    assert cap_from_dict(None) is None
    assert cap_from_dict({}) is None


def test_pid_alive():
    assert pid_alive(None) is False
    assert pid_alive(0) is False
    assert pid_alive(-5) is False
    assert pid_alive(True) is False
    assert pid_alive(os.getpid()) is True
    # 99999999 is (practically) never a live pid
    assert pid_alive(99999999) is False
