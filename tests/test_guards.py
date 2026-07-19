"""Dedicated tests for Go guard checks."""

import subprocess
from types import SimpleNamespace
from unittest.mock import call, patch

import pytest

from engine.guards import (
    GoGuardResult,
    check_go_build,
    check_go_lint,
    check_go_tests,
    is_go_project,
)


def completed(stdout="", stderr="", returncode=0):
    return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode)


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
    with patch("engine.guards.subprocess.run", return_value=completed("README.md\n")) as run:
        result = checker("/repo")

    assert result == GoGuardResult(name=name, passed=True, output="No Go files staged")
    run.assert_called_once_with(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"],
        capture_output=True,
        text=True,
        timeout=10,
        cwd="/repo",
    )


def test_check_go_lint_uses_golangci_lint_when_it_passes():
    with patch(
        "engine.guards.subprocess.run",
        side_effect=[completed("main.go\npkg/lib.go\n"), completed()],
    ) as run:
        result = check_go_lint("/repo")

    assert result == GoGuardResult(
        name="go_lint", passed=True, output="golangci-lint: clean"
    )
    assert run.call_args_list[1] == call(
        [
            "golangci-lint",
            "run",
            "--new-from-rev=HEAD~1",
            "main.go",
            "pkg/lib.go",
        ],
        capture_output=True,
        text=True,
        timeout=60,
        cwd="/repo",
    )


@pytest.mark.parametrize("lint_result", [FileNotFoundError(), completed(returncode=1)])
def test_check_go_lint_falls_back_to_go_vet(lint_result):
    with patch(
        "engine.guards.subprocess.run",
        side_effect=[completed("main.go\n"), lint_result, completed()],
    ) as run:
        result = check_go_lint("/repo")

    assert result == GoGuardResult(name="go_lint", passed=True, output="go vet: clean")
    assert run.call_args_list[-1] == call(
        ["go", "vet", "./..."],
        capture_output=True,
        text=True,
        timeout=60,
        cwd="/repo",
    )


def test_check_go_lint_returns_truncated_vet_failure():
    output = "out" + "e" * 2100
    with patch(
        "engine.guards.subprocess.run",
        side_effect=[completed("main.go\n"), FileNotFoundError(), completed("out", "e" * 2100, 1)],
    ):
        result = check_go_lint("/repo")

    assert result.passed is False
    assert result.output == output[:2000] + "\n... [truncated]"


def test_check_go_lint_reports_vet_exception_as_error():
    with patch(
        "engine.guards.subprocess.run",
        side_effect=[completed("main.go\n"), FileNotFoundError(), OSError("go unavailable")],
    ):
        result = check_go_lint("/repo")

    assert result == GoGuardResult(name="go_lint", passed=False, error="go unavailable")


def test_check_go_tests_returns_bounded_success_output():
    output = "a" * 600
    with patch(
        "engine.guards.subprocess.run",
        side_effect=[completed("main.go\n"), completed(output)],
    ):
        result = check_go_tests("/repo")

    assert result == GoGuardResult(name="go_tests", passed=True, output=output[:500])


def test_check_go_tests_keeps_tail_of_failure_output():
    output = "x" * 2100 + "failure"
    with patch(
        "engine.guards.subprocess.run",
        side_effect=[completed("main.go\n"), completed("x" * 2100, "failure", 1)],
    ):
        result = check_go_tests("/repo")

    assert result == GoGuardResult(name="go_tests", passed=False, output=output[-2000:])


def test_check_go_tests_handles_timeout_and_other_exceptions():
    timeout = subprocess.TimeoutExpired(["go", "test"], 180)
    with patch(
        "engine.guards.subprocess.run",
        side_effect=[completed("main.go\n"), timeout],
    ):
        timed_out = check_go_tests("/repo")
    with patch(
        "engine.guards.subprocess.run",
        side_effect=[completed("main.go\n"), OSError("go unavailable")],
    ):
        errored = check_go_tests("/repo")

    assert timed_out == GoGuardResult(
        name="go_tests", passed=False, output="Tests timed out after 180s"
    )
    assert errored == GoGuardResult(name="go_tests", passed=False, error="go unavailable")


def test_check_go_build_returns_success_and_expected_command():
    with patch(
        "engine.guards.subprocess.run",
        side_effect=[completed("main.go\n"), completed()],
    ) as run:
        result = check_go_build("/repo")

    assert result == GoGuardResult(name="go_build", passed=True, output="go build: ok")
    assert run.call_args_list[1] == call(
        ["go", "build", "-buildvcs=false", "./..."],
        capture_output=True,
        text=True,
        timeout=120,
        cwd="/repo",
    )


def test_check_go_build_returns_truncated_failure_or_exception():
    output = "o" * 1000 + "e" * 1100
    with patch(
        "engine.guards.subprocess.run",
        side_effect=[completed("main.go\n"), completed("o" * 1000, "e" * 1100, 1)],
    ):
        failed = check_go_build("/repo")
    with patch(
        "engine.guards.subprocess.run",
        side_effect=[completed("main.go\n"), OSError("build unavailable")],
    ):
        errored = check_go_build("/repo")

    assert failed == GoGuardResult(
        name="go_build",
        passed=False,
        output=output[:2000] + "\n... [truncated]",
    )
    assert errored == GoGuardResult(name="go_build", passed=False, error="build unavailable")
