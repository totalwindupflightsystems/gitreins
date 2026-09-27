"""Tier1 per-repo tests lock (INT-FLAKE-6).

Contract under test:
- the lock is taken ONLY by a tier1 tests step running under a judge
  (TIER1_ENV_VAR stamped), never by the guard or manual pytest;
- the lock file lives OUTSIDE the repo (/tmp, keyed by the repo root) so a run
  never writes an in-tree file and two repos never contend;
- the acquisition is serialized: a second tier1 run WAITS for the winner and
  its step data names ``tier1_tests_lock: waited`` (attribution, not silence);
- the wait is bounded: an expired wait still runs the step and names
  ``wait-expired`` in the data;
- the release is idempotent and never raises.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from engine import tier1_lock
from engine.pipeline import Pipeline, TIER1_ENV_VAR

pytestmark = pytest.mark.timeout(60)


@pytest.fixture
def _isolated_lock_path(tmp_path, monkeypatch):
    """Point the lock at this test's own repo root (tmp), never the real repo."""
    monkeypatch.setenv("GITREINS_TIER1_REPO_ROOT", str(tmp_path / "repo"))
    (tmp_path / "repo").mkdir()
    yield tmp_path / "repo"


class TestLockScope:
    def test_guard_and_manual_runs_never_take_the_lock(self, tmp_workdir):
        """No TIER1_ENV_VAR → no lock, no attribution, nothing to release."""
        pipeline = Pipeline({"pipeline": {"stages": []}}, tmp_workdir)
        result = pipeline._run_script_step(
            {"id": "tests", "type": "script", "run": "echo ok"}, {}, stage_id="tier1"
        )
        assert result.passed is True
        assert tier1_lock.LAST_ACQUIRE_RESULT is None
        assert "tier1_tests_lock" not in result.data

    def test_lock_file_lives_outside_the_repo(self, _isolated_lock_path):
        """The lock file is in /tmp keyed by the repo root — never in-tree."""
        path = tier1_lock.tier1_tests_lock_path()
        assert path.startswith("/tmp/"), path
        assert "gitreins-tier1-" in path
        assert str(_isolated_lock_path) not in path


class TestSerialization:
    def test_concurrent_tier1_runs_serialize_and_the_loser_names_the_wait(
        self, _isolated_lock_path, monkeypatch
    ):
        """Two real tier1 tests steps at once: the loser waits, both complete.

        The winner holds the lock in a subprocess for ~2s while the loser's
        step is dispatched; the loser's step must still PASS (it ran to
        completion after the winner released) and its data must carry the
        ``tier1_tests_lock`` attribution.
        """
        repo = str(_isolated_lock_path)
        monkeypatch.setenv(TIER1_ENV_VAR, "1")
        holder = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import time, os\n"
                "os.environ['GITREINS_TIER1'] = '1'\n"
                "from engine import tier1_lock\n"
                "print(tier1_lock.acquire(), flush=True)\n"
                "time.sleep(2)\n"
                "tier1_lock.release()\n",
            ],
            cwd=os.path.dirname(tier1_lock.__file__),
            env={**os.environ, "GITREINS_TIER1_REPO_ROOT": repo},
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            # The winner is holding it (acquire in the child is immediate).
            line = holder.stdout.readline().strip()
            assert line == "acquired", line

            pipeline = Pipeline({"pipeline": {"stages": []}}, repo)
            # A tier1 tests step dispatched WHILE the winner holds the lock:
            # env-stamped so it takes the lock, run is a quick pytest-free
            # command so the step body itself is instant — the only possible
            # delay is the wait for the winner's release.
            result = pipeline._run_script_step(
                {"id": "tests", "type": "script", "run": "echo loser-ran"},
                {},
                stage_id="tier1",
            )
            assert result.passed is True, result.error
            assert "loser-ran" in result.output
            assert result.data.get("tier1_tests_lock") == "waited", result.data
        finally:
            holder.wait(timeout=30)

    def test_wait_expiry_still_runs_and_names_itself(self, _isolated_lock_path, monkeypatch):
        """The bounded wait never wedges the gate: expiry runs the step, named."""
        repo = str(_isolated_lock_path)
        monkeypatch.setattr(tier1_lock, "LOCK_WAIT_BUDGET_S", 0.5)
        monkeypatch.setenv(TIER1_ENV_VAR, "1")

        # Hold the lock in THIS process (a lock the test never releases) so the
        # step's wait has a holder that will not let go.
        fd = os.open(tier1_lock.tier1_tests_lock_path(), os.O_CREAT | os.O_RDWR, 0o600)
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            pipeline = Pipeline({"pipeline": {"stages": []}}, repo)
            result = pipeline._run_script_step(
                {"id": "tests", "type": "script", "run": "echo still-ran"},
                {},
                stage_id="tier1",
            )
            assert result.passed is True, result.error
            assert "still-ran" in result.output
            assert result.data.get("tier1_tests_lock") == "wait-expired", result.data
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def test_winner_names_acquired_and_releases_cleanly(self, _isolated_lock_path, monkeypatch):
        """An uncontended tier1 run: acquired, attributed, lock gone after."""
        repo = str(_isolated_lock_path)
        monkeypatch.setenv(TIER1_ENV_VAR, "1")
        pipeline = Pipeline({"pipeline": {"stages": []}}, repo)
        result = pipeline._run_script_step(
            {"id": "tests", "type": "script", "run": "echo winner"},
            {},
            stage_id="tier1",
        )
        assert result.passed is True
        assert result.data.get("tier1_tests_lock") == "acquired"
        # The lock file may persist, but the LOCK must be gone: a second
        # process can acquire it immediately (non-blocking proof).
        fd = os.open(tier1_lock.tier1_tests_lock_path(), os.O_CREAT | os.O_RDWR, 0o600)
        import fcntl

        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
