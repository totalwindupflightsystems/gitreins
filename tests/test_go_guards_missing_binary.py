"""DF-GITREINS-POC-46: a Go lane whose binary is not on PATH says so.

The defect these tests pin. ``check_go_build`` / ``check_go_lint`` /
``check_go_tests`` return ``GoGuardResult(passed=False, error=result["error"])``
when the spawn fails because the binary is not on PATH, but
``GuardResult.summary`` rendered ``passed``/``output`` and never ``error`` — so
the console printed a bare ``✗`` and the cause
(``[Errno 2] No such file or directory: 'go'``) reached only the run log
(``.gitreins/logs/guard-*.log``). The Python lane solved this class twice on
purpose (``_resolve_test_command`` GR-GAP-037 names the missing runner;
``_pytest_not_found_hint`` names the interpreter), so the gap was lane-local.

Fixed at three points, each asserted here:

1. the Go guards name the missing binary and an install hint in ``error``;
2. ``GuardResult.summary`` renders that ``error`` on the step's own console
   line (an install hint the operator can act on without opening the log);
3. the golangci-lint-absent fallback carries a ``warning`` naming the absent
   linter and its install hint, so a green graded by ``go vet`` cannot read as
   a green golangci-lint run.

Hermetic. The missing-binary tests need NO toolchain at all — every spawn
under test is the one that fails — so they run on a bare box and on a box with
Go installed (the binary is removed from ``PATH`` for the call). Only the
``go vet`` fallback leg touches a real toolchain, and it asserts the warning
(which is present on both the fallback-pass and the fallback-failure shape)
rather than the tool's verdict.
"""

import os
import shutil
import subprocess
import sys

import pytest

from engine.guard_manager import _go_guard_result
from engine.guards import (
    _missing_binary_message,
    _spawn_error_message,
    check_go_build,
    check_go_lint,
    check_go_tests,
)
from engine.types import Tier1Result

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLI_SCRIPT = os.path.join(PROJECT_ROOT, "gitreins", "cli.py")

GO_HINT = "https://go.dev/doc/install"
LINT_HINT = "https://golangci-lint.run/usage/install/"
# The exact shape run_bounded surfaces for a failed Popen (str(OSError)).
ENOENT_GO = "[Errno 2] No such file or directory: 'go'"

GO_MOD = "module example.com/poc46\n\ngo 1.21\n"
CLEAN_GO = "package quota\n\n// Clean returns a real int.\nfunc Clean() int {\n\treturn 0\n}\n"


# ── Helpers ───────────────────────────────────────────────────────


def _write(workdir: str, relpath: str, content: str) -> None:
    path = os.path.join(workdir, relpath)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as handle:
        handle.write(content)


def _path_without(programs: tuple[str, ...], mirror_root: str) -> str:
    """PATH with every occurrence of *programs* hidden.

    Directories are NOT dropped when they expose a banned program: on this
    class of host ``/usr/bin`` holds both ``go`` and ``git``, so dropping the
    directory would take ``git`` (the guard's own change-scope discovery) with
    it. Such a directory is replaced by a mirror holding a symlink for every
    entry EXCEPT the banned ones — nothing else on the machine disappears, and
    the missing-binary condition is real (the name genuinely does not resolve).

    A box that already lacks the programs keeps its PATH unchanged.
    """
    seen = set()
    kept = []
    for index, directory in enumerate(os.environ.get("PATH", "").split(os.pathsep)):
        if not directory or directory in seen:
            continue
        seen.add(directory)
        banned = [p for p in programs if os.path.isfile(os.path.join(directory, p))]
        if not banned:
            kept.append(directory)
            continue
        mirror = os.path.join(mirror_root, f"dir{index}")
        os.makedirs(mirror, exist_ok=True)
        try:
            names = os.listdir(directory)
        except OSError:
            continue
        for name in names:
            if name in banned:
                continue
            try:
                os.symlink(os.path.join(directory, name), os.path.join(mirror, name))
            except OSError:
                pass
        kept.append(mirror)
    return os.pathsep.join(kept)


def _git_env() -> dict:
    """Environment without leaked GIT_* vars (DF-008)."""
    return {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}


def _cli_env() -> dict:
    """Environment for a CLI subprocess: no GIT_* leak, no live-LLM opt-in."""
    env = {k: v for k, v in _git_env().items() if not k.startswith("GITREINS_")}
    env["PYTHONPATH"] = PROJECT_ROOT + (":" + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    return env


def _git(workdir: str, *args: str) -> None:
    subprocess.run(["git", *args], cwd=workdir, capture_output=True, check=True, env=_git_env())


def _go_repo(tmp_path, name: str) -> str:
    """A real git repo holding a small Go module, clean index afterwards."""
    workdir = tmp_path / name
    workdir.mkdir()
    _write(str(workdir), "go.mod", GO_MOD)
    _write(str(workdir), "main.go", CLEAN_GO)
    _git(str(workdir), "init", "-q")
    _git(str(workdir), "config", "user.email", "poc46@example.invalid")
    _git(str(workdir), "config", "user.name", "POC-46")
    _git(str(workdir), "add", "-A")
    _git(str(workdir), "commit", "-qm", "init")
    return str(workdir)


def _run_cli(*args, cwd=None):
    return subprocess.run(
        [sys.executable, CLI_SCRIPT] + list(args),
        capture_output=True,
        text=True,
        timeout=180,
        cwd=cwd,
        env=_cli_env(),
    )


@pytest.fixture
def no_go_path(monkeypatch, tmp_path_factory) -> str:
    """A PATH with neither ``go`` nor ``golangci-lint`` on it.

    ``git`` must stay resolvable: the guard's own change-scope discovery shells
    out to it.
    """
    path = _path_without(("go", "golangci-lint"), str(tmp_path_factory.mktemp("poc46-path")))
    assert shutil.which("go", path=path) is None, "the fixture must hide go"
    assert shutil.which("golangci-lint", path=path) is None
    if shutil.which("git", path=path) is None:
        pytest.skip("git would not resolve on the stripped PATH")
    monkeypatch.setenv("PATH", path)
    return path


@pytest.fixture
def no_lint_path(monkeypatch, tmp_path_factory) -> str:
    """A PATH with ``golangci-lint`` hidden but everything else intact."""
    path = _path_without(("golangci-lint",), str(tmp_path_factory.mktemp("poc46-lintpath")))
    assert shutil.which("golangci-lint", path=path) is None, "the fixture must hide the linter"
    monkeypatch.setenv("PATH", path)
    return path


# ── 1. The guard names the missing binary + install hint ──────────


class TestMissingGoBinaryIsNamed:
    def test_go_build_error_names_go_and_the_install_hint(self, tmp_path, no_go_path) -> None:
        """RED-PROOF: pre-fix the error was the raw
        ``[Errno 2] No such file or directory: 'go'`` and the console showed a
        bare ✗ — no binary named, no hint."""
        workdir = tmp_path / "build"
        workdir.mkdir()
        _write(str(workdir), "main.go", CLEAN_GO)

        result = check_go_build(str(workdir), changed_files=["main.go"])

        assert result.passed is False
        assert result.skipped is False
        assert "'go' is not on PATH" in result.error, result.error
        assert GO_HINT in result.error, result.error

    def test_go_tests_error_names_go_and_the_install_hint(self, tmp_path, no_go_path) -> None:
        workdir = tmp_path / "tests"
        workdir.mkdir()
        _write(str(workdir), "main.go", CLEAN_GO)

        result = check_go_tests(str(workdir), changed_files=["main.go"])

        assert result.passed is False
        assert "'go' is not on PATH" in result.error, result.error
        assert GO_HINT in result.error, result.error

    def test_go_lint_without_the_toolchain_names_both_binaries(self, tmp_path, no_go_path) -> None:
        """golangci-lint absent → the lane falls back to ``go vet``; with no
        toolchain at all the vet spawn fails too, so the lane carries BOTH
        diagnostics: the missing grader (error) and the absent linter
        (warning)."""
        workdir = tmp_path / "lint"
        workdir.mkdir()
        _write(str(workdir), "main.go", CLEAN_GO)

        result = check_go_lint(str(workdir), changed_files=["main.go"])

        assert result.passed is False
        assert "'go' is not on PATH" in result.error, result.error
        assert GO_HINT in result.error, result.error
        assert "'golangci-lint' is not on PATH" in result.warning, result.warning
        assert LINT_HINT in result.warning, result.warning


# ── 2. The diagnostics reach the console summary ──────────────────


class TestDiagnosticReachesTheConsoleSummary:
    def test_summary_renders_the_error_and_the_warning(self, tmp_path, no_go_path) -> None:
        """The console line, not just the result object: both hints are in
        ``Tier1Result.summary`` — the only thing `gitreins guard` prints for a
        step (the run log keeps the untruncated output)."""
        workdir = tmp_path / "summary"
        workdir.mkdir()
        _write(str(workdir), "main.go", CLEAN_GO)

        lane = check_go_lint(str(workdir), changed_files=["main.go"])
        summary = Tier1Result(passed=False, results=[_go_guard_result(lane)]).summary

        assert "✗ go_lint" in summary, summary
        assert "'go' is not on PATH" in summary, summary
        assert GO_HINT in summary, summary
        assert "⚠ 'golangci-lint' is not on PATH" in summary, summary
        assert LINT_HINT in summary, summary

    def test_spawn_error_message_rewrites_only_the_missing_binary_shape(self) -> None:
        """A non-ENOENT failure (a permission error, a refused busy-wait) keeps
        its own words — claiming "not on PATH" there would name the wrong fix."""
        assert _spawn_error_message(ENOENT_GO) == _missing_binary_message("go")
        # Path-qualified program: the hint names the binary, not the path.
        assert _spawn_error_message(
            "[Errno 2] No such file or directory: '/opt/bin/go'"
        ) == _missing_binary_message("go")
        assert _spawn_error_message("Permission denied: './go'") == "Permission denied: './go'"
        assert _spawn_error_message("refused: busy-wait loop detected") == (
            "refused: busy-wait loop detected"
        )

    def test_unknown_program_still_gets_a_named_line(self) -> None:
        """The escape hatch a configured ``guards.test_command`` runner needs:
        an unknown binary is still named, with a usable fix — never a bare
        errno string."""
        message = _spawn_error_message("[Errno 2] No such file or directory: 'mytap'")

        assert "'mytap' is not on PATH" in message
        assert "guards.test_command" in message


# ── 3. The golangci-lint-absent fallback warns on the console ─────


class TestGolangciLintAbsentFallback:
    def test_golangci_lint_absent_warns_naming_the_linter_and_hint(
        self, tmp_path, no_lint_path
    ) -> None:
        """A green graded by ``go vet`` must still name the absent linter.

        The warning is asserted rather than the lane's verdict: whether
        ``go vet`` itself can grade this tree depends on the host's toolchain,
        and the diagnostic must be present either way (it rides all three
        fallback exits)."""
        workdir = tmp_path / "lint-fallback"
        workdir.mkdir()
        _write(str(workdir), "main.go", CLEAN_GO)

        lane = check_go_lint(str(workdir), changed_files=["main.go"])

        assert "'golangci-lint' is not on PATH" in lane.warning, lane.warning
        assert LINT_HINT in lane.warning, lane.warning
        assert "go vet" in lane.warning, lane.warning

        summary = Tier1Result(passed=lane.passed, results=[_go_guard_result(lane)]).summary
        assert "⚠" in summary, summary
        assert LINT_HINT in summary, summary

    def test_clean_golangci_lint_run_carries_no_warning(self, tmp_path) -> None:
        """Control: when the linter RAN, there is nothing to warn about — the
        warning is a missing-tool signal, not a permanent note."""
        from unittest.mock import patch

        from engine.guards import GoGuardResult

        workdir = tmp_path / "lint-clean"
        workdir.mkdir()
        _write(str(workdir), "main.go", CLEAN_GO)

        with patch(
            "engine.guards.command_hygiene.run_bounded",
            return_value={"cmd": ["golangci-lint"], "output": "", "exit_code": 0, "pgid": 1},
        ):
            lane = check_go_lint(str(workdir), changed_files=["main.go"])

        assert lane == GoGuardResult(name="go_lint", passed=True, output="golangci-lint: clean")
        assert lane.warning == ""


# ── 4. The real CLI console ───────────────────────────────────────


class TestCliConsoleNamesTheMissingToolchain:
    def test_gitreins_guard_prints_the_go_and_linter_hints(self, tmp_path, no_go_path) -> None:
        """Acceptance criterion: `gitreins guard` on a staged ``.go`` file with
        no Go toolchain on PATH prints console lines naming ``go`` and
        ``golangci-lint``, each with its install hint."""
        workdir = _go_repo(tmp_path, "cli-missing-go")
        _write(workdir, "staged.go", CLEAN_GO)
        _git(workdir, "add", "staged.go")
        # The CLI refuses to run without an initialized config; only the Go
        # lanes are in play (they are what this row is about).
        _write(
            workdir,
            os.path.join(".gitreins", "config.yaml"),
            "guards:\n"
            "  secrets: false\n"
            "  lint: false\n"
            "  tests: false\n"
            "  lsp: false\n"
            "  go: {build: true, lint: true, tests: true}\n",
        )

        result = _run_cli("guard", cwd=workdir)

        assert result.returncode == 1, f"stdout={result.stdout} stderr={result.stderr}"
        assert "Tier 1 Guards: FAIL" in result.stdout
        # AC1: `go` named, with an install hint, on the lane's own console line.
        assert "'go' is not on PATH" in result.stdout, result.stdout
        assert GO_HINT in result.stdout, result.stdout
        # AC2: golangci-lint named, with its install hint.
        assert "'golangci-lint' is not on PATH" in result.stdout, result.stdout
        assert LINT_HINT in result.stdout, result.stdout
        # The diagnostic is a per-lane console line, not a bare ✗.
        assert "✗ go_build" in result.stdout
        assert "✗ go_tests" in result.stdout
