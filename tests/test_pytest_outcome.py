"""INT-FLAKE-2 — a pytest exit code is not a diagnosis.

The row was filed as "the tier1 tests step intermittently dies mid-suite
(exit 2, truncated output, no pytest summary)" after verdict 47f9d514 recorded
`tests/exit_code = 2` while the identical command passed twice in a row.

The live reproduction (2026-09-16) shows the mapping, not an interruption:
`-x` (maxfail) plus pytest-xdist makes the MASTER raise xdist's own
``Interrupted(KeyboardInterrupt)`` as soon as maxfail is reached, and pytest
maps KeyboardInterrupt onto ``ExitCode.INTERRUPTED`` (2). So a suite with a
REAL failing test exits 2 — byte-identical to the code an externally signalled
run exits — and the evidence that would have named the failing test sat past
the step's head-only ``[:2000]`` capture slice.

These tests pin both halves: the classification (``pytest_outcome``) and the
retention (``_run_script_step`` keeps the whole output so
``_bound_step_evidence`` can preserve pytest's short test summary).
"""

import os
import shlex
import subprocess
import sys

import pytest

from engine.pipeline import Pipeline
from engine.types import PYTEST_OUTCOME_KINDS, pytest_outcome

# ── Captured evidence (verbatim, from the live reproductions) ────────────────

# 2026-09-16: `pytest -x --tb=short -n 2` over a 3-test synthetic suite with
# one failing assertion. Exit code: 2. The last line before the summary is
# xdist's own Interrupted — that marker, not the exit code, says "tests failed".
MAXFAIL_XDIST_OUTPUT = """
============================= test session starts ==============================
platform linux -- Python 3.10.20, pytest-9.1.1, pluggy-1.6.0
rootdir: /tmp/gp-flake/mini
plugins: xdist-3.8.0
2 workers [3 items]
scheduling tests via LoadScheduling

..F
=================================== FAILURES ===================================
_________________________________ test_broken __________________________________
[gw1] linux -- Python 3.10.20 /home/kara/gitreins-poc/.venv/bin/python
test_b.py:2: in test_broken
    assert 1 == 2
E   assert 1 == 2
=========================== short test summary info ============================
FAILED test_b.py::test_broken - assert 1 == 2
!!!!!!!!!!!!!!!!!!!!!!!!!! stopping after 1 failures !!!!!!!!!!!!!!!!!!!!!!!!!!!
!!!!!!!!!!!! xdist.dsession.Interrupted: stopping after 1 failures !!!!!!!!!!!!!
========================= 1 failed, 2 passed in 1.17s ==========================
"""

# 2026-09-16: the healthy-legit case — a SIGINT delivered to a running xdist
# master. Exit code: 2 as well, but there is no failing test, only the
# KeyboardInterrupt banner.
KEYBOARD_INTERRUPT_OUTPUT = """
4 workers [1590 items]

....................................................................... [ 44%]
..............
=============================== warnings summary ===============================
  /home/kara/gitreins-poc/.venv/lib/python3.10/site-packages/_pytest/python.py:171:
    PytestReturnNotNoneWarning: Test functions should return None
-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
!!!!!!!!!!!!!!!!!!!!!!!!!!!!!! KeyboardInterrupt !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
/home/kara/.local/share/uv/python/cpython-3.10-linux-x86_64-gnu/lib/python3.10/threading.py:324:
KeyboardInterrupt
(to show a full traceback on KeyboardInterrupt use --full-trace)
================== 726 passed, 8 skipped, 2 warnings in 9.01s ==================
"""

# The exact 500-char head the pre-fix verdict retained: banner + dots, the
# summary (and any cause) already discarded by the capture slice.
TRUNCATED_HEAD_OUTPUT = """\
============================= test session starts ==============================
platform linux -- Python 3.11.15, pytest-9.1.1, pluggy-1.6.0
rootdir: /home/kara/gitreins-poc
configfile: pyproject.toml
testpaths: tests
plugins: xdist-3.8.0, timeout-2.4.0, anyio-4.14.2
created: 4/4 workers
4 workers [1358 items]

........................................................................ [  5%]
........................................................................ [ 10%]
..........................
"""

# No `-x`: a failing suite exits 1 with the normal short summary.
PLAIN_FAILURE_OUTPUT = """\
============================= test session starts ==============================
rootdir: /tmp/gp-flake/mini
2 workers [3 items]

..F
=========================== short test summary info ============================
FAILED test_b.py::test_broken - assert 1 == 2
=========================== 1 failed, 2 passed in 2.44s ============================
"""


def _sanitized_env() -> dict:
    """Environment for nested pytest runs: no leaked git/xdist parenting."""
    return {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("GIT_", "PYTEST_XDIST", "PYTEST_ADDOPTS"))
    }


# ── Classification ──────────────────────────────────────────────────────────


class TestPytestOutcomeClassification:
    def test_exit_2_with_xdist_maxfail_is_a_real_failure_not_an_interruption(self):
        """INT-FLAKE-2's core defect: exit 2 + maxfail must not read as a flake."""
        outcome = pytest_outcome(2, MAXFAIL_XDIST_OUTPUT)

        assert outcome["kind"] == "maxfail"
        assert outcome["interrupted"] is False
        assert outcome["first_failing_test"] == "test_b.py::test_broken"
        assert outcome["failures"] == 1
        assert "real test failure" in outcome["detail"]
        assert "exit 2 here is xdist's Interrupted" in outcome["detail"]
        assert "test_b.py::test_broken" in outcome["detail"]

    def test_exit_2_with_keyboard_interrupt_stays_an_interruption(self):
        """The other meaning of 2 survives — classified distinctly, not merged."""
        outcome = pytest_outcome(2, KEYBOARD_INTERRUPT_OUTPUT)

        assert outcome["kind"] == "interrupted"
        assert outcome["interrupted"] is True
        assert outcome["first_failing_test"] is None
        assert "KeyboardInterrupt" in outcome["detail"]

    def test_exit_2_without_evidence_is_reported_as_undetermined(self):
        """Truncated capture: say so — never invent a cause in either direction."""
        outcome = pytest_outcome(2, TRUNCATED_HEAD_OUTPUT)

        assert outcome["kind"] == "interrupted-unclassified"
        assert outcome["interrupted"] is True
        assert "neither a FAILED line nor a KeyboardInterrupt" in outcome["detail"]

    def test_exit_1_is_a_plain_failure_with_the_failing_id(self):
        outcome = pytest_outcome(1, PLAIN_FAILURE_OUTPUT)

        assert outcome["kind"] == "failed"
        assert outcome["interrupted"] is False
        assert outcome["first_failing_test"] == "test_b.py::test_broken"
        assert outcome["failures"] == 1

    def test_exit_0_is_passed(self):
        outcome = pytest_outcome(0, "4 workers [3 items]\n\n=== 3 passed in 0.5s ===")

        assert outcome["kind"] == "passed"
        assert outcome["interrupted"] is False
        assert outcome["failures"] == 0

    def test_maxfail_banner_without_xdist_is_not_read_as_an_interruption(self):
        """`-x` on a serial run exits 1 but prints the same banner."""
        output = (
            "=========================== short test summary info ============================\n"
            "FAILED tests/test_a.py::test_x - assert False\n"
            "!!!!!!!!!!!!!!!!!!!!!!!!!! stopping after 1 failures !!!!!!!!!!!!!!!!!!!!!!!!!!!\n"
            "========================= 1 failed, 4 passed in 3.00s ==========================\n"
        )
        outcome = pytest_outcome(1, output)

        assert outcome["kind"] == "failed"
        assert outcome["interrupted"] is False

    def test_exit_5_no_tests_collected(self):
        assert pytest_outcome(5, "no tests ran in 0.01s")["kind"] == "no-tests-collected"

    def test_exit_4_usage_error(self):
        assert pytest_outcome(4, "ERROR: usage: pytest [options]")["kind"] == "usage-error"

    def test_exit_3_internal_error(self):
        outcome = pytest_outcome(3, "INTERNALERROR> Traceback (most recent call last):")

        assert outcome["kind"] == "internal-error"

    def test_killed_by_signal_is_named(self):
        """A SIGKILLed pytest never writes a summary: name the signal instead."""
        outcome = pytest_outcome(-9, "4 workers [1358 items]\n\n.....")

        assert outcome["kind"] == "unknown"
        assert "signal 9" in outcome["detail"]

    @pytest.mark.parametrize(
        "code,output",
        [
            (0, "3 passed"),
            (1, PLAIN_FAILURE_OUTPUT),
            (2, MAXFAIL_XDIST_OUTPUT),
            (2, KEYBOARD_INTERRUPT_OUTPUT),
            (2, TRUNCATED_HEAD_OUTPUT),
            (3, "INTERNALERROR> boom"),
            (4, "ERROR: usage"),
            (5, "no tests ran"),
            (None, "4 workers"),
        ],
    )
    def test_every_kind_is_in_the_enumerated_vocabulary(self, code, output):
        assert pytest_outcome(code, output)["kind"] in PYTEST_OUTCOME_KINDS


# ── The previous failure mode, reproduced live ──────────────────────────────


class TestLiveMaxfailReproduction:
    def test_live_xdist_maxfail_run_exits_2_and_is_classified_as_a_failure(self, tmp_path):
        """Pins the mapping against the real pytest-xdist in this environment.

        Pre-fix, `exit_code == 2` in a verdict was read as "interrupted". This
        test asserts the exit code AND that the classifier calls it what it is.
        """
        pytest.importorskip("xdist")
        (tmp_path / "test_ok.py").write_text("def test_ok():\n    assert 1 + 1 == 2\n")
        (tmp_path / "test_broken.py").write_text("def test_broken():\n    assert 1 == 2\n")

        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "-x",
                "--tb=short",
                "-n",
                "2",
                "-p",
                "no:cacheprovider",
            ],
            capture_output=True,
            text=True,
            timeout=180,
            cwd=str(tmp_path),
            env=_sanitized_env(),
        )
        output = proc.stdout + proc.stderr

        assert proc.returncode == 2, (
            "expected the maxfail+xdist mapping (a failing test exits 2, not 1); "
            f"got {proc.returncode}\n{output[-2000:]}"
        )
        assert "xdist.dsession.Interrupted" in output

        outcome = pytest_outcome(proc.returncode, output)
        assert outcome["kind"] == "maxfail"
        assert outcome["interrupted"] is False
        assert outcome["first_failing_test"].endswith("test_broken.py::test_broken")


# ── The step that writes verdict.json ───────────────────────────────────────


class TestTier1TestsStepEvidence:
    def _pipeline(self, workdir) -> Pipeline:
        return Pipeline({"pipeline": {"stages": []}}, str(workdir))

    def test_outcome_is_recorded_and_the_summary_tail_survives_serialization(self, tmp_path):
        """Both halves of the fix, on the step that produces the verdict record.

        Fails on the pre-fix code: the head-only `[:2000]` slice kept the filler
        below and discarded pytest's short test summary, so neither the failing
        test id nor the maxfail cause reached the record.
        """
        pytest.importorskip("xdist")
        workdir = tmp_path / "workdir"
        workdir.mkdir()
        (workdir / "test_broken.py").write_text("def test_broken():\n    assert 1 == 2\n")

        # >2000 chars of head that is NOT pytest output, so anything pytest
        # appends — the FAILED line and the summary — falls outside the old slice.
        filler = "pre-slice filler " + "x" * 4000
        cmd = (
            f'{shlex.quote(sys.executable)} -c "print({filler!r})" && '
            f"{shlex.quote(sys.executable)} -m pytest -x --tb=short -n 2 "
            "-p no:cacheprovider test_broken.py"
        )

        step = self._pipeline(workdir)._run_script_step(
            {"id": "tests", "type": "script", "run": cmd},
            {"id": "int-flake-2", "criteria": []},
        )

        assert step.passed is False
        assert step.data["exit_code"] == 2
        outcome = step.data["pytest_outcome"]
        assert outcome["kind"] == "maxfail"
        assert outcome["interrupted"] is False
        assert outcome["first_failing_test"].endswith("test_broken.py::test_broken")

        # The serialized (bounded) evidence must still carry the summary tail.
        retained = step.to_dict()["output"]
        assert len(retained) < len(step.output), "expected the 4000-char evidence bound to apply"
        assert "FAILED test_broken.py::test_broken" in retained
        assert "xdist.dsession.Interrupted" in retained

    def test_non_pytest_steps_carry_no_pytest_outcome(self, tmp_path):
        """Scope guard: the classifier is for pytest invocations, not every step."""
        workdir = tmp_path / "workdir"
        workdir.mkdir()

        step = self._pipeline(workdir)._run_script_step(
            {"id": "lint", "type": "script", "run": "echo no-findings"},
            {"id": "int-flake-2", "criteria": []},
        )

        assert step.passed is True
        assert "pytest_outcome" not in step.data
