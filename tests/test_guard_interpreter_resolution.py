"""DF-030: the tests lane must not let PATH order decide whether it grades.

The defect (dogfood ``gitreins-dogfood-2026-11-09-12-54-05``, gitreins 0.20.0,
consumer repo textkit): a ``python3 -m pytest`` test_command resolved
``python3`` through the guard's inherited PATH. With the host's ambient PATH
(a runtime venv sits early) the lane picked an interpreter without pytest and
reported ``~ tests (full) — skipped``; with a short PATH the same tree and the
same staged change reported ``✓ tests (full)``. A skip is the weak direction —
the gate claims success without grading — so gate strength became a function
of a directory ordering nobody wrote down.

Pinned here:
  * the same repo yields the same verdict under both PATH shapes, end to end
    (real subprocesses, no stubs);
  * resolution prefers the repo's own environment and the configured
    ``guards.test_command`` before any PATH lookup;
  * when the first PATH interpreter lacks pytest and a later one has it, the
    lane USES the capable one instead of skipping;
  * when none can import pytest, the skip names every interpreter probed.
"""

import os
from pathlib import Path

import pytest

from engine.guard_manager import (
    GuardManager,
    _probe_interpreter_cached,
    _resolve_pytest_interpreter,
)

# A shim that CLAIMS pytest (the probe runs `<exe> -c "... find_spec"`) and
# reports a green pytest run when asked to run one. It is a shell script, not a
# real interpreter: the lane only needs the probe verdict and the exit code.
_CAPABLE_PYTHON = """#!/bin/sh
if [ "$1" = "-c" ]; then
  exit 0
fi
echo "================== test session starts =================="
echo "1 passed in 0.00s"
exit 0
"""

# A shim that cannot import pytest, and whose `-m pytest` run dies with the
# interpreter's own line — the exact shape the runner-missing classifier reads.
_NO_PYTEST_PYTHON = """#!/bin/sh
if [ "$1" = "-c" ]; then
  exit 3
fi
echo "$0: No module named pytest" >&2
exit 1
"""


def _write_shim(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(0o755)
    return path


def _capable_interpreter(directory: Path, name: str = "python") -> Path:
    return _write_shim(directory / name, _CAPABLE_PYTHON)


def _no_pytest_interpreter(directory: Path, name: str = "python3") -> Path:
    return _write_shim(directory / name, _NO_PYTEST_PYTHON)


def _repo_venv(repo: str) -> Path:
    """The repo-local environment resolution must prefer."""
    return _capable_interpreter(Path(repo) / ".venv" / "bin", "python")


def _prepend_path(monkeypatch: pytest.MonkeyPatch, dirs: list[str]) -> None:
    monkeypatch.setenv("PATH", os.pathsep.join(dirs))


def _run_lane(repo: str, cmd: str, config_extra: dict | None = None):
    guards = {"test_command": cmd, "scanner_nice": 0}
    guards.update(config_extra or {})
    gm = GuardManager(repo, {"guards": guards})
    return gm._run_test_command(cmd, "tests (full)")


# ─── resolution order (criterion 2) ─────────────────────────────────────────


class TestResolutionOrder:
    """Repo-local first, configured pin honoured, PATH probed not trusted."""

    def test_repo_local_venv_wins_over_path_interpreters(
        self, tmp_workdir: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`.venv/bin/python` is preferred even when PATH has a capable one.

        Criterion 2: a repo-local environment outranks every PATH lookup, so a
        globally installed pytest can never decide the lane for this repo.
        """
        venv_python = _repo_venv(tmp_workdir)
        path_bin = Path(tmp_workdir) / "path-bin"
        path_python = _capable_interpreter(path_bin, "python3")
        _prepend_path(monkeypatch, [str(path_bin)])

        resolution = _resolve_pytest_interpreter("python3 -m pytest -x", tmp_workdir)

        assert resolution.interpreter == str(venv_python)
        assert str(venv_python) in resolution.command
        assert str(path_python) not in resolution.command
        assert resolution.warning and "repo-local environment" in resolution.warning

    def test_first_path_interpreter_without_pytest_falls_through_to_capable(
        self, tmp_workdir: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Criterion 3: the first `python3` on PATH is not taken on trust.

        Without a repo-local env, the scan walks PATH and picks the FIRST
        interpreter that can import pytest — here the second one — instead of
        the one the shell would have run.
        """
        first = _no_pytest_interpreter(Path(tmp_workdir) / "first-bin", "python3")
        second = _capable_interpreter(Path(tmp_workdir) / "second-bin", "python3")
        _prepend_path(monkeypatch, [str(first.parent), str(second.parent)])

        resolution = _resolve_pytest_interpreter("python3 -m pytest", tmp_workdir)

        assert resolution.interpreter == str(second)
        assert resolution.command == f"{second} -m pytest"
        assert resolution.considered[:2] == (str(first), str(second))
        assert "pytest present" in (resolution.warning or "")

    def test_no_capable_interpreter_leaves_command_and_names_all_considered(
        self, tmp_workdir: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Nothing can import pytest → command untouched, every candidate named."""
        first = _no_pytest_interpreter(Path(tmp_workdir) / "first-bin", "python3")
        second = _no_pytest_interpreter(Path(tmp_workdir) / "second-bin", "python3")
        _prepend_path(monkeypatch, [str(first.parent), str(second.parent)])

        resolution = _resolve_pytest_interpreter("python3 -m pytest -x", tmp_workdir)

        assert resolution.interpreter is None
        assert resolution.command == "python3 -m pytest -x"
        assert set(resolution.considered) == {str(first), str(second)}

    def test_configured_pinned_interpreter_is_left_verbatim(
        self, tmp_workdir: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Criterion 2: `guards.test_command`'s own pin is never second-guessed."""
        _repo_venv(tmp_workdir)  # even with a capable repo env present
        _prepend_path(monkeypatch, [str(Path(tmp_workdir) / "path-bin")])

        for command in (
            ".venv/bin/python -m pytest -x",
            "/usr/bin/python3 -m pytest -x",
        ):
            resolution = _resolve_pytest_interpreter(command, tmp_workdir)
            assert resolution.command == command
            assert resolution.interpreter is None
            assert resolution.considered == ()

    def test_runner_prefixed_command_is_left_verbatim(
        self, tmp_workdir: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`uv run python3 -m pytest` belongs to GR-GAP-037, not to this scan."""
        _repo_venv(tmp_workdir)

        for command in ("uv run python3 -m pytest -x", "poetry run python3 -m pytest"):
            resolution = _resolve_pytest_interpreter(command, tmp_workdir)
            assert resolution.command == command
            assert resolution.interpreter is None

    def test_chained_command_resolves_each_segment(
        self, tmp_workdir: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Every segment of a chained test_command is resolved, separators kept."""
        venv_python = _repo_venv(tmp_workdir)
        command = "python3 scripts/check.py && python3 -m pytest -x"

        resolution = _resolve_pytest_interpreter(command, tmp_workdir)

        assert resolution.command == (f"python3 scripts/check.py && {venv_python} -m pytest -x")
        assert resolution.interpreter == str(venv_python)

    def test_probe_is_cached_per_interpreter(
        self, tmp_workdir: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One probe per interpreter, not one per resolution.

        The scan may walk dozens of PATH entries; probing each one on every
        look would make the lane cost a subprocess per directory per run.
        """
        from engine import guard_manager

        venv_python = _repo_venv(tmp_workdir)
        calls: list[str] = []
        real_probe = guard_manager._pytest_interpreter_has_pytest

        def _counting_probe(exe: str, workdir: str, timeout: float = 30) -> bool | None:
            calls.append(exe)
            return real_probe(exe, workdir, timeout=timeout)

        monkeypatch.setattr(guard_manager, "_pytest_interpreter_has_pytest", _counting_probe)
        guard_manager._PYTEST_PROBE_CACHE.clear()

        first = _probe_interpreter_cached(str(venv_python), tmp_workdir)
        again = _probe_interpreter_cached(str(venv_python), tmp_workdir)

        assert first is True
        assert again is True
        assert calls == [str(venv_python)]


# ─── end-to-end lane verdicts (criteria 1 and 3) ────────────────────────────

_SHORT_PATH = ["/usr/local/bin", "/usr/bin", "/bin"]


class TestLaneVerdictIsPathIndependent:
    """The lane grades the SAME way no matter how PATH is shaped."""

    def test_same_verdict_under_short_and_40_dir_paths(
        self, tmp_workdir: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Criterion 1: 45-entry ambient PATH vs the short PATH — same verdict.

        The ambient shape carries 40 leading venv bin dirs whose `python3`
        cannot import pytest (the host that produced the dogfood defect). Both
        shapes must grade `✓ tests (full)`, not one ✓ and one skip.
        """
        _repo_venv(tmp_workdir)
        ambient_dirs: list[str] = []
        for index in range(40):
            directory = Path(tmp_workdir) / f"runtime-venv-{index}" / "bin"
            _no_pytest_interpreter(directory, "python3")
            ambient_dirs.append(str(directory))

        verdicts = []
        for path_dirs in (ambient_dirs + _SHORT_PATH, _SHORT_PATH):
            _prepend_path(monkeypatch, path_dirs)
            result = _run_lane(tmp_workdir, "python3 -m pytest -x --tb=short")
            verdicts.append((result.passed, result.skipped, result.exit_code, result.skip_reason))

        assert verdicts[0] == verdicts[1] == (True, False, 0, "")
        # The run must not have leaned on the PATH interpreter to fail/skip.
        assert "resolved to" in result.warning
        assert ".venv/bin/python" in result.warning

    def test_capable_interpreter_is_used_instead_of_skipping(
        self, tmp_workdir: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Criterion 3: first PATH interpreter lacks pytest, the second has it.

        Without a repo-local env the lane must still grade: it picks the capable
        interpreter on PATH rather than skipping on the first one.
        """
        first = _no_pytest_interpreter(Path(tmp_workdir) / "first-bin", "python3")
        second = _capable_interpreter(Path(tmp_workdir) / "second-bin", "python3")
        _prepend_path(monkeypatch, [str(first.parent), str(second.parent)] + _SHORT_PATH)

        result = _run_lane(tmp_workdir, "python3 -m pytest -x --tb=short")

        assert result.passed is True
        assert result.skipped is False
        assert result.exit_code == 0
        assert str(second) in (result.warning or "")

    def test_no_capable_interpreter_skips_naming_every_interpreter(
        self, tmp_workdir: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Criterion 3's other arm: a genuine gap stays loud and names the set.

        No interpreter on PATH can import pytest — the lane runs the command
        unchanged, the shell's first pick reports `No module named pytest`, and
        the skip reason names BOTH probed interpreters instead of only the one
        the shell happened to run.
        """
        first = _no_pytest_interpreter(Path(tmp_workdir) / "first-bin", "python3")
        second = _no_pytest_interpreter(Path(tmp_workdir) / "second-bin", "python3")
        # No real system directories on PATH: the set of candidates is exactly
        # the two shims, so the assertion cannot be satisfied by a host python.
        _prepend_path(monkeypatch, [str(first.parent), str(second.parent)])

        result = _run_lane(tmp_workdir, "python3 -m pytest -x --tb=short")

        assert result.passed is True
        assert result.skipped is True
        assert result.exit_code == 1
        reason = result.skip_reason or ""
        assert "pytest is not installed in" in reason
        assert str(first) in reason
        assert str(second) in reason
