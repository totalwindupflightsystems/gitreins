import os

import pytest

from engine.guards import check_go_lint

from tests.test_guard_go_lanes import (
    GO_BIN,
    GO_MOD,
    _git,
    _scratch_repo,
    _write,
)

requires_go = pytest.mark.skipif(GO_BIN is None, reason="go toolchain not installed (POC-46)")

LINT_SHIM = """#!/bin/sh
printf '%s\t' "$@" >> "$(dirname "$0")/golangci-argv.log"
echo >> "$(dirname "$0")/golangci-argv.log"
exit 0
"""


def _lint_argv_shim(tmp_path):
    """A fake golangci-lint earlier on PATH that records every invocation."""
    bin_dir = str(tmp_path / "lint-shim")
    os.makedirs(bin_dir, exist_ok=True)
    argv_log = os.path.join(bin_dir, "golangci-argv.log")
    if os.path.exists(argv_log):
        os.remove(argv_log)
    shim = os.path.join(bin_dir, "golangci-lint")
    with open(shim, "w") as handle:
        handle.write(LINT_SHIM)
    os.chmod(shim, 0o755)
    return bin_dir, argv_log


def _read_argv(path):
    with open(path) as handle:
        return [line.rstrip("\n").rstrip("\t").split("\t") for line in handle if line.strip("\t")]


class TestGoLintPackageScope:
    """GR-LINT-001: an explicit per-file golangci-lint invocation breaks
    cross-file typecheck, so every reference to a package-level symbol in a
    sibling file reads ``undefined`` — false findings that blocked real
    single-file Go commits three times on 2026-09-24."""

    def test_single_file_scope_has_no_path_args(self, tmp_path, monkeypatch):
        """RED-PROOF: pre-fix the argv ended with the changed FILE name; the
        shim log must instead show an invocation with no path after the rev
        flag (package-dir scoping)."""
        workdir = _scratch_repo(
            tmp_path,
            {
                "go.mod": GO_MOD,
                "quota.go": "package quota\n\nfunc Existing() int { return 7 }\n",
                "staged.go": "package quota\n\nfunc Extra() int { return Existing() }\n",
            },
        )
        _git(workdir, "add", "staged.go")
        bin_dir, argv_log = _lint_argv_shim(tmp_path)
        monkeypatch.setenv("PATH", bin_dir + os.pathsep + os.environ["PATH"])

        result = check_go_lint(workdir, changed_files=["staged.go"])

        assert result.passed, result.output
        invocations = _read_argv(argv_log)
        assert invocations, "golangci-lint was never invoked"
        argv = invocations[0]
        assert argv.index("--new-from-rev=HEAD~1") == len(argv) - 1, argv
        assert "staged.go" not in argv

    def test_multi_package_scope_yields_one_invocation_per_package(self, tmp_path, monkeypatch):
        workdir = _scratch_repo(
            tmp_path,
            {
                "go.mod": GO_MOD,
                "alpha/a.go": "package alpha\n",
                "beta/b.go": "package beta\n",
            },
        )
        _git(workdir, "add", "alpha/a.go", "beta/b.go")
        bin_dir, argv_log = _lint_argv_shim(tmp_path)
        monkeypatch.setenv("PATH", bin_dir + os.pathsep + os.environ["PATH"])

        result = check_go_lint(workdir, changed_files=["alpha/a.go", "beta/b.go"])

        assert result.passed, result.output
        invocations = _read_argv(argv_log)
        assert len(invocations) == 2, invocations
        pkg_args = [argv[-1] for argv in invocations]
        assert sorted(pkg_args) == ["alpha", "beta"], invocations

    @requires_go
    def test_real_toolchain_single_file_commit_passes(self, tmp_path):
        """The live defect: a real single-file commit in a populated package
        must grade clean with the REAL golangci-lint (no undefined-symbol
        false findings)."""
        workdir = _scratch_repo(
            tmp_path,
            {
                "go.mod": GO_MOD,
                "quota.go": "package quota\n\n// Existing returns a real int.\nfunc Existing() int { return 7 }\n",
            },
        )
        # staged.go references Existing() from its sibling file: the exact
        # shape the file-scoped invocation reported as 17 undefined findings.
        _write(
            workdir,
            "staged.go",
            "package quota\n\n// Extra calls the sibling.\nfunc Extra() int { return Existing() + 1 }\n",
        )
        _git(workdir, "add", "staged.go")

        result = check_go_lint(workdir, changed_files=["staged.go"])

        assert result.passed, result.output
