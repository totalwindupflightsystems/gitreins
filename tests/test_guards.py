"""Dedicated tests for Go guard checks."""

import os
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from engine.guards import (
    GoGuardResult,
    _sanitized_env,
    check_go_build,
    check_go_lint,
    check_go_tests,
    is_go_project,
)


def completed(stdout="", stderr="", returncode=0):
    return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode)


def lane(exit_code=0, output="", error=None, timed_out=False):
    """A command_hygiene.run_bounded-shaped result (DF-CRIER-258 seam).

    The Go guards execute their tools through engine.command_hygiene.run_bounded,
    so tests stub that seam (not subprocess.run) for the command calls;
    subprocess.run remains the seam for the `git diff --cached` staging query.
    """
    result = {
        "cmd": ["stubbed"],
        "output": output,
        "timed_out": timed_out,
        "pgid": 424242,
    }
    if error is not None:
        # Spawn-failure shape: run_bounded returns {"cmd", "error"} with no
        # exit_code when Popen itself fails (e.g. binary not on PATH).
        result["error"] = error
    else:
        result["exit_code"] = -9 if timed_out else exit_code
    return result


def test_go_guard_result_defaults():
    result = GoGuardResult(name="go_build", passed=True)

    assert result.output == ""
    assert result.error == ""


def test_is_go_project_requires_go_mod_file(tmp_path):
    assert is_go_project(str(tmp_path)) is False

    (tmp_path / "go.mod").mkdir()
    assert is_go_project(str(tmp_path)) is False

    (tmp_path / "go.mod").rmdir()
    (tmp_path / "go.mod").write_text("module example.test\n")
    assert is_go_project(str(tmp_path)) is True


@pytest.mark.parametrize(
    ("checker", "name"),
    [
        (check_go_lint, "go_lint"),
        (check_go_tests, "go_tests"),
        (check_go_build, "go_build"),
    ],
)
def test_checkers_skip_when_no_go_files_are_staged(checker, name):
    """DF-GITREINS-POC-42: a lane that graded no file is a SKIP, not a silent
    pass — the historical wording stays, the skip signal is now recorded."""
    with patch("engine.guards.subprocess.run", return_value=completed("README.md\n")) as run:
        result = checker("/repo")

    assert result == GoGuardResult(
        name=name,
        passed=True,
        output="No Go files staged",
        skipped=True,
        skip_reason="No Go files staged",
    )
    run.assert_called_once_with(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"],
        capture_output=True,
        text=True,
        timeout=10,
        cwd="/repo",
        env={k: v for k, v in os.environ.items() if not k.startswith("GIT_")},
    )


def test_check_go_lint_uses_golangci_lint_when_it_passes():
    with (
        patch(
            "engine.guards.subprocess.run",
            return_value=completed("main.go\npkg/lib.go\n"),
        ),
        patch(
            "engine.guards.command_hygiene.run_bounded",
            return_value=lane(0),
        ) as run,
    ):
        result = check_go_lint("/repo")

    assert result == GoGuardResult(name="go_lint", passed=True, output="golangci-lint: clean")
    assert run.call_args.args[0] == [
        "golangci-lint",
        "run",
        "--new-from-rev=HEAD~1",
        "main.go",
        "pkg/lib.go",
    ]
    assert run.call_args.kwargs["timeout"] == 60
    assert run.call_args.kwargs["cwd"] == "/repo"
    assert "GIT_INDEX_FILE" not in run.call_args.kwargs["env"]


@pytest.mark.parametrize("lint_exit", [1, None])
def test_check_go_lint_falls_back_to_go_vet(lint_exit):
    """golangci-lint failure (exit 1) or spawn failure (no exit_code) → go vet."""
    first = lane(1) if lint_exit is not None else lane(error="[Errno 2] golangci-lint")
    with (
        patch(
            "engine.guards.subprocess.run",
            return_value=completed("main.go\n"),
        ),
        patch(
            "engine.guards.command_hygiene.run_bounded",
            side_effect=[first, lane(0)],
        ) as run,
    ):
        result = check_go_lint("/repo")

    assert result == GoGuardResult(name="go_lint", passed=True, output="go vet: clean")
    assert run.call_args_list[-1].args[0] == ["go", "vet", "./..."]
    assert run.call_args_list[-1].kwargs["timeout"] == 60


def test_check_go_lint_returns_truncated_vet_failure():
    output = "out" + "e" * 2100
    with (
        patch(
            "engine.guards.subprocess.run",
            return_value=completed("main.go\n"),
        ),
        patch(
            "engine.guards.command_hygiene.run_bounded",
            side_effect=[lane(error="[Errno 2] golangci-lint"), lane(1, output)],
        ),
    ):
        result = check_go_lint("/repo")

    assert result.passed is False
    assert result.output == output[:2000] + "\n... [truncated]"


def test_check_go_lint_reports_vet_spawn_failure_as_error():
    with (
        patch(
            "engine.guards.subprocess.run",
            return_value=completed("main.go\n"),
        ),
        patch(
            "engine.guards.command_hygiene.run_bounded",
            side_effect=[lane(error="[Errno 2] golangci-lint"), lane(error="go unavailable")],
        ),
    ):
        result = check_go_lint("/repo")

    assert result == GoGuardResult(name="go_lint", passed=False, error="go unavailable")


def test_check_go_tests_returns_bounded_success_output():
    output = "a" * 600
    with (
        patch(
            "engine.guards.subprocess.run",
            return_value=completed("main.go\n"),
        ),
        patch(
            "engine.guards.command_hygiene.run_bounded",
            return_value=lane(0, output),
        ),
    ):
        result = check_go_tests("/repo")

    assert result == GoGuardResult(name="go_tests", passed=True, output=output[:500])


def test_check_go_tests_keeps_tail_of_failure_output():
    output = "x" * 2100 + "failure"
    with (
        patch(
            "engine.guards.subprocess.run",
            return_value=completed("main.go\n"),
        ),
        patch(
            "engine.guards.command_hygiene.run_bounded",
            return_value=lane(1, output),
        ),
    ):
        result = check_go_tests("/repo")

    assert result == GoGuardResult(name="go_tests", passed=False, output=output[-2000:])


def test_check_go_tests_handles_timeout_and_other_exceptions():
    with (
        patch(
            "engine.guards.subprocess.run",
            return_value=completed("main.go\n"),
        ),
        patch(
            "engine.guards.command_hygiene.run_bounded",
            return_value=lane(timed_out=True),
        ),
    ):
        timed_out = check_go_tests("/repo")
    with (
        patch(
            "engine.guards.subprocess.run",
            return_value=completed("main.go\n"),
        ),
        patch(
            "engine.guards.command_hygiene.run_bounded",
            return_value=lane(error="go unavailable"),
        ),
    ):
        errored = check_go_tests("/repo")

    assert timed_out == GoGuardResult(
        name="go_tests",
        passed=False,
        output="Tests timed out after 180s (guards.test_timeout). Raise it in .gitreins/config.yaml — e.g. test_timeout: 900 for large projects with slow integration suites.",
    )
    assert errored == GoGuardResult(name="go_tests", passed=False, error="go unavailable")


def test_check_go_tests_coerces_string_timeout():
    """GR-GAP-028: a string timeout ('300s') must be coerced to int before
    subprocess.run — the raw string raises TypeError, not TimeoutExpired
    (Kobayashi-Maru ticks 240-242 crashed fleet-wide on test_timeout: 300s)."""
    with (
        patch(
            "engine.guards.subprocess.run",
            return_value=completed("main.go\n"),
        ),
        patch(
            "engine.guards.command_hygiene.run_bounded",
            return_value=lane(0, "ok"),
        ) as run,
    ):
        result = check_go_tests("/repo", timeout="300s")

    assert result == GoGuardResult(name="go_tests", passed=True, output="ok")
    # The `go test` call — timeout must be the coerced int
    assert run.call_args.kwargs["timeout"] == 300


def test_check_go_tests_rejects_garbage_timeout():
    """GR-GAP-028: non-numeric timeout values raise a clear ValueError
    naming the config key instead of crashing deep inside subprocess."""
    with pytest.raises(ValueError, match="test_timeout"):
        check_go_tests("/repo", timeout="asap")


def test_check_go_build_returns_success_and_expected_command():
    with (
        patch(
            "engine.guards.subprocess.run",
            return_value=completed("main.go\n"),
        ),
        patch(
            "engine.guards.command_hygiene.run_bounded",
            return_value=lane(0),
        ) as run,
    ):
        result = check_go_build("/repo")

    assert result == GoGuardResult(name="go_build", passed=True, output="go build: ok")
    assert run.call_args.args[0] == ["go", "build", "-buildvcs=false", "./..."]
    assert run.call_args.kwargs["timeout"] == 120


def test_sanitized_env_strips_all_git_vars():
    """GIT_* leaked by the pre-commit hook must not reach go subprocesses."""
    with patch.dict(
        os.environ,
        {
            "GIT_INDEX_FILE": "/repo/.git/index",
            "GIT_DIR": "/repo/.git",
            "GIT_WORK_TREE": "/repo",
            "PATH": "/usr/bin",
            "HOME": "/home/test",
        },
        clear=True,
    ):
        env = _sanitized_env()
    assert "GIT_INDEX_FILE" not in env
    assert "GIT_DIR" not in env
    assert "GIT_WORK_TREE" not in env
    assert env["PATH"] == "/usr/bin"
    assert env["HOME"] == "/home/test"


def test_go_tests_sanitizes_env_even_with_git_index_file_leak():
    """DF-008 Go variant: go test subprocess must not inherit GIT_INDEX_FILE
    (breaks worktree tests: 'fatal: .git/index: index file open failed')."""
    with patch("engine.guards.subprocess.run", return_value=completed("main.go\n")) as run:
        check_go_tests("/repo")
    _, kwargs = run.call_args_list[0]
    assert "GIT_INDEX_FILE" not in kwargs["env"]
    assert "GIT_DIR" not in kwargs["env"]


def test_check_go_build_returns_truncated_failure_or_exception():
    output = "o" * 1000 + "e" * 1100
    with (
        patch(
            "engine.guards.subprocess.run",
            return_value=completed("main.go\n"),
        ),
        patch(
            "engine.guards.command_hygiene.run_bounded",
            return_value=lane(1, output),
        ),
    ):
        failed = check_go_build("/repo")
    with (
        patch(
            "engine.guards.subprocess.run",
            return_value=completed("main.go\n"),
        ),
        patch(
            "engine.guards.command_hygiene.run_bounded",
            return_value=lane(error="build unavailable"),
        ),
    ):
        errored = check_go_build("/repo")

    assert failed == GoGuardResult(
        name="go_build",
        passed=False,
        output=output[:2000] + "\n... [truncated]",
    )
    assert errored == GoGuardResult(name="go_build", passed=False, error="build unavailable")
