"""INT-FLAKE-5: the load harness must not be able to outlive its runner.

A verification script that reproduced a load-dependent flake with detached
`setsid sh -c 'while :; do :; done'` loops leaked 24+ session leaders onto the
host running the Hermes gateway/scheduler/DuckBrain (load average 33.8) because
the loops were never in the runner's process group.  These tests pin the
replacement's guarantees: kill the runner, nothing survives; a shared host is
refused unless explicitly allowed; the request is hard-capped; a clean run
reports zero survivors.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
)

import loadgen  # noqa: E402

SCRIPT = Path(__file__).parents[1] / "scripts" / "loadgen.py"
LINUX = sys.platform.startswith("linux")


def _wait_for_started(proc, timeout=30.0):
    """Read the runner's `started` line (the child pids) or fail."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        line = proc.stderr.readline()
        if not line:
            if proc.poll() is not None:
                raise AssertionError(f"runner exited early: rc={proc.returncode}")
            continue
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        if payload.get("event") == "started":
            return payload["pids"]
    raise AssertionError("runner never announced its child pids")


@pytest.mark.skipif(not LINUX, reason="PR_SET_PDEATHSIG is a Linux mechanism")
def test_load_generator_cannot_outlive_a_killed_runner():
    """SIGKILL the runner: the kernel must reap every burner (INT-FLAKE-5)."""
    proc = subprocess.Popen(
        [
            sys.executable,
            os.fspath(SCRIPT),
            "--workers",
            "2",
            "--seconds",
            "120",
            "--allow-shared-host",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    pids: list[int] = []
    try:
        pids = _wait_for_started(proc)
        assert len(pids) == 2, pids
        assert all(loadgen._alive(pid) for pid in pids), "burners should be running"
        # Kill the runner the way an impatient operator does: no cleanup chance.
        os.kill(proc.pid, signal.SIGKILL)
        proc.wait(timeout=10)
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline and any(loadgen._alive(pid) for pid in pids):
            time.sleep(0.1)
        survivors = [pid for pid in pids if loadgen._alive(pid)]
        assert not survivors, f"load outlived its runner: {survivors}"
    finally:
        if proc.poll() is None:  # pragma: no cover - defensive
            proc.kill()
        for pid in pids:  # pragma: no cover - defensive
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass


def test_clean_run_reports_no_survivors():
    """A normal bounded run returns promptly and verifies its own cleanup."""
    started = time.monotonic()
    summary = loadgen.generate(2, 0.4, allow_shared_host=True, markers=())
    elapsed = time.monotonic() - started
    assert summary["clean"] is True, summary
    assert summary["survivors"] == []
    assert len(summary["pids"]) == 2
    assert elapsed < 20.0, f"a 0.4 s run took {elapsed:.1f}s"


def test_shared_host_is_refused_unless_allowlisted(monkeypatch):
    """Synthetic load is refused where the fleet's own services run."""
    monkeypatch.delenv(loadgen.ALLOW_SHARED_ENV, raising=False)
    monkeypatch.setattr(loadgen, "shared_host_processes", lambda markers=(): ["42: gateway run"])
    with pytest.raises(RuntimeError, match="shared Hermes services"):
        loadgen.generate(1, 0.2)

    # The env override is the documented escape hatch (lane paused / bunker box).
    monkeypatch.setenv(loadgen.ALLOW_SHARED_ENV, "1")
    summary = loadgen.generate(1, 0.2, markers=())
    assert summary["clean"] is True
    assert summary["allow_shared_host"] is True


def test_request_is_hard_capped():
    """Workers, seconds and the CPU set are validated, not trusted."""
    with pytest.raises(ValueError, match="positive integer"):
        loadgen.generate(0, 1)
    with pytest.raises(ValueError, match="capped at"):
        loadgen.generate(loadgen.MAX_WORKERS + 1, 1)
    with pytest.raises(ValueError, match="positive"):
        loadgen.generate(1, 0)
    with pytest.raises(ValueError, match="capped at"):
        loadgen.generate(1, loadgen.MAX_SECONDS + 1)
    with pytest.raises(ValueError, match="empty CPU set"):
        loadgen._parse_cpus(" , ")


def test_cpu_set_parsing_and_restoration(monkeypatch):
    """`--cpus` narrows the run and the runner's own affinity is restored."""
    assert loadgen._parse_cpus("0-2,4", {0, 1, 2, 3, 4}) == {0, 1, 2, 4}
    assert loadgen._parse_cpus("0-3", {1, 2}) == {1, 2}

    if not (LINUX and hasattr(os, "sched_setaffinity")):  # pragma: no cover
        pytest.skip("affinity control is Linux-only")
    allowed = sorted(os.sched_getaffinity(0))
    if len(allowed) < 2:  # pragma: no cover - single-CPU box
        pytest.skip("needs at least two CPUs")
    target = f"{allowed[0]}-{allowed[1]}"
    summary = loadgen.generate(1, 0.2, cpus=target, allow_shared_host=True, markers=())
    assert summary["clean"] is True
    assert summary["cpus"] == target
    # The runner's own affinity is back to where it started.
    assert sorted(os.sched_getaffinity(0)) == allowed
