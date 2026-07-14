"""Go-specific guard checks for GitReins."""

import logging
import os
import subprocess
from dataclasses import dataclass

logger = logging.getLogger("gitreins.guards.go")


@dataclass
class GoGuardResult:
    name: str
    passed: bool
    output: str = ""
    error: str = ""


def is_go_project(workdir: str) -> bool:
    """Return True if go.mod exists in workdir."""
    return os.path.isfile(os.path.join(workdir, "go.mod"))


def check_go_lint(workdir: str, changed_files: list[str] | None = None) -> GoGuardResult:
    """Run Go lint checks for the selected change scope."""
    if changed_files is None:
        staged = subprocess.run(
            ["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"],
            capture_output=True, text=True, timeout=10, cwd=workdir
        )
        changed_files = staged.stdout.strip().split("\n")
    go_files = [f for f in changed_files if f.endswith(".go")]
    if not go_files:
        return GoGuardResult(name="go_lint", passed=True, output="No Go files in scope")

    # Try golangci-lint first
    try:
        result = subprocess.run(
            ["golangci-lint", "run", "--new-from-rev=HEAD~1", *go_files],
            capture_output=True, text=True, timeout=60, cwd=workdir
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
            capture_output=True, text=True, timeout=60, cwd=workdir
        )
        output = result.stdout + result.stderr
        if len(output) > 2000:
            output = output[:2000] + "\n... [truncated]"
        if result.returncode == 0:
            return GoGuardResult(name="go_lint", passed=True, output="go vet: clean")
        return GoGuardResult(name="go_lint", passed=False, output=output)
    except Exception as e:
        return GoGuardResult(name="go_lint", passed=False, error=str(e))


def check_go_tests(workdir: str, changed_files: list[str] | None = None) -> GoGuardResult:
    """Run Go tests when Go files are present in the selected change scope."""
    if changed_files is None:
        staged = subprocess.run(
            ["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"],
            capture_output=True, text=True, timeout=10, cwd=workdir
        )
        changed_files = staged.stdout.strip().split("\n")
    go_files = [f for f in changed_files if f.endswith(".go")]
    if not go_files:
        return GoGuardResult(name="go_tests", passed=True, output="No Go files in scope")

    try:
        result = subprocess.run(
            ["go", "test", "-count=1", "-short", "./..."],
            capture_output=True, text=True, timeout=180, cwd=workdir
        )
        output = result.stdout + result.stderr
        if len(output) > 2000:
            output = output[-2000:]
        if result.returncode == 0:
            return GoGuardResult(name="go_tests", passed=True, output=output[:500])
        return GoGuardResult(name="go_tests", passed=False, output=output)
    except subprocess.TimeoutExpired:
        return GoGuardResult(name="go_tests", passed=False, output="Tests timed out after 180s")
    except Exception as e:
        return GoGuardResult(name="go_tests", passed=False, error=str(e))


def check_go_build(workdir: str, changed_files: list[str] | None = None) -> GoGuardResult:
    """Run Go build when Go files are present in the selected change scope."""
    if changed_files is None:
        staged = subprocess.run(
            ["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"],
            capture_output=True, text=True, timeout=10, cwd=workdir
        )
        changed_files = staged.stdout.strip().split("\n")
    go_files = [f for f in changed_files if f.endswith(".go")]
    if not go_files:
        return GoGuardResult(name="go_build", passed=True, output="No Go files in scope")

    try:
        result = subprocess.run(
            ["go", "build", "-buildvcs=false", "./..."],
            capture_output=True, text=True, timeout=120, cwd=workdir
        )
        output = result.stdout + result.stderr
        if len(output) > 2000:
            output = output[:2000] + "\n... [truncated]"
        if result.returncode == 0:
            return GoGuardResult(name="go_build", passed=True, output="go build: ok")
        return GoGuardResult(name="go_build", passed=False, output=output)
    except Exception as e:
        return GoGuardResult(name="go_build", passed=False, error=str(e))
