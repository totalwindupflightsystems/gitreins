"""
GR-218 / SCHED-GAP-1753: a judge grades the DELIVERED tree, not an empty index.

`gitreins task complete` (and the worktree judge path) judges work that is
already COMMITTED, so the index is empty. Until this fix the `Judge` built its
`GuardManager` with the historical default (`grade_full_tree=False`), so on that
empty index the tier-1 lanes returned their TRUST-001 "no staged files" SKIPs
and the run announced a pass over a tree that was never linted and never
tested — a phantom pass of the same class DF-GITREINS-POC-42 fixed for the Go
lanes under `--full`.

These tests pin the contract at the `Judge` level (the GuardManager level is
already covered by tests/test_guard_full_tree.py):

- a `Judge` built over a CLEAN index grades the whole tree: the configured
  test_command executes (proven by a marker file) and the lane is not skipped;
- the tier-1 result carries ``grade_full_tree=True`` so the run log and the
  banner can never claim a graded run while skipping every lane;
- a NON-empty index is unaffected: the change set stays the index (staged
  files only), so the fix cannot silently widen every other caller's scope.

No network, no LLM: the tests exercise the tier-1 guard the Judge constructs,
which is the surface that lied. `llm=None` is safe because tier-2 is never
reached.
"""

import os
import shlex
import subprocess
import sys
from pathlib import Path

from engine.judge import Judge

MARKER_NAME = "tests_ran.marker"


def _git_env() -> dict:
    """Environment without leaked GIT_* vars (DF-008): these tests git-init
    scratch dirs, and an inherited GIT_INDEX_FILE/GIT_DIR from a pre-commit
    hook would point them at the outer repo."""
    return {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}


def _git(workdir: str, *args: str) -> None:
    subprocess.run(["git", *args], cwd=workdir, capture_output=True, check=True, env=_git_env())


def _scratch_repo(tmp_path: Path, files: dict[str, str]) -> str:
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

    The marker is the execution proof: a skip writes nothing, so a clean tree
    plus an existing marker means the command actually ran.
    """
    cmd = (
        f"{shlex.quote(sys.executable)} -c \"open({MARKER_NAME!r}, 'w')"
        ".write('ran'); print('1 passed in 0.01s')\""
    )
    return {
        "guards": {
            "secrets": False,
            "lint": False,
            "tests": True,
            "test_mode": "full",
            "test_command": cmd,
            "test_timeout": 60,
        }
    }


def test_judge_grades_whole_tree_on_clean_index(tmp_path: Path) -> None:
    """The phantom pass: an empty index must still run the tests lane."""
    workdir = _scratch_repo(tmp_path, {"pkg.py": "x = 1\n"})
    judge = Judge(None, workdir, guard_config=_marker_test_config())  # type: ignore[arg-type]

    result = judge.guard_manager._check_tests()

    assert result.skipped is False, "clean-index judge run skipped the tests lane"
    assert result.passed is True
    assert os.path.isfile(os.path.join(workdir, MARKER_NAME)), (
        "test_command never executed under the judge — marker file missing"
    )


def test_judge_tier1_extra_declares_full_tree(tmp_path: Path) -> None:
    """The run log/banner must know the run graded the whole tree."""
    workdir = _scratch_repo(tmp_path, {"pkg.py": "x = 1\n"})
    judge = Judge(None, workdir, guard_config=_marker_test_config())  # type: ignore[arg-type]

    result = judge.guard_manager.run_all()

    assert result.extra.get("grade_full_tree") is True
    assert "no staged files" not in " ".join(result.skipped_steps), (
        f"clean-index judge run still reported index skips: {result.skipped_steps}"
    )


def test_judge_keeps_index_scope_when_something_is_staged(tmp_path: Path) -> None:
    """Blast-radius pin: a non-empty index is graded as the index, unchanged."""
    workdir = _scratch_repo(tmp_path, {"a.py": "a = 1\n", "b.py": "b = 1\n"})
    # Stage a change to a.py only; leave b.py's change unstaged.
    (Path(workdir) / "a.py").write_text("a = 2\n")
    _git(workdir, "add", "a.py")
    (Path(workdir) / "b.py").write_text("b = 2\n")

    judge = Judge(None, workdir, guard_config=_marker_test_config())  # type: ignore[arg-type]

    assert judge.guard_manager.changed_files == ["a.py"], (
        f"staged change set widened: {judge.guard_manager.changed_files}"
    )


GO_BROKEN = """package main

func main() {
	x := undefinedIdentifier
	_ = x
}
"""


def test_judge_go_lanes_grade_a_committed_broken_tree(tmp_path: Path) -> None:
    """The AC's go_build/go_lint/go_tests: a clean index must not skip them.

    A Go tree that is committed (clean index) with a compile error must FAIL
    under the judge. Pre-fix the lanes returned "no Go files" SKIPs and the run
    printed a DEGRADED pass over code that does not build.
    """
    workdir = _scratch_repo(
        tmp_path,
        {"go.mod": "module probe\n\ngo 1.21\n", "main.go": GO_BROKEN},
    )
    judge = Judge(  # type: ignore[arg-type]
        None,
        workdir,
        guard_config={"guards": {"secrets": False, "lint": False, "tests": False}},
    )

    result = judge.guard_manager.run_all()

    go_build = next(r for r in result.results if r.name == "go_build")
    assert getattr(go_build, "skipped", False) is False, (
        f"go_build skipped on a committed Go tree: {go_build.output[:200]}"
    )
    assert go_build.passed is False, "go_build passed over code that does not compile"
    assert result.passed is False, "clean-index judge run passed a tree that does not build"


def test_judge_opt_out_keeps_scope_only_semantics(tmp_path: Path) -> None:
    """EVID-002: the EPHEMERAL judge must still grade only the requested scope.

    `judge --ephemeral --scope staged` promises that an untracked file is
    invisible (tests/test_cli.py::TestJudgeEphemeralCLI pins the behaviour end
    to end). The whole-tree default is opt-OUT for exactly that caller, so a
    clean index must still SKIP the lane here instead of silently widening.
    """
    workdir = _scratch_repo(tmp_path, {"pkg.py": "x = 1\n"})
    (Path(workdir) / "notes.py").write_text("OPENAI_API_KEY = 'sk-probe-probe-probe'\n")
    judge = Judge(  # type: ignore[arg-type]
        None,
        workdir,
        guard_config=_marker_test_config(),
        grade_full_tree=False,
    )

    result = judge.guard_manager._check_tests()

    assert result.skipped is True, "opt-out judge widened a staged-scope run to the whole tree"
    assert not os.path.isfile(os.path.join(workdir, MARKER_NAME))
