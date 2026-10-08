"""DF-GITREINS-POC-42: the Go lanes grade the run's SCOPE — and a lane that
graded nothing says so.

The defect these tests red-prove: ``gitreins guard --full`` is advertised as the
whole-tree gate, but the three Go lanes (``go_build`` / ``go_lint`` /
``go_tests``) resolved their scope from ``git diff --cached``. ``--full``
populated ``changed_files`` with the whole tree yet left the lanes grading the
index, so with an empty index every lane returned early with
``passed=True, output="No Go files staged"`` — a PASS that graded nothing. A Go
tree that does not compile printed ``Tier 1 Guards: PASS`` and exited 0.

Pinned here, at the GuardManager level:

- ``grade_full_tree=True`` (what the CLI builds for ``--full``) over a scratch
  Go repo with an UNTRACKED uncompilable ``.go`` file and a clean index FAILS:
  ``go_build`` is ``passed=False`` and its output carries the real compiler
  text. This test fails against the pre-fix tree.
- The stale-index variant — the same broken file COMMITTED, index clean, same
  ``--full`` run — is not a green either.
- A Go run where no ``.go`` file was in scope reports every lane as
  ``skipped=True`` with a named reason (``"No Go files staged"`` when the index
  was the scope, ``"No Go files in scope"`` for a working-tree/whole-tree
  scope), never as a silent passing verdict, and spawns no tool.
- ``guards.allow_skips: false`` turns such a zero-work Go run into exit 2.
- Precedence is unchanged: ``--scope working-tree`` with a broken working-tree
  file still FAILS, a non-empty index of ``.go`` files still grades the staged
  set, and a CLEAN Go repo under ``--full`` PASSES with real tool output
  (``go build: ok``) rather than ``"No Go files staged"``.

Hermetic: real scratch git repos, every ``GIT_*`` variable stripped from the
children (DF-008), no network, no provider/LLM call. The ``go`` toolchain is
resolved with ``shutil.which`` and the toolchain-dependent tests are skipped
without it (POC-46: asserting on a missing binary reds CI).

DF-GITREINS-POC-45 adds section 5: the Go tests lane honours a configured
``guards.test_command`` (a shell string, the same shape the Python lane
already runs), the historical argv stays byte-for-byte when the key is
absent, and ``init`` writes the ``test_command`` key it prints as
``Test cmd:`` — without clobbering a custom command on a re-run.
"""

import os
import shutil
import subprocess
import sys

import pytest
import yaml

from engine.guard_manager import GuardManager
from engine.guards import check_go_tests
from pathlib import Path

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLI_SCRIPT = os.path.join(PROJECT_ROOT, "gitreins", "cli.py")

GO_BIN = shutil.which("go")
requires_go = pytest.mark.skipif(GO_BIN is None, reason="go toolchain not installed (POC-46)")

GO_MOD = "module example.com/poc42\n\ngo 1.21\n"
CLEAN_GO = "package quota\n\n// Clean returns a real int.\nfunc Clean() int {\n\treturn 0\n}\n"
# A SECOND distinct Go file for the staged scope (POC-45): same package, a
# different symbol, so the tree still compiles and a real `go test` PASSES —
# a configured-command failure must be attributable to the command alone.
STAGED_GO = "package quota\n\n// Extra returns a real int.\nfunc Extra() int {\n\treturn 1\n}\n"
# The compiler error is the evidence the lane really ran: an untyped string
# constant returned as an int.
BROKEN_GO = (
    'package quota\n\n// Broken does not compile.\nfunc Broken() int {\n\treturn "not an int"\n}\n'
)
BROKEN_MARKER = "not an int"

LANE_NAMES = ("go_build", "go_lint", "go_tests")


# ── Helpers ───────────────────────────────────────────────────────


def _git_env() -> dict:
    """Environment without leaked GIT_* vars (DF-008).

    These tests git-init scratch repos; an inherited GIT_INDEX_FILE/GIT_DIR
    from a pre-commit hook would point them at the outer repo instead.
    """
    return {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}


def _cli_env() -> dict:
    """Environment for a CLI subprocess: no GIT_* leak, no live-LLM opt-in.

    A leaked ``GITREINS_LLM_*`` variable would make a child attempt a real
    tier-2 evaluation — the tests must never touch a provider.
    """
    env = {k: v for k, v in _git_env().items() if not k.startswith("GITREINS_")}
    env["PYTHONPATH"] = PROJECT_ROOT + (":" + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    return env


def _git(workdir: str, *args: str) -> None:
    subprocess.run(["git", *args], cwd=workdir, capture_output=True, check=True, env=_git_env())


def _write(workdir: str, relpath: str, content: str) -> None:
    path = os.path.join(workdir, relpath)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as handle:
        handle.write(content)


def _scratch_repo(tmp_path: Path, files: dict[str, str], name: str = "scratch") -> str:
    """A real git repo with *files* committed and a clean index afterwards."""
    workdir = tmp_path / name
    workdir.mkdir()
    for relpath, content in files.items():
        _write(str(workdir), relpath, content)
    _git(str(workdir), "init", "-q")
    _git(str(workdir), "config", "user.email", "poc42@example.invalid")
    _git(str(workdir), "config", "user.name", "POC-42")
    _git(str(workdir), "add", "-A")
    _git(str(workdir), "commit", "-qm", "init")
    return str(workdir)


def _go_config(**overrides: object) -> dict:
    """Guards config for a Go repo: only the Go lanes are in play.

    The Python lanes are switched off so ``run_all``'s verdict and its
    degradation marker come from the Go lanes alone; ``guards.go`` is spelled
    out so the Go lanes cannot be dropped by a later default change.
    """
    guards: dict = {
        "secrets": False,
        "lint": False,
        "tests": False,
        "go": {"build": True, "lint": True, "tests": True},
    }
    guards.update(overrides)
    return {"guards": guards}


def _manager(workdir: str, **overrides: object) -> GuardManager:
    return GuardManager(workdir, config=_go_config(**overrides))


def _manager_full_tree(workdir: str, **overrides: object) -> GuardManager:
    """The CLI's ``--full``: grade_full_tree is keyword-only by contract."""
    return GuardManager(workdir, config=_go_config(**overrides), grade_full_tree=True)


def _lane(result: object, name: str) -> object:
    lanes = [r for r in result.results if r.name == name]
    assert len(lanes) == 1, (
        f"expected exactly one {name} lane, got {[r.name for r in result.results]}"
    )
    return lanes[0]


def _run_cli(*args: object, cwd: str = None) -> object:
    env = _cli_env()
    cmd = [sys.executable, CLI_SCRIPT] + list(args)
    return subprocess.run(cmd, capture_output=True, text=True, timeout=180, cwd=cwd, env=env)


# ── 1. --full grades the whole tree, not the index ────────────────


class TestFullTreeGoScope:
    @requires_go
    def test_untracked_uncompilable_go_file_fails_the_build_lane(self, tmp_path: Path) -> None:
        """RED-PROOF: on the pre-fix tree this lane returned passed=True with
        output "No Go files staged" (the index was empty) — the whole-tree flag
        graded nothing."""
        workdir = _scratch_repo(tmp_path, {"go.mod": GO_MOD, "internal/quota/clean.go": CLEAN_GO})
        _write(workdir, "internal/quota/broken.go", BROKEN_GO)

        gm = _manager_full_tree(workdir)

        assert gm.changed_files == [], "precondition: the index is clean"

        result = gm._check_go_build()

        assert result.passed is False, f"whole-tree --full must not pass: {result.output!r}"
        assert result.skipped is False
        assert BROKEN_MARKER in result.output, result.output

    @requires_go
    def test_untracked_uncompilable_go_file_fails_the_whole_run(self, tmp_path: Path) -> None:
        """The run's verdict, not just the lane's: not passed, and the Go lanes
        ran instead of reporting an empty scope."""
        workdir = _scratch_repo(tmp_path, {"go.mod": GO_MOD, "internal/quota/clean.go": CLEAN_GO})
        _write(workdir, "internal/quota/broken.go", BROKEN_GO)

        result = _manager_full_tree(workdir).run_all()

        assert result.passed is False
        build = _lane(result, "go_build")
        assert build.passed is False
        assert BROKEN_MARKER in build.output, build.output
        assert "No Go files" not in build.output

    @requires_go
    def test_committed_broken_go_file_with_clean_index_is_not_a_green(self, tmp_path: Path) -> None:
        """The stale-index false green: the broken file is COMMITTED (index
        clean), the tree still does not compile, so --full must not pass."""
        workdir = _scratch_repo(tmp_path, {"go.mod": GO_MOD, "broken.go": BROKEN_GO})

        gm = _manager_full_tree(workdir)
        assert gm.changed_files == []

        result = gm.run_all()

        assert result.passed is False
        assert BROKEN_MARKER in _lane(result, "go_build").output

    @requires_go
    def test_clean_go_tree_under_full_passes_with_real_tool_output(self, tmp_path: Path) -> None:
        """A clean Go repo under --full is a REAL pass: the tool ran (go build:
        ok), no lane is a skip, and the run is not degraded."""
        workdir = _scratch_repo(tmp_path, {"go.mod": GO_MOD, "internal/quota/clean.go": CLEAN_GO})

        result = _manager_full_tree(workdir).run_all()

        assert result.passed is True
        build = _lane(result, "go_build")
        assert build.passed is True
        assert build.skipped is False
        assert "go build: ok" in build.output
        assert "No Go files" not in build.output
        assert result.skipped_steps == []
        assert result.degraded is False

    def test_tree_go_files_lists_tracked_and_untracked_go_only(self, tmp_path: Path) -> None:
        """The Go twin of _tree_python_files: tracked + untracked-but-not-ignored,
        .go only, deduped, repo-relative."""
        from engine.guard_manager import _tree_go_files

        workdir = _scratch_repo(tmp_path, {"go.mod": GO_MOD, "tracked.go": CLEAN_GO})
        _write(workdir, "cmd/untracked.go", CLEAN_GO)
        _write(workdir, "notes.md", "not Go\n")

        files = _tree_go_files(workdir)

        assert set(files) == {"tracked.go", os.path.join("cmd", "untracked.go")}
        assert len(files) == len(set(files)), "the listing must be deduped"
        assert all(f.endswith(".go") for f in files)


# ── 2. Precedence: working-tree scope, staged index, clean repo ───


class TestScopePrecedence:
    @requires_go
    def test_working_tree_scope_still_fails_on_a_broken_working_tree_file(
        self, tmp_path: Path
    ) -> None:
        """Control (unchanged behaviour): --scope working-tree grades what is on
        disk, so an uncompilable untracked .go file FAILS the run."""
        workdir = _scratch_repo(tmp_path, {"go.mod": GO_MOD, "internal/quota/clean.go": CLEAN_GO})
        _write(workdir, "internal/quota/broken.go", BROKEN_GO)

        gm = GuardManager(workdir, config=_go_config(), scope="working-tree")
        result = gm.run_all()

        assert result.passed is False
        build = _lane(result, "go_build")
        assert build.passed is False
        assert BROKEN_MARKER in build.output

    @requires_go
    def test_non_empty_index_of_go_files_keeps_the_staged_scope(self, tmp_path: Path) -> None:
        """A staged .go file still decides the scope (that is what the
        pre-commit hook grades): the lanes keep their own index discovery and
        run the tool."""
        workdir = _scratch_repo(tmp_path, {"go.mod": GO_MOD, "internal/quota/clean.go": CLEAN_GO})
        _write(workdir, "staged.go", CLEAN_GO)
        _git(workdir, "add", "staged.go")

        gm = _manager_full_tree(workdir)

        assert gm._go_scope_files_or_none() is None
        build = gm._check_go_build()
        assert build.passed is True
        assert build.skipped is False
        assert "go build: ok" in build.output

    def test_no_go_files_staged_keeps_the_staged_wording(self, tmp_path: Path) -> None:
        """A bare `gitreins guard` (no --full) on a Go repo with only non-Go
        files staged skips with the historical wording — and, with the skip
        signal in place, that is now a DEGRADED run rather than a silent green."""
        workdir = _scratch_repo(tmp_path, {"go.mod": GO_MOD, "README.md": "no Go here\n"})
        _write(workdir, "CHANGELOG.md", "notes\n")
        _git(workdir, "add", "CHANGELOG.md")

        gm = _manager(workdir)
        result = gm.run_all()

        assert result.passed is True, "a skip is not a failure"
        for name in LANE_NAMES:
            lane = _lane(result, name)
            assert lane.skipped is True
            assert lane.skip_reason == "No Go files staged"
            assert "ok" not in lane.output
        assert result.degraded is True
        assert result.skip_summary == (
            "go_build=No Go files staged, go_lint=No Go files staged, go_tests=No Go files staged"
        )


# ── 3. A lane that graded nothing is an honest SKIP ───────────────


class TestNoWorkLaneIsASkip:
    @requires_go
    def test_no_go_file_in_the_whole_tree_reports_a_named_skip(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """--full over a Go repo whose tree holds no .go file: every lane is a
        SKIP with the whole-tree reason, no tool is spawned, and the run is
        degraded (never a silent PASS)."""
        workdir = _scratch_repo(tmp_path, {"go.mod": GO_MOD, "README.md": "no Go here\n"})

        def _boom(*args: object, **kwargs: object) -> None:
            raise AssertionError(f"a tool was spawned for a scope that graded nothing: {args}")

        monkeypatch.setattr("engine.guards.command_hygiene.run_bounded", _boom)

        result = _manager_full_tree(workdir).run_all()

        for name in LANE_NAMES:
            lane = _lane(result, name)
            assert lane.passed is True, "the toolchain is not at fault"
            assert lane.skipped is True
            assert lane.skip_reason == "No Go files in scope"
            assert "ok" not in lane.output
            assert "skipped" in result.summary
            assert f"~ {name} — skipped (No Go files in scope)" in result.summary
        assert result.degraded is True
        assert result.skip_summary == (
            "go_build=No Go files in scope, go_lint=No Go files in scope, "
            "go_tests=No Go files in scope"
        )

    def test_clean_tree_without_full_still_skips_the_index(self, tmp_path: Path) -> None:
        """No --full, clean index: the scope really is the index, and the reason
        says so (the hook path keeps its wording). No toolchain needed — the
        lanes return before spawning anything."""
        workdir = _scratch_repo(tmp_path, {"go.mod": GO_MOD, "internal/quota/clean.go": CLEAN_GO})

        result = _manager(workdir).run_all()

        for name in LANE_NAMES:
            lane = _lane(result, name)
            assert lane.skipped is True
            assert lane.skip_reason == "No Go files staged"
        assert result.degraded is True


# ── 4. Exit-code policy on the zero-work Go run ───────────────────


class TestDegradedGoRunExitCode:
    def _repo_with_no_go_file(self, tmp_path: Path, name: str, allow_skips: bool) -> str:
        """A Go repo whose tree holds no .go file, with an explicit allow_skips."""
        workdir = _scratch_repo(
            tmp_path, {"go.mod": GO_MOD, "README.md": "no Go here\n"}, name=name
        )
        _write(
            workdir,
            os.path.join(".gitreins", "config.yaml"),
            "guards:\n"
            "  secrets: false\n"
            "  lint: false\n"
            "  tests: false\n"
            f"  allow_skips: {str(allow_skips).lower()}\n",
        )
        return workdir

    def test_allow_skips_false_makes_a_zero_work_go_run_exit_2(self, tmp_path: Path) -> None:
        workdir = self._repo_with_no_go_file(tmp_path, "strict", allow_skips=False)

        result = _run_cli("guard", "--full", cwd=workdir)

        assert result.returncode == 2, f"stdout={result.stdout} stderr={result.stderr}"
        assert "DEGRADED PASS" in result.stdout
        assert "go_build=No Go files in scope" in result.stdout
        assert "Tier 1 Guards: PASS" not in result.stdout
        assert "guards.allow_skips" in result.stderr

    def test_allow_skips_true_accepts_the_zero_work_go_run(self, tmp_path: Path) -> None:
        workdir = self._repo_with_no_go_file(tmp_path, "lenient", allow_skips=True)

        result = _run_cli("guard", "--full", cwd=workdir)

        assert result.returncode == 0, f"stdout={result.stdout} stderr={result.stderr}"
        assert "~ go_build — skipped (No Go files in scope)" in result.stdout
        assert "Tier 1 Guards: PASS" not in result.stdout

    @requires_go
    def test_full_run_on_a_broken_go_tree_exits_1_with_the_compiler_text(
        self, tmp_path: Path
    ) -> None:
        """The CLI-level acceptance criterion: --full over an untracked
        uncompilable file fails with exit 1 and the real compiler error."""
        workdir = _scratch_repo(
            tmp_path, {"go.mod": GO_MOD, "internal/quota/clean.go": CLEAN_GO}, name="broken"
        )
        # The CLI refuses to run without an initialized config (its own guard).
        _write(workdir, os.path.join(".gitreins", "config.yaml"), "guards:\n  secrets: false\n")
        _write(workdir, "internal/quota/broken.go", BROKEN_GO)

        result = _run_cli("guard", "--full", cwd=workdir)

        assert result.returncode == 1, f"stdout={result.stdout} stderr={result.stderr}"
        assert "Tier 1 Guards: FAIL" in result.stdout
        assert BROKEN_MARKER in result.stdout, result.stdout


# ── 5. DF-GITREINS-POC-45: guards.test_command drives the Go tests lane ──


# A configured command that cannot be confused with `go test`: it prints its
# own marker to stderr (run_bounded merges stderr into stdout) and exits 7.
# A lane that ignores the config runs real `go test` — which PASSES this repo
# — so "the lane failed with the marker in its output" is RED-proof on its own.
CONFIGURED_GO_TEST_CMD = "sh -c 'echo CONFIGURED-COMMAND-RAN >&2; exit 7'"
CONFIGURED_MARKER = "CONFIGURED-COMMAND-RAN"

HISTORICAL_GO_TEST_ARGV = ["go", "test", "-count=1", "-short", "./..."]
# The shim IS `go` (argv[0] never reaches "$@"), so the recorded expectation
# drops it.
SHIM_RECORDED_ARGV = HISTORICAL_GO_TEST_ARGV[1:]


def _go_argv_shim(tmp_path: Path, name: str) -> tuple[str, str]:
    """A fake ``go`` earlier on PATH that records the argv it was handed.

    The lane's argv decision (historical list vs shell string) is observed at
    the exact boundary where ``go`` would run. Returns ``(bin_dir, argv_log)``.
    """
    bin_dir = str(tmp_path / name)
    os.makedirs(bin_dir, exist_ok=True)
    argv_log = os.path.join(bin_dir, "go-argv.txt")
    shim = os.path.join(bin_dir, "go")
    with open(shim, "w") as handle:
        handle.write('#!/bin/sh\nprintf "%s\\n" "$@" > ' + repr(argv_log) + "\nexit 0\n")
    os.chmod(shim, 0o755)
    return bin_dir, argv_log


def _read_config(workdir: str) -> dict:
    with open(os.path.join(workdir, ".gitreins", "config.yaml")) as handle:
        return yaml.safe_load(handle)


class TestConfiguredGoTestCommand:
    @requires_go
    def test_configured_test_command_is_run_by_the_go_tests_lane(self, tmp_path: Path) -> None:
        """RED-PROOF: pre-fix the lane hard-coded the go test argv, ignored the
        key, and PASSED this repo — the configured command never executed, so
        neither its marker nor its exit 7 could appear."""
        workdir = _scratch_repo(tmp_path, {"go.mod": GO_MOD, "main.go": CLEAN_GO})
        _write(
            workdir,
            os.path.join(".gitreins", "config.yaml"),
            "guards:\n"
            "  secrets: false\n"
            "  lint: false\n"
            "  tests: false\n"
            "  go: {build: false, lint: false, tests: true}\n"
            f"  test_command: {CONFIGURED_GO_TEST_CMD}\n",
        )
        # A non-empty staged .go file keeps the lane off the skip path.
        _write(workdir, "staged.go", STAGED_GO)
        _git(workdir, "add", "staged.go")

        gm = GuardManager(workdir)  # loads .gitreins/config.yaml from disk
        result = gm._check_go_tests()

        assert result.passed is False, (
            f"a failing configured command must fail the lane: {result.output!r}"
        )
        assert result.skipped is False
        assert CONFIGURED_MARKER in result.output, result.output

    @requires_go
    def test_run_all_carries_the_configured_command_failure(self, tmp_path: Path) -> None:
        """The full-run verdict, not just the lane: a configured command's
        failure fails the run, with the command's own text as the evidence."""
        workdir = _scratch_repo(tmp_path, {"go.mod": GO_MOD, "main.go": CLEAN_GO})
        _write(
            workdir,
            os.path.join(".gitreins", "config.yaml"),
            "guards:\n"
            "  secrets: false\n"
            "  lint: false\n"
            "  tests: false\n"
            "  go: {build: true, lint: true, tests: true}\n"
            f"  test_command: {CONFIGURED_GO_TEST_CMD}\n",
        )
        _write(workdir, "staged.go", STAGED_GO)
        _git(workdir, "add", "staged.go")

        result = GuardManager(workdir).run_all()

        assert result.passed is False
        tests = _lane(result, "go_tests")
        assert tests.passed is False
        assert CONFIGURED_MARKER in tests.output, tests.output

    @requires_go
    def test_absent_key_keeps_the_historical_argv(self, tmp_path: Path) -> None:
        """A Go repo without test_command still gets the pinned argv list —
        shell-quoted when the key is absent is WRONG (it would break every
        existing config's semantics), so the exact list is asserted here."""
        workdir = _scratch_repo(tmp_path, {"go.mod": GO_MOD, "main.go": CLEAN_GO})
        _write(
            workdir,
            os.path.join(".gitreins", "config.yaml"),
            "guards:\n"
            "  secrets: false\n"
            "  lint: false\n"
            "  tests: false\n"
            "  go: {build: false, lint: false, tests: true}\n",
        )
        _write(workdir, "staged.go", STAGED_GO)
        _git(workdir, "add", "staged.go")

        # A fake `go` shim that records its argv: the lane's argv decision is
        # observed at the exact boundary where `go` would run.
        bin_dir, argv_log = _go_argv_shim(tmp_path, "fakebin")
        env = {k: v for k, v in _git_env().items() if not k.startswith("GITREINS_")}
        env["PATH"] = bin_dir + os.pathsep + env["PATH"]
        saved_env = os.environ.copy()
        os.environ.clear()
        os.environ.update(env)
        try:
            # Direct unit call: the lane consults the (absent) config key.
            r = check_go_tests(workdir)
        finally:
            os.environ.clear()
            os.environ.update(saved_env)

        assert r.passed is True, r.output
        with open(argv_log) as f:
            recorded = [line for line in f.read().splitlines() if line]
        assert recorded == SHIM_RECORDED_ARGV, recorded

    @requires_go
    def test_explicit_empty_test_command_falls_back_to_the_default(self, tmp_path: Path) -> None:
        """An explicit empty value is "not configured", not "run nothing".

        Discriminating arm: an implementation that treats a PRESENT key as
        configured even when it is empty would shell out to ``sh -c ""`` —
        which exits 0 with no output and never invokes ``go`` at all. The shim
        records whether the historical argv really ran.
        """
        workdir = _scratch_repo(tmp_path, {"go.mod": GO_MOD, "main.go": CLEAN_GO}, name="empty-key")
        _write(
            workdir,
            os.path.join(".gitreins", "config.yaml"),
            "guards:\n"
            "  secrets: false\n"
            "  lint: false\n"
            "  tests: false\n"
            "  go: {build: false, lint: false, tests: true}\n"
            '  test_command: ""\n',
        )
        _write(workdir, "staged.go", STAGED_GO)
        _git(workdir, "add", "staged.go")

        bin_dir, argv_log = _go_argv_shim(tmp_path, "empty-key-shim")
        env = {k: v for k, v in _git_env().items() if not k.startswith("GITREINS_")}
        env["PATH"] = bin_dir + os.pathsep + env["PATH"]
        saved_env = os.environ.copy()
        os.environ.clear()
        os.environ.update(env)
        try:
            gm = GuardManager(workdir)
            result = gm._check_go_tests()
        finally:
            os.environ.clear()
            os.environ.update(saved_env)

        assert result.skipped is False
        assert result.passed is True, result.output
        # The historical argv ran — the empty string was NOT executed as a
        # shell command (that path leaves no argv log at all).
        with open(argv_log) as handle:
            recorded = [line for line in handle.read().splitlines() if line]
        assert recorded == SHIM_RECORDED_ARGV, recorded


class TestInitWritesGoTestCommand:
    """init must write the Go `test_command` it prints (POC-45): the printed
    `Test cmd:` line and the generated config must agree."""

    def _scratch_go_repo_for_init(self, tmp_path: Path, name: str) -> str:
        workdir = tmp_path / name
        workdir.mkdir()
        _write(str(workdir), "go.mod", GO_MOD)
        _write(str(workdir), "main.go", CLEAN_GO)
        _git(str(workdir), "init", "-q")
        _git(str(workdir), "config", "user.email", "poc45@example.invalid")
        _git(str(workdir), "config", "user.name", "POC-45")
        _git(str(workdir), "add", "-A")
        _git(str(workdir), "commit", "-qm", "init")
        return str(workdir)

    @requires_go
    def test_init_writes_the_test_command_it_prints(self, tmp_path: Path) -> None:
        """RED-PROOF: pre-fix the Go branch of _build_guards_section carried no
        test_command key at all — the config parsed WITHOUT 'test_command'
        under guards while the printed line claimed one."""
        workdir = self._scratch_go_repo_for_init(tmp_path, "fresh-go")

        result = _run_cli("init", cwd=workdir)

        assert result.returncode == 0, f"stdout={result.stdout} stderr={result.stderr}"
        printed = next(
            line.split("Test cmd:", 1)[1].strip()
            for line in result.stdout.splitlines()
            if line.lstrip().startswith("Test cmd:")
        )
        config = _read_config(workdir)
        assert "test_command" in config["guards"], (
            f"init printed {printed!r} but wrote no test_command: {config['guards']!r}"
        )
        assert config["guards"]["test_command"] == printed
        assert config["guards"]["test_command"] == "go test -short -count=1 ./..."

    @requires_go
    def test_init_rerun_preserves_a_custom_go_test_command(self, tmp_path: Path) -> None:
        """A user-authored command survives a re-run: only the untouched
        install baseline may be upgraded, an explicit setting is preserved."""
        workdir = self._scratch_go_repo_for_init(tmp_path, "custom-go")
        custom = "go test -race ./..."
        # First run creates the baseline config, second run must preserve.
        assert _run_cli("init", cwd=workdir).returncode == 0
        config = _read_config(workdir)
        config["guards"]["test_command"] = custom
        with open(os.path.join(workdir, ".gitreins", "config.yaml"), "w") as f:
            yaml.dump(config, f)

        rerun = _run_cli("init", cwd=workdir)

        assert rerun.returncode == 0, f"stdout={rerun.stdout} stderr={rerun.stderr}"
        assert _read_config(workdir)["guards"]["test_command"] == custom
        assert custom in rerun.stdout, rerun.stdout
