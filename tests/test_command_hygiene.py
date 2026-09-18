"""Tests for engine.command_hygiene — the 2026-09-18 orphan-burner fix.

BEHAVIOUR tests: they spawn real processes and assert nothing survives the call.
Anti-self-match discipline (three incidents of this class on this box): never
assert by scanning cmdlines for a needle that could appear in the test runner's
own argv — capture the child's real PID via a file and check /proc/<pid>, or
assert on a side effect (a file that must NOT exist).
"""
from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import time
import uuid

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from engine import command_hygiene as ch  # noqa: E402


def _alive(pid: int) -> bool:
    return os.path.isdir(f"/proc/{pid}")


def _wait_gone(pid: int, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.05)
    return not _alive(pid)


@pytest.fixture()
def pidfile(tmp_path):
    return tmp_path / f"child-{uuid.uuid4().hex}.pid"


# ── refusal policy ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("cmd", [
    "timeout 300 nice -n 0 sh -c while :; do :; done",
    "cd /home/kara/crier && for i in $(seq 1 64); do timeout 300 nice -n 0 sh -c 'while :; do :; done' & done",
    "bash -c 'while true; do :; done'",
    "yes > /dev/null",
    "cat /dev/zero > /dev/null",
    "dd if=/dev/zero of=/dev/null bs=1M",
    ":(){ :|:& };:",
])
def test_busy_wait_commands_are_refused(cmd):
    assert ch.busy_wait_reason(cmd), f"must be refused: {cmd}"


@pytest.mark.parametrize("cmd", [
    "sleep 0.1",
    "go test ./... -count=1",
    "yes | head -100",
    "dd if=/dev/urandom of=/dev/null bs=64k count=8",
    "for i in $(seq 1 3); do echo $i; sleep 0.05; done",
    "python3 scripts/loadgen.py --workers 4 --seconds 5",
])
def test_legitimate_commands_still_run(cmd):
    assert ch.busy_wait_reason(cmd) is None, f"must NOT be refused: {cmd}"


def test_refusal_message_points_at_the_right_primitives():
    out = ch.run_bounded("while :; do :; done")
    assert out.get("refused") is True
    assert "loadgen.py" in out["reason"] and "sleep" in out["reason"]
    assert "2026-09-18" in out["reason"]  # carries the evidence


def test_refused_command_never_executes(tmp_path):
    """Proof by side effect: the refused command must not have run at all."""
    canary = tmp_path / "executed.canary"
    spin = "while :; do :; done"
    out = ch.run_bounded(f"{spin} ; touch {canary}")
    assert out.get("refused") is True
    assert not canary.exists(), "refused command executed its side effect"


# ── the leak fix: backgrounded children cannot escape ────────────────────────

def test_backgrounded_child_is_reaped_on_normal_exit(pidfile):
    """The exact incident shape: the call RETURNS while a `&` child is alive.

    The child records its own PID; the child is a `sleep`, which would outlive a
    naive implementation (subprocess.run only waits for the parent shell).
    """
    cmd = f"sh -c 'echo $$ > {pidfile}; exec sleep 30' & echo started"
    out = ch.run_bounded(cmd, timeout=10)
    assert "started" in out.get("output", "")
    assert pidfile.exists(), "child never recorded its pid (test harness problem)"
    child = int(pidfile.read_text().strip())
    assert _wait_gone(child), f"backgrounded child {child} escaped the process-group reap"


def test_timeout_kills_the_whole_group(pidfile):
    cmd = f"sh -c 'echo $$ > {pidfile}; exec sleep 60'"
    out = ch.run_bounded(cmd, timeout=1)
    assert out.get("timed_out") is True
    assert "timed out" in out.get("error", "")
    child = int(pidfile.read_text().strip())
    assert _wait_gone(child), f"timed-out child {child} survived the group kill"


def test_group_helpers_validate_inputs():
    # Never signal PID 1 / invalid values (the os.killpg incident in this repo).
    assert ch.pids_in_group(1) == []
    assert ch.kill_group(1) == []
    assert ch.pids_in_group(0) == []
    assert ch.kill_group("not-an-int") == []  # type: ignore[arg-type]
    assert ch.kill_group(999_999_999) == []   # nonexistent group


def test_pids_in_group_finds_a_child_group():
    proc = subprocess.Popen(["sleep", "5"], start_new_session=True)
    try:
        time.sleep(0.2)
        assert proc.pid in ch.pids_in_group(os.getpgid(proc.pid))
    finally:
        ch.kill_group(os.getpgid(proc.pid))
        proc.wait(timeout=5)


def test_happy_path_reports_exit_code_and_output():
    out = ch.run_bounded("echo hello; exit 3", timeout=10)
    assert out["exit_code"] == 3
    assert "hello" in out["output"]
    assert out["timed_out"] is False
    assert "leftover_pids" not in out
