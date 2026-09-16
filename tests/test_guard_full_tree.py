"""
DF-GITREINS-POC-11: `gitreins guard --full` grades the whole tree.

Before this change, an explicit `--full` run on a clean tree still returned
the TRUST-001 skips (``no staged files``) from the tests and lint lanes —
the flag that is supposed to gate everything ran zero tests and zero lint.

These tests pin the whole-tree contract at the GuardManager level:

- ``grade_full_tree=True`` (what the CLI builds for ``--full``): a clean tree
  runs the configured test_command (proven by a marker file the command
  writes) and lints the whole tree — tracked AND untracked-but-not-ignored
  Python files — naming the graded file count in the output.
- The forced lint lane is a real gate: a genuine ruff finding (F401, unused
  import) in a tracked file FAILS the lane (``passed is False``), and an
  untracked-but-not-ignored file with a finding is graded too.
- Default construction behaves exactly as before (AC 4): both lanes return
  ``skipped=True, skip_reason="no staged files"`` on the same clean tree.
- Staged files still take precedence when the index is non-empty, and a
  tree with no Python files at all keeps the honest skip (the linter is
  never invoked with an empty file list).

No network, no LSP, no dependence on the real repo's git state. The ruff
binary is pinned by PATH (repo venv first) so the lint tests fail loudly —
never silently pass — when ruff is absent.
"""

import os
import shlex
import shutil
import subprocess
import sys

import pytest

from engine.guard_manager import GuardManager, _tree_python_files

SCRIPT_NAME = "probe_tests.py"
MARKER_NAME = "tests_ran.marker"
RUFF_BIN = shutil.which("ruff")


# ── Helpers ───────────────────────────────────────────────────────


def _git_env() -> dict:
    """Environment without leaked GIT_* vars (DF-008) — same reason as
    tests/test_guard_log_persistence.py: these tests git-init scratch dirs,
    and an inherited GIT_INDEX_FILE/GIT_DIR from a pre-commit hook would
    point them at the outer repo."""
    return {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}


def _git(workdir: str, *args: str) -> None:
    subprocess.run(["git", *args], cwd=workdir, capture_output=True, check=True, env=_git_env())


def _scratch_repo(tmp_path, files: dict[str, str]) -> str:
    """A real git repo with *files* committed and a clean index."""
    workdir = tmp_path / "scratch"
    workdir.mkdir()
    for name, content in files.items():
        path = workdir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    _git(str(workdir), "init", "-q")
    _git(str(workdir), "config", "user.email", "test@example.com")
    _git(str(workdir), "config", "user.name", "test")
    _git(str(workdir), "add", "-A")
    _git(str(workdir), "commit", "-qm", "init")
    return str(workdir)


def _marker_test_config() -> dict:
    """Guards config whose test command writes a marker file into the repo.

    The marker is the execution proof: a skip writes nothing, so an empty
    tree plus an existing marker means the command actually ran.
    """
    cmd = (
        f"{shlex.quote(sys.executable)} -c \"open({MARKER_NAME!r}, 'w')"
        ".write('ran'); print('1 passed in 0.01s')\""
    )
    return {
        "guards": {
            "secrets": False,
            "lint": True,
            "tests": True,
            "test_mode": "full",
            "test_command": cmd,
            "test_timeout": 60,
        }
    }


@pytest.fixture
def ruff_on_path(monkeypatch):
    """Pin the linter to the repo venv's ruff by putting its directory first
    on PATH. Fails loudly (no skip) when ruff is not installed — the forced
    lint tests must grade with a real linter, never silently no-op."""
    if RUFF_BIN is None:
        pytest.fail("ruff is not on PATH — the whole-tree lint tests need the repo venv's ruff")
    ruff_dir = os.path.dirname(RUFF_BIN)
    monkeypatch.setenv("PATH", ruff_dir + os.pathsep + os.environ.get("PATH", ""))
    return ruff_dir


# ── AC 1: --full runs the tests lane on a clean tree ──────────────


class TestFullTreeTestsLane:
    def test_full_tree_runs_test_command_with_empty_index(self, tmp_path):
        """grade_full_tree + nothing staged → the configured test_command
        executes (marker file appears) and the lane is not skipped."""
        workdir = _scratch_repo(tmp_path, {"pkg.py": "x = 1\n"})
        gm = GuardManager(workdir, config=_marker_test_config(), grade_full_tree=True)

        result = gm._check_tests()

        assert result.skipped is False
        assert result.passed is True
        assert os.path.isfile(os.path.join(workdir, MARKER_NAME)), (
            "test_command never executed — marker file missing"
        )

    def test_full_tree_runs_test_command_in_diff_mode_too(self, tmp_path):
        """The whole-tree opt-in is independent of test_mode: --full may
        override a diff-mode config, and the clean tree must still run the
        full command instead of landing on the diff-mode skips."""
        workdir = _scratch_repo(tmp_path, {"pkg.py": "x = 1\n"})
        config = _marker_test_config()
        config["guards"]["test_mode"] = "diff"
        gm = GuardManager(workdir, config=config, grade_full_tree=True)

        result = gm._check_tests()

        assert result.skipped is False
        assert os.path.isfile(os.path.join(workdir, MARKER_NAME))


# ── AC 2: --full lints the working tree when nothing is staged ────


class TestFullTreeLintLane:
    def test_full_tree_lints_tracked_files_when_nothing_staged(self, tmp_path, ruff_on_path):
        """grade_full_tree + clean tree → ruff grades the tracked .py files
        and the output names the graded scope."""
        workdir = _scratch_repo(tmp_path, {"a.py": "x = 1\n", "b.py": "y = 2\n"})
        gm = GuardManager(workdir, config={"guards": {"secrets": False}}, grade_full_tree=True)

        result = gm._check_lint()

        assert result.skipped is False
        assert result.passed is True
        assert result.output == "ruff: clean (2 tracked files)"

    def test_full_tree_lints_untracked_non_ignored_files_too(self, tmp_path, ruff_on_path):
        """--others --exclude-standard semantics: an untracked, non-ignored
        .py file with a genuine finding is part of the graded tree."""
        workdir = _scratch_repo(tmp_path, {"a.py": "x = 1\n"})
        # Untracked + not ignored (no .gitignore in the scratch repo).
        with open(os.path.join(workdir, "untracked_dirty.py"), "w") as f:
            f.write("import os\n")

        gm = GuardManager(workdir, config={"guards": {"secrets": False}}, grade_full_tree=True)
        result = gm._check_lint()

        assert result.skipped is False
        assert result.passed is False
        assert "F401" in result.output

    def test_full_tree_clean_lint_output_names_scope_in_full_output(self, tmp_path, ruff_on_path):
        """The scope line survives into the run log's full output: raw ruff
        stdout is EMPTY on a clean tree, so without the prefix the log's
        evidence lookup would render an empty lint body and a post-mortem
        could not tell what was graded."""
        workdir = _scratch_repo(tmp_path, {"a.py": "x = 1\n", "b.py": "y = 2\n"})
        gm = GuardManager(workdir, config={"guards": {"secrets": False}}, grade_full_tree=True)

        gm._check_lint()

        full_output = gm._full_outputs.get("lint", "")
        assert full_output.startswith("ruff: clean (2 tracked files)")

    def test_full_tree_lint_fails_on_real_ruff_finding(self, tmp_path, ruff_on_path):
        """AC 5: the forced lane GRADES — a genuine ruff finding (F401,
        unused import) in a tracked file fails the lane. passed is False,
        not merely non-empty output."""
        workdir = _scratch_repo(tmp_path, {"dirty.py": "import os\n"})
        gm = GuardManager(workdir, config={"guards": {"secrets": False}}, grade_full_tree=True)

        result = gm._check_lint()

        assert result.passed is False
        assert result.skipped is False
        assert "F401" in result.output

    def test_full_tree_lint_honest_skip_with_no_python_files(self, tmp_path, ruff_on_path):
        """A tree with no Python files at all keeps the honest skip — the
        linter is never invoked with an empty file list."""
        workdir = _scratch_repo(tmp_path, {"README.md": "# nothing to lint\n"})
        gm = GuardManager(workdir, config={"guards": {"secrets": False}}, grade_full_tree=True)

        result = gm._check_lint()

        assert result.skipped is True
        assert result.skip_reason == "no staged files"

    def test_staged_files_take_precedence_over_tree(self, tmp_path, ruff_on_path):
        """With a non-empty index the staged set is graded — not the whole
        tree. The committed-but-unstaged dirty file must NOT be graded."""
        workdir = _scratch_repo(tmp_path, {"committed_dirty.py": "import os\n"})
        # Stage one clean file; leave the committed dirty file unstaged.
        with open(os.path.join(workdir, "staged_clean.py"), "w") as f:
            f.write("x = 1\n")
        _git(workdir, "add", "staged_clean.py")

        gm = GuardManager(workdir, config={"guards": {"secrets": False}}, grade_full_tree=True)
        result = gm._check_lint()

        assert result.skipped is False
        assert result.passed is True
        assert result.output == "ruff: clean (1 tracked files)"

    def test_tree_python_files_helper_lists_tracked_plus_untracked(self, tmp_path):
        """_tree_python_files mirrors lang_detect's listing: tracked +
        untracked-but-not-ignored .py files, deduped, nothing else."""
        workdir = _scratch_repo(tmp_path, {"a.py": "x = 1\n", "notes.md": "hi\n"})
        with open(os.path.join(workdir, "b.py"), "w") as f:
            f.write("y = 2\n")
        with open(os.path.join(workdir, ".gitignore"), "w") as f:
            f.write("ignored.py\n")
        with open(os.path.join(workdir, "ignored.py"), "w") as f:
            f.write("z = 3\n")
        _git(workdir, "add", ".gitignore")
        _git(workdir, "commit", "-qm", "gitignore")

        files = _tree_python_files(workdir)

        assert sorted(files) == ["a.py", "b.py"]


# ── AC 4: default construction keeps today's skip semantics ───────


class TestDefaultManagerUnchanged:
    def test_default_tests_lane_skips_on_clean_tree(self, tmp_path):
        """Without the opt-in, the same clean tree still produces the
        TRUST-001 skip from the tests lane."""
        workdir = _scratch_repo(tmp_path, {"pkg.py": "x = 1\n"})
        gm = GuardManager(workdir, config=_marker_test_config())

        result = gm._check_tests()

        assert result.skipped is True
        assert result.skip_reason == "no staged files"
        assert not os.path.isfile(os.path.join(workdir, MARKER_NAME)), (
            "test_command must not run under default construction"
        )

    def test_default_lint_lane_skips_on_clean_tree(self, tmp_path, ruff_on_path):
        """Without the opt-in, the same clean tree still produces the
        TRUST-001 skip from the lint lane."""
        workdir = _scratch_repo(tmp_path, {"pkg.py": "x = 1\n"})
        gm = GuardManager(workdir, config={"guards": {"secrets": False}})

        result = gm._check_lint()

        assert result.skipped is True
        assert result.skip_reason == "no staged files"
        assert result.output == "No Python files staged"

    def test_grade_full_tree_is_keyword_only(self):
        """The opt-in must not become a positional argument — every existing
        caller constructs GuardManager(workdir, config) and must keep
        working (and keep skipping) unchanged."""
        import inspect

        sig = inspect.signature(GuardManager.__init__)
        grade_param = sig.parameters["grade_full_tree"]
        assert grade_param.kind is inspect.Parameter.KEYWORD_ONLY
        assert grade_param.default is False


# ── run_all plumbing: extra carries the flag for the CLI/log ──────


class TestRunAllExtraFlag:
    def test_run_all_extra_carries_grade_full_tree(self, tmp_path):
        """run_all() exposes the opt-in through extra so the CLI can print
        the whole-tree mode note without re-deriving it."""
        workdir = _scratch_repo(tmp_path, {"pkg.py": "x = 1\n"})
        config = {
            "guards": {
                "secrets": False,
                "lint": False,
                "tests": False,
                "test_mode": "full",
            }
        }
        off = GuardManager(workdir, config=config).run_all()
        on = GuardManager(workdir, config=config, grade_full_tree=True).run_all()

        assert off.extra["grade_full_tree"] is False
        assert on.extra["grade_full_tree"] is True

    def test_log_scope_line_names_whole_tree(self, tmp_path):
        """The persisted run log's test_targets line distinguishes a
        whole-tree run from an ordinary full-mode run."""
        from engine.guard_manager import _log_test_scope

        assert _log_test_scope({"test_mode": "full"}) == "all (full mode)"
        assert _log_test_scope({"test_mode": "full", "grade_full_tree": True}) == "all (whole tree)"
