"""Go-specific guard checks for GitReins."""

import logging
import os
import re
import subprocess
from dataclasses import dataclass

from engine import command_hygiene

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
            f"guards.{name} must be a positive number of seconds (e.g. {name}: 300), got {value!r}"
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
            f"guards.{name} must be a positive number of seconds (e.g. {name}: 300), got {value!r}"
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


def _changed_go_files(workdir: str, changed_files: list[str] | None) -> list[str]:
    """Go files to grade under the caller's change scope.

    ``changed_files`` is the caller's collected scope (the guard's
    ``--scope working-tree`` set) — paths that no longer exist are dropped,
    since nothing can compile or lint them. ``None`` means "no scope was
    handed in": the guards then run their own historical staged discovery,
    unchanged, wording included.
    """
    if changed_files is None:
        staged = subprocess.run(
            ["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=workdir,
            env=_sanitized_env(),
        )
        return [f for f in staged.stdout.strip().split("\n") if f.endswith(".go")]
    return [
        f for f in changed_files if f.endswith(".go") and os.path.isfile(os.path.join(workdir, f))
    ]


def _no_go_files(changed_files: list[str] | None) -> str:
    """The skip message for the scope that produced no Go file."""
    return "No Go files staged" if changed_files is None else "No Go files in scope"


def check_go_lint(workdir: str, changed_files: list[str] | None = None) -> GoGuardResult:
    """Run go vet for the selected change scope. Fall back to golangci-lint if available."""
    go_files = _changed_go_files(workdir, changed_files)
    if not go_files:
        return GoGuardResult(name="go_lint", passed=True, output=_no_go_files(changed_files))

    # Try golangci-lint first. run_bounded never raises for a missing
    # binary — it returns {"error": ...} without an exit_code — so any
    # non-zero/absent outcome falls through to go vet (DF-CRIER-258:
    # DF-008's kill-group discipline now covers this spawn too).
    result = command_hygiene.run_bounded(
        ["golangci-lint", "run", "--new-from-rev=HEAD~1", *go_files],
        cwd=workdir,
        timeout=60,
        env=_sanitized_env(),
    )
    if result.get("exit_code") == 0:
        return GoGuardResult(name="go_lint", passed=True, output="golangci-lint: clean")
    # Fall through to go vet on failure

    # Fallback: go vet (per package or per file)
    result = command_hygiene.run_bounded(
        ["go", "vet", "./..."],
        cwd=workdir,
        timeout=60,
        env=_sanitized_env(),
    )
    if "error" in result and "exit_code" not in result:
        # Spawn failure (e.g. go itself missing) — surfaced in error,
        # matching the old except-Exception contract.
        return GoGuardResult(name="go_lint", passed=False, error=result["error"])
    output = result.get("output") or ""
    if len(output) > 2000:
        output = output[:2000] + "\n... [truncated]"
    if result.get("exit_code") == 0:
        return GoGuardResult(name="go_lint", passed=True, output="go vet: clean")
    return GoGuardResult(name="go_lint", passed=False, output=output)


def check_go_tests(
    workdir: str, timeout: int | str = 180, changed_files: list[str] | None = None
) -> GoGuardResult:
    """Run go test for the selected change scope.

    timeout is configurable so large Go projects (slow integration
    suites) can raise it via guards.test_timeout in .gitreins/config.yaml.
    ``changed_files`` carries the caller's scope (see :func:`_changed_go_files`).
    """
    # Belt-and-braces: consumers may pass a raw string config value (e.g.
    # '300s'); subprocess.run(timeout='300s') raises TypeError instead of
    # timing out (GR-GAP-028). GuardManager already coerces at init — this
    # protects direct callers.
    timeout = _coerce_timeout(timeout, "test_timeout", 180)
    go_files = _changed_go_files(workdir, changed_files)
    if not go_files:
        return GoGuardResult(name="go_tests", passed=True, output=_no_go_files(changed_files))

    result = command_hygiene.run_bounded(
        ["go", "test", "-count=1", "-short", "./..."],
        cwd=workdir,
        timeout=timeout,
        env=_sanitized_env(),
    )
    if result.get("timed_out"):
        return GoGuardResult(
            name="go_tests",
            passed=False,
            output=f"Tests timed out after {timeout}s (guards.test_timeout). "
            "Raise it in .gitreins/config.yaml — e.g. test_timeout: 900 for "
            "large projects with slow integration suites.",
        )
    if "error" in result and "exit_code" not in result:
        return GoGuardResult(name="go_tests", passed=False, error=result["error"])
    output = result.get("output") or ""
    if len(output) > 2000:
        output = output[-2000:]
    if result.get("exit_code") == 0:
        return GoGuardResult(name="go_tests", passed=True, output=output[:500])
    return GoGuardResult(name="go_tests", passed=False, output=output)


def check_go_build(workdir: str, changed_files: list[str] | None = None) -> GoGuardResult:
    """Run go build for the selected change scope to catch compile errors."""
    go_files = _changed_go_files(workdir, changed_files)
    if not go_files:
        return GoGuardResult(name="go_build", passed=True, output=_no_go_files(changed_files))

    result = command_hygiene.run_bounded(
        ["go", "build", "-buildvcs=false", "./..."],
        cwd=workdir,
        timeout=120,
        env=_sanitized_env(),
    )
    if "error" in result and "exit_code" not in result:
        # Spawn failure (e.g. go missing) — old except-Exception contract.
        return GoGuardResult(name="go_build", passed=False, error=result["error"])
    output = result.get("output") or ""
    if len(output) > 2000:
        output = output[:2000] + "\n... [truncated]"
    if result.get("exit_code") == 0:
        return GoGuardResult(name="go_build", passed=True, output="go build: ok")
    return GoGuardResult(name="go_build", passed=False, output=output)
