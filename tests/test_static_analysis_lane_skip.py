"""GR-MYPY-001 — the static-analysis lane must skip a diff it cannot touch.

The lane runs its tools WHOLE-TREE (``_build_command`` sends mypy ``.``
with ``--explicit-package-bases``). On a commit whose staged/diff set has
no file in the tools' language (a board-only commit: a .md note, a .jsonl
row), the whole-tree run grades only files the commit cannot have affected
— and a cold checker cache can surface unrelated diagnostics (or blow the
timeout) and turn ``gitreins guard`` into a hard FAIL that blocks the
commit. The lane must SKIP in that situation — which the substantive-gate
net (TRUST-001, ``_is_substantive_step``) surfaces as a DEGRADED pass with
the skip reason as the disclosure — never FAIL.
"""

import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from engine.guard_manager import GuardManager
from engine.static_analysis import diff_files_match_tools, tool_languages


# ── Fixture: a real git repo whose committed tree would fail mypy ─────────


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
    )


@pytest.fixture
def py_repo(tmp_path: Path) -> Path:
    """Git repo: Python packaging marker + committed .py that mypy rejects."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "test")
    (repo / "pyproject.toml").write_text("[project]\nname = 'consumer'\n", encoding="utf-8")
    (repo / "bad.py").write_text('x: int = "not an int"\n', encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    return repo


def _stage_board_only(repo: Path) -> None:
    """Stage a board-only change set: no file in any tool's language."""
    (repo / "docs").mkdir()
    (repo / "docs" / "notes.md").write_text("# note\n", encoding="utf-8")
    (repo / "tasks.jsonl").write_text('{"id": 1}\n', encoding="utf-8")
    _git(repo, "add", "docs/notes.md", "tasks.jsonl")


def _stage_python(repo: Path) -> None:
    (repo / "worse.py").write_text("y: str = 123\n", encoding="utf-8")
    _git(repo, "add", "worse.py")


_POISON_DIAGS = [
    {"file": "bad.py", "line": 1, "severity": "error", "message": "boom", "tool": "mypy"}
]


def _manager(repo: Path) -> GuardManager:
    return GuardManager(
        str(repo),
        {
            "guards": {
                "secrets": False,
                "lint": False,
                "tests": False,
                "static_analysis": True,
                "static_analysis_tools": {"python": ["mypy"]},
            }
        },
    )


@contextmanager
def _patched_tool_infrastructure(
    diags: list[dict[str, object]],
) -> Iterator[tuple[MagicMock, MagicMock]]:
    """find_tool resolves every tool; run_static_check returns ``diags``.

    Patched on engine.static_analysis (not guard_manager) because the lane
    imports the helpers at call time — the same seam the DF-019 tests use.
    """
    with (
        patch(
            "engine.static_analysis.find_tool", side_effect=lambda tool: f"/fake/{tool}"
        ) as mock_find,
        patch("engine.static_analysis.run_static_check", return_value=list(diags)) as mock_run,
    ):
        yield mock_run, mock_find


# ── The core regression: board-only staged set → SKIP, tool never runs ────


class TestBoardOnlyDiffSkipsTheLane:
    def test_board_only_staged_set_skips_without_running_the_tool(self, py_repo: Path) -> None:
        """A .md/.jsonl staged set must not grade the tree — SKIP, not FAIL."""
        _stage_board_only(py_repo)
        manager = _manager(py_repo)

        with _patched_tool_infrastructure(_POISON_DIAGS) as (mock_run, _mock_find):
            result = manager._check_static_analysis()

        assert result.passed is True
        assert result.skipped is True
        assert result.skip_reason, "a skip must carry a machine-readable reason"
        assert "no files matching" in result.skip_reason
        assert "mypy" in result.skip_reason
        # The whole-tree run never happened — a cold cache cannot be consulted.
        mock_run.assert_not_called()

    def test_board_only_skip_discloses_the_cold_cache_tradeoff(self, py_repo: Path) -> None:
        """The skip reason names why the whole-tree run was skipped."""
        _stage_board_only(py_repo)
        manager = _manager(py_repo)

        with _patched_tool_infrastructure(_POISON_DIAGS):
            result = manager._check_static_analysis()

        assert "cold" in result.skip_reason
        assert "whole-tree" in result.skip_reason

    def test_board_only_skip_arms_the_degraded_net(self, py_repo: Path) -> None:
        """static_analysis is substantive: the skip DEGRADES the run, disclosing."""
        from engine.types import Tier1Result, _is_substantive_step

        _stage_board_only(py_repo)
        manager = _manager(py_repo)

        with _patched_tool_infrastructure(_POISON_DIAGS):
            result = manager._check_static_analysis()

        tier1 = Tier1Result(passed=True, results=[result])
        assert _is_substantive_step("static_analysis") is True
        assert tier1.degraded is True
        assert tier1.degraded_steps == [{"step": "static_analysis", "reason": result.skip_reason}]

    def test_staged_python_file_still_runs_the_lane(self, py_repo: Path) -> None:
        """A .py file in the staged set keeps the whole-tree grading behavior."""
        _stage_python(py_repo)
        manager = _manager(py_repo)

        with _patched_tool_infrastructure([]) as (mock_run, _mock_find):
            result = manager._check_static_analysis()

        assert result.skipped is False
        assert result.passed is True
        assert "mypy — clean" in result.output
        mock_run.assert_called_once()

    def test_empty_staged_set_keeps_legacy_whole_tree_behavior(self, tmp_path: Path) -> None:
        """No diff information at all (non-repo probe) → legacy run, no new skip."""
        (tmp_path / "main.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
        (tmp_path / "pyproject.toml").write_text("[project]\nname = 'consumer'\n", encoding="utf-8")
        manager = GuardManager(
            str(tmp_path),
            {
                "guards": {
                    "secrets": False,
                    "lint": False,
                    "tests": False,
                    "static_analysis": True,
                    "static_analysis_tools": {"python": ["mypy"]},
                }
            },
        )

        with _patched_tool_infrastructure([]) as (mock_run, _mock_find):
            result = manager._check_static_analysis()

        assert result.skipped is False
        assert result.passed is True
        mock_run.assert_called_once()


# ── diff_files_match_tools unit contract ──────────────────────────────────


class TestDiffFilesMatchTools:
    def test_python_file_matches_mypy(self) -> None:
        assert diff_files_match_tools(["engine/foo.py"], ["mypy"]) is True

    def test_stub_file_matches_mypy(self) -> None:
        assert diff_files_match_tools(["stubs/mod.pyi"], ["mypy"]) is True

    def test_board_only_files_do_not_match_mypy(self) -> None:
        assert diff_files_match_tools(["docs/notes.md", "tasks.jsonl"], ["mypy"]) is False

    def test_match_is_case_insensitive(self) -> None:
        assert diff_files_match_tools(["Engine/Foo.PY"], ["mypy"]) is True

    def test_empty_diff_set_fails_open_to_grading(self) -> None:
        assert diff_files_match_tools([], ["mypy"]) is True

    def test_unknown_tool_fails_open_to_grading(self) -> None:
        assert diff_files_match_tools(["docs/notes.md"], ["custom-lint"]) is True

    def test_js_covers_typescript_like_detection(self) -> None:
        assert diff_files_match_tools(["src/app.tsx"], ["eslint"]) is True
        assert diff_files_match_tools(["src/app.ts"], ["eslint"]) is True
        assert diff_files_match_tools(["README.md"], ["eslint"]) is False

    def test_cpp_uses_lang_detect_suffixes(self) -> None:
        assert diff_files_match_tools(["src/main.cc"], ["cppcheck"]) is True
        assert diff_files_match_tools(["src/main.hpp"], ["cppcheck"]) is True

    def test_tool_languages_dedupes_and_passes_unknown_through(self) -> None:
        assert tool_languages(["mypy", "pyright"]) == ["python"]
        assert tool_languages(["mypy", "custom-lint"]) == ["python", "custom-lint"]


# ── End-to-end with the real mypy binary (skipped when mypy is absent) ────


def _mypy_bin_dir() -> str | None:
    """A directory containing a mypy binary, or None (bunker CI has none)."""
    import shutil

    found = shutil.which("mypy")
    if found:
        return str(Path(found).parent)
    venv_bin = Path(sys.executable).parent
    if (venv_bin / "mypy").exists():
        return str(venv_bin)
    return None


def _put_mypy_on_path(monkeypatch: pytest.MonkeyPatch, bin_dir: str) -> None:
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")


class TestRealMypyEndToEnd:
    def test_board_only_commit_with_real_cold_mypy_skips(
        self, py_repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The FAILIF scenario, live: cold mypy cache + board-only staged set."""
        bin_dir = _mypy_bin_dir()
        if bin_dir is None:
            pytest.skip("mypy binary not available on this host")
        _put_mypy_on_path(monkeypatch, bin_dir)

        _stage_board_only(py_repo)
        manager = _manager(py_repo)

        result = manager._check_static_analysis()

        assert result.passed is True
        assert result.skipped is True
        # The tree holds a committed mypy error, so a whole-tree run on the
        # cold cache WOULD have produced diagnostics. Proof it never ran:
        # mypy's cache directory was never even created in the repo.
        assert not (py_repo / ".mypy_cache").exists()

    def test_staged_python_file_with_real_mypy_still_fails(
        self, py_repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Control arm: a .py staged set really does grade (and really fails)."""
        bin_dir = _mypy_bin_dir()
        if bin_dir is None:
            pytest.skip("mypy binary not available on this host")
        _put_mypy_on_path(monkeypatch, bin_dir)

        _stage_python(py_repo)
        manager = _manager(py_repo)

        result = manager._check_static_analysis()

        assert result.passed is False
        assert "worse.py" in result.output
        # The whole-tree run graded the committed bad.py too — exactly the
        # unrelated-file grading the skip exists to avoid.
        assert "bad.py" in result.output
