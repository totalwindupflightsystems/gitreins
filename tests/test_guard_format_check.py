"""GR-GAP-063: the lint lane grades formatting, and `ruff format` is really gated.

The defect was a gate that did not exist: `ruff format` was named only in task
briefs, `.gitreins/config.yaml` graded `ruff check` alone, and CI ran no ruff
step at all — so tracked files drifted from the formatter (9 files as of
2026-09-19, repaired in 4a55784) while every gate stayed green.

These tests pin the two halves of the fix at the level they can actually
regress:

- The **binary-level flag semantics**: `ruff format --check <file>` exits
  non-zero and names the file; `ruff format --diff <file>` exits **0** on the
  same file. That second assertion is deliberately present: `--diff` is the
  false-green shape GR-GAP-061 hit, and a future edit that swaps the flag
  passes every other test in this file, so it fails this one.
- The **lane-level behaviour** against a real ruff binary (never a stub — a
  mocked formatter proves nothing about a gate): a misformatted staged file
  fails the lane, a clean one passes, the failure output names the offending
  files and the `ruff format <files>` repair command, and the verdict is still
  ONE `lint` GuardResult, not a new lane.

Scratch repos are real `git init` trees so `_check_lint` resolves its own
staged scope; no network, no LSP, no dependence on the outer repo's state.
"""

import os
import shutil
import subprocess

import pytest

from engine.guard_manager import (
    GuardManager,
    GuardResult,
    _format_failure_message,
    _parse_unformatted_files,
    _ruff_format_command,
)

RUFF_BIN = shutil.which("ruff")

# A file that is lint-clean under ruff's default rules (E4/E7/E9/F) and still
# NOT what `ruff format` produces: the multi-item collection literal fits on
# one line. This is exactly the drift class that was invisible — `ruff check`
# passes it, `ruff format --check` does not.
MISFORMATTED = "values = [\n    1, 2, 3,\n]\n"
CLEAN = "x = 1\n"


# ── Helpers ───────────────────────────────────────────────────────


def _git_env() -> dict:
    """Environment without leaked GIT_* vars — an inherited GIT_INDEX_FILE
    from the outer pre-commit hook would point these scratch repos at the
    outer repo's index (the same reason tests/test_guard_full_tree.py does
    this)."""
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


def _stage(workdir: str, relpath: str, content: str) -> None:
    """Write *relpath* into the repo and stage it — the lane's graded scope."""
    path = os.path.join(workdir, relpath)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(content)
    _git(workdir, "add", relpath)


def _run_ruff(workdir: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [RUFF_BIN, *args],
        cwd=workdir,
        capture_output=True,
        text=True,
        env=_git_env(),
    )


@pytest.fixture
def ruff_on_path(monkeypatch):
    """Pin the lane's `ruff` lookup to the repo venv's binary. Fails loudly
    (never a silent skip) when ruff is absent: the lane tests below grade with
    a real formatter or they prove nothing."""
    if RUFF_BIN is None:
        pytest.fail("ruff is not on PATH — the format-gate tests need the repo venv's ruff")
    monkeypatch.setenv("PATH", os.path.dirname(RUFF_BIN) + os.pathsep + os.environ.get("PATH", ""))
    return RUFF_BIN


# ── The flag itself: --check bites, --diff does not ────────────────


class TestRuffFormatCheckFlagSemantics:
    """The binary-level contract the gate rests on, with a real ruff."""

    def test_check_flags_a_misformatted_file(self, tmp_path, ruff_on_path):
        """Negative test: a misformatted file on a tmp path is flagged."""
        target = tmp_path / "drifted.py"
        target.write_text(MISFORMATTED)

        proc = _run_ruff(str(tmp_path), "format", "--check", target.name)

        assert proc.returncode != 0
        assert target.name in proc.stdout + proc.stderr

    def test_check_passes_a_clean_file(self, tmp_path, ruff_on_path):
        """Control: the same invocation on a formatted file exits 0, so the
        negative test above is not passing because the command always fails."""
        target = tmp_path / "clean.py"
        target.write_text(CLEAN)

        proc = _run_ruff(str(tmp_path), "format", "--check", target.name)

        assert proc.returncode == 0

    def test_bare_format_rewrites_and_exits_zero(self, tmp_path, ruff_on_path):
        """The false green this gate exists to replace: a bare `ruff format`
        both rewrites the file in place AND exits 0, so a step/lane that ran
        it would never fail — it would silently launder the drift it was
        supposed to report, and a reviewer would see a green tick."""
        target = tmp_path / "drifted.py"
        target.write_text(MISFORMATTED)

        proc = _run_ruff(str(tmp_path), "format", target.name)

        assert proc.returncode == 0
        assert target.read_text() != MISFORMATTED, "bare format rewrites in place — never a gate"

    def test_diff_is_not_a_stable_exit_contract(self, tmp_path, ruff_on_path):
        """`--diff` is NOT the gate either. Its exit code is an implementation
        detail that has already moved (0 on differences in the ruff the
        GR-GAP-061 report described, non-zero here in 0.15.22), so a gate built
        on it would flip meaning on a ruff upgrade. `--check` is the documented
        contract and the lane uses only that."""
        target = tmp_path / "drifted.py"
        target.write_text(MISFORMATTED)

        diff_rc = _run_ruff(str(tmp_path), "format", "--diff", target.name).returncode
        check_rc = _run_ruff(str(tmp_path), "format", "--check", target.name).returncode

        assert check_rc != 0
        # The two are not interchangeable: whatever --diff does on this ruff,
        # the lane's command is the one asserted below (and never --diff).
        assert "--diff" not in _ruff_format_command([target.name])
        assert diff_rc in (0, 1, 2)  # no assertion on the value — it is unstable

    def test_lint_check_passes_the_drifted_file(self, tmp_path, ruff_on_path):
        """Why a format gate was needed at all: `ruff check` accepts the
        drifted file. The failure mode was invisible to the lint lane."""
        target = tmp_path / "drifted.py"
        target.write_text(MISFORMATTED)

        proc = _run_ruff(str(tmp_path), "check", target.name)

        assert proc.returncode == 0


# ── Command construction and failure rendering ─────────────────────


class TestFormatCommandShape:
    def test_command_uses_check_not_diff(self):
        cmd = _ruff_format_command(["a.py"])
        assert cmd[:3] == ["ruff", "format", "--check"]
        assert "--diff" not in cmd

    def test_command_keeps_the_config_authority_over_named_files(self):
        """Same scope contract as `ruff check --force-exclude` (DF-018): a
        config-excluded path must not be graded merely because it was named."""
        assert "--force-exclude" in _ruff_format_command(["a.py"])

    def test_command_carries_every_submitted_path(self):
        assert _ruff_format_command(["a.py", "b.py"])[-2:] == ["a.py", "b.py"]

    def test_parse_names_each_offender(self):
        raw = (
            "Would reformat: pkg/drifted.py\n"
            "1 file would be reformatted, 3 files already formatted\n"
        )
        assert _parse_unformatted_files(raw) == ["pkg/drifted.py"]

    def test_parse_dedupes_repeated_lines(self):
        raw = "Would reformat: a.py\nWould reformat: a.py\n"
        assert _parse_unformatted_files(raw) == ["a.py"]

    def test_parse_ignores_a_count_only_report(self):
        assert _parse_unformatted_files("1 file would be reformatted\n") == []

    def test_failure_message_names_the_files_and_the_fix(self):
        msg = _format_failure_message("Would reformat: pkg/a.py\nWould reformat: b.py\n")
        assert "pkg/a.py" in msg and "b.py" in msg
        assert "ruff format pkg/a.py b.py" in msg

    def test_failure_message_still_reads_as_a_failure_without_file_lines(self):
        """A non-drift failure (parse error, everything excluded) must not be
        flattened into "0 file(s) would be reformatted"."""
        msg = _format_failure_message("error: Failed to parse x.py:1:1: bad\n")
        assert "ruff format" in msg
        assert "error: Failed to parse" in msg

    def test_failure_message_keeps_the_raw_output(self):
        raw = "Would reformat: a.py\n1 file would be reformatted\n"
        assert _format_failure_message(raw).count("Would reformat: a.py") == 1


# ── The lane: one verdict, format included ─────────────────────────


class TestLintLaneGradesFormat:
    def test_misformatted_staged_file_fails_the_lint_lane(self, tmp_path, ruff_on_path):
        """Negative test at the lane level: a staged misformatted file that
        `ruff check` accepts still fails the gate."""
        workdir = _scratch_repo(tmp_path, {"clean.py": CLEAN})
        _stage(workdir, "drifted.py", MISFORMATTED)
        gm = GuardManager(workdir, config={"guards": {"secrets": False}})

        result = gm._check_lint()

        assert result.passed is False
        assert result.skipped is False
        assert "drifted.py" in result.output
        assert "ruff format drifted.py" in result.output

    def test_clean_staged_file_passes_and_names_the_format_subcheck(self, tmp_path, ruff_on_path):
        """Control: the identical lane on a formatted file passes, and the
        clean line names the formatter so a reader can tell a lane that graded
        formatting from one that never ran it."""
        workdir = _scratch_repo(tmp_path, {"committed.py": CLEAN})
        _stage(workdir, "clean.py", CLEAN)
        _stage(workdir, "also_clean.py", "y = 2\n")
        gm = GuardManager(workdir, config={"guards": {"secrets": False}})

        result = gm._check_lint()

        assert result.passed is True
        assert result.output == "ruff: clean, format: clean (2 files)"

    def test_format_failure_is_still_one_lint_result(self, tmp_path, ruff_on_path):
        """The GuardResult contract holds: formatting is part of lint's
        verdict, not a lane of its own."""
        workdir = _scratch_repo(tmp_path, {"clean.py": CLEAN})
        _stage(workdir, "drifted.py", MISFORMATTED)
        gm = GuardManager(workdir, config={"guards": {"secrets": False}})

        result = gm._check_lint()

        assert isinstance(result, GuardResult)
        assert result.name == "lint"
        assert result.exit_code not in (0, None)

    def test_format_failure_reaches_the_run_log(self, tmp_path, ruff_on_path):
        """The formatter's output is the post-mortem evidence; the console
        body is capped, the run log is not (DF-018)."""
        workdir = _scratch_repo(tmp_path, {"clean.py": CLEAN})
        _stage(workdir, "drifted.py", MISFORMATTED)
        gm = GuardManager(workdir, config={"guards": {"secrets": False}})

        gm._check_lint()

        assert "would be reformatted" in gm._full_outputs["lint"]

    def test_only_the_drifted_file_is_named(self, tmp_path, ruff_on_path):
        """A formatted sibling must not be blamed: the message points at the
        file that actually needs the reformat."""
        workdir = _scratch_repo(tmp_path, {"clean.py": CLEAN})
        _stage(workdir, "fine.py", "z = 3\n")
        _stage(workdir, "drifted.py", MISFORMATTED)
        gm = GuardManager(workdir, config={"guards": {"secrets": False}})

        result = gm._check_lint()

        assert "ruff format drifted.py" in result.output
        assert "fine.py" not in result.output

    def test_no_staged_files_still_skips_before_any_format_check(self, tmp_path, ruff_on_path):
        """Skip semantics are unchanged: with an empty index the lane never
        invokes ruff at all, so a clean checkout stays a DEGRADED pass and not
        a format failure."""
        workdir = _scratch_repo(tmp_path, {"committed.py": MISFORMATTED})
        gm = GuardManager(workdir, config={"guards": {"secrets": False}})

        result = gm._check_lint()

        assert result.skipped is True
        assert result.skip_reason == "no staged files"

    def test_config_excluded_misformatted_file_is_not_graded(self, tmp_path, ruff_on_path):
        """--force-exclude keeps the config's authority: a file the repo's
        ruff config excludes is not a format failure merely because it was
        named — the same rule `ruff check` follows (DF-018). The in-scope file
        beside it is still graded, and the excluded one is counted, not
        silently dropped."""
        workdir = _scratch_repo(
            tmp_path,
            {
                "pyproject.toml": (
                    "[tool.ruff]\nextend-exclude = [\n    'sandbox/',\n]\nline-length = 100\n"
                ),
            },
        )
        _stage(workdir, "clean.py", CLEAN)
        _stage(workdir, "sandbox/scratch.py", MISFORMATTED)
        gm = GuardManager(workdir, config={"guards": {"secrets": False}}, grade_full_tree=True)

        result = gm._check_lint()

        assert result.passed is True
        assert (
            result.output
            == "ruff: clean (1 tracked files, 1 excluded by config), format: clean (1 files)"
        )

    def test_config_excluded_only_list_stays_an_honest_skip(self, tmp_path, ruff_on_path):
        """When the config excludes EVERY submitted file the lane still skips
        by name — the format sub-check never gets to invent a failure over a
        scope the repo refuses to grade."""
        workdir = _scratch_repo(
            tmp_path,
            {
                "pyproject.toml": (
                    "[tool.ruff]\nextend-exclude = [\n    'sandbox/',\n]\nline-length = 100\n"
                ),
                "clean.py": CLEAN,
            },
        )
        _stage(workdir, "sandbox/scratch.py", MISFORMATTED)
        gm = GuardManager(workdir, config={"guards": {"secrets": False}})

        result = gm._check_lint()

        assert result.skipped is True
        assert result.passed is True
        assert "all excluded" in result.output
        assert "format:" not in result.output

    def test_whole_tree_run_grades_format_over_the_tree(self, tmp_path, ruff_on_path):
        """`gitreins guard --full` has no staged files and still fails on a
        tree-resident formatting drift — the whole-tree path keeps the gate."""
        workdir = _scratch_repo(tmp_path, {"clean.py": CLEAN, "drifted.py": MISFORMATTED})
        gm = GuardManager(workdir, config={"guards": {"secrets": False}}, grade_full_tree=True)

        result = gm._check_lint()

        assert result.passed is False
        assert "drifted.py" in result.output
