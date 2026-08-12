"""Go-specific guard checks for GitReins."""

import logging
import os
import re
import subprocess
from dataclasses import dataclass

logger = logging.getLogger("gitreins.guards.go")


def _sanitized_env() -> dict[str, str]:
    """Return the current environment with every GIT_* variable removed.

    Git exports GIT_INDEX_FILE (plus GIT_DIR, GIT_WORK_TREE, and friends) to
    pre-commit hooks. Leaking them into `go test` subprocesses breaks tests
    that exec git in temp repos or worktrees — the relative GIT_INDEX_FILE
    resolves against the wrong directory and `git worktree add` fails with
    `fatal: .git/index: index file open failed: Not a directory`. Same
    class as DF-008 (guard_manager.py, c24f29e) — the Go guards missed it.
    """
    return {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}


def _coerce_timeout(value, name: str, default: int) -> int:
    """Coerce a guard timeout config value to a positive int of seconds.

    YAML durations are commonly written with a unit suffix
    (``test_timeout: 300s``). Passing that string straight into
    subprocess.run(timeout=...) raises TypeError — not a clean
    TimeoutExpired — which crashed the full-suite go_tests stage and judge
    tier1 fleet-wide (Kobayashi-Maru ticks 240-242, GR-GAP-028).

    Leading digits are parsed ('300s' -> 300, '300' -> 300); None/missing
    -> default; garbage (no leading digits, <= 0, bool) -> ValueError with
    a message naming the config key.
    """
    if value is None:
        return default
    if isinstance(value, bool):
        raise ValueError(
            f"guards.{name} must be a positive number of seconds "
            f"(e.g. {name}: 300), got {value!r}"
        )
    if isinstance(value, str):
        match = re.match(r"^\s*(\d+)", value)
        if not match:
            raise ValueError(
                f"guards.{name} must be a positive number of seconds "
                f"(e.g. {name}: 300), got {value!r}"
            )
        value = int(match.group(1))
    else:
        try:
            value = int(value)
        except (TypeError, ValueError, OverflowError):
            raise ValueError(
                f"guards.{name} must be a positive number of seconds "
                f"(e.g. {name}: 300), got {value!r}"
            ) from None
    if value <= 0:
        raise ValueError(
            f"guards.{name} must be a positive number of seconds "
            f"(e.g. {name}: 300), got {value!r}"
        )
    return value


@dataclass
class GoGuardResult:
    name: str
    passed: bool
    output: str = ""
    error: str = ""


def is_go_project(workdir: str) -> bool:
    """Return True if go.mod exists in workdir."""
    return os.path.isfile(os.path.join(workdir, "go.mod"))


def check_go_lint(workdir: str) -> GoGuardResult:
    """Run go vet on staged Go files. Fall back to golangci-lint if available."""
    # Get staged Go files
    staged = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"],
        capture_output=True,
        text=True,
        timeout=10,
        cwd=workdir,
        env=_sanitized_env(),
    )
    go_files = [f for f in staged.stdout.strip().split("\n") if f.endswith(".go")]
    if not go_files:
        return GoGuardResult(name="go_lint", passed=True, output="No Go files staged")

    # Try golangci-lint first
    try:
        result = subprocess.run(
            ["golangci-lint", "run", "--new-from-rev=HEAD~1", *go_files],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=workdir,
            env=_sanitized_env(),
        )
        if result.returncode == 0:
            return GoGuardResult(name="go_lint", passed=True, output="golangci-lint: clean")
        # Fall through to go vet on failure
    except FileNotFoundError:
        pass

    # Fallback: go vet (per package or per file)
    try:
        result = subprocess.run(
            ["go", "vet", "./..."],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=workdir,
            env=_sanitized_env(),
        )
        output = result.stdout + result.stderr
        if len(output) > 2000:
            output = output[:2000] + "\n... [truncated]"
        if result.returncode == 0:
            return GoGuardResult(name="go_lint", passed=True, output="go vet: clean")
        return GoGuardResult(name="go_lint", passed=False, output=output)
    except Exception as e:
        return GoGuardResult(name="go_lint", passed=False, error=str(e))


def check_go_tests(workdir: str, timeout: int | str = 180) -> GoGuardResult:
    """Run go test on staged Go files.

    timeout is configurable so large Go projects (slow integration
    suites) can raise it via guards.test_timeout in .gitreins/config.yaml.
    """
    # Belt-and-braces: consumers may pass a raw string config value (e.g.
    # '300s'); subprocess.run(timeout='300s') raises TypeError instead of
    # timing out (GR-GAP-028). GuardManager already coerces at init — this
    # protects direct callers.
    timeout = _coerce_timeout(timeout, "test_timeout", 180)
    staged = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"],
        capture_output=True,
        text=True,
        timeout=10,
        cwd=workdir,
        env=_sanitized_env(),
    )
    go_files = [f for f in staged.stdout.strip().split("\n") if f.endswith(".go")]
    if not go_files:
        return GoGuardResult(name="go_tests", passed=True, output="No Go files staged")

    try:
        result = subprocess.run(
            ["go", "test", "-count=1", "-short", "./..."],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=workdir,
            env=_sanitized_env(),
        )
        output = result.stdout + result.stderr
        if len(output) > 2000:
            output = output[-2000:]
        if result.returncode == 0:
            return GoGuardResult(name="go_tests", passed=True, output=output[:500])
        return GoGuardResult(name="go_tests", passed=False, output=output)
    except subprocess.TimeoutExpired:
        return GoGuardResult(
            name="go_tests",
            passed=False,
            output=f"Tests timed out after {timeout}s (guards.test_timeout). "
            "Raise it in .gitreins/config.yaml — e.g. test_timeout: 900 for "
            "large projects with slow integration suites.",
        )
    except Exception as e:
        return GoGuardResult(name="go_tests", passed=False, error=str(e))


def check_go_build(workdir: str) -> GoGuardResult:
    """Run go build on staged Go files to catch compile errors."""
    staged = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"],
        capture_output=True,
        text=True,
        timeout=10,
        cwd=workdir,
        env=_sanitized_env(),
    )
    go_files = [f for f in staged.stdout.strip().split("\n") if f.endswith(".go")]
    if not go_files:
        return GoGuardResult(name="go_build", passed=True, output="No Go files staged")

    try:
        result = subprocess.run(
            ["go", "build", "-buildvcs=false", "./..."],
            capture_output=True,
            text=True,
            timeout=120,
            cwd=workdir,
            env=_sanitized_env(),
        )
        output = result.stdout + result.stderr
        if len(output) > 2000:
            output = output[:2000] + "\n... [truncated]"
        if result.returncode == 0:
            return GoGuardResult(name="go_build", passed=True, output="go build: ok")
        return GoGuardResult(name="go_build", passed=False, output=output)
    except Exception as e:
        return GoGuardResult(name="go_build", passed=False, error=str(e))
