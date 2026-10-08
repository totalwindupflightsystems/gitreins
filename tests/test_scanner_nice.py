"""DF-GITREINS-POC-55 — every spawned external scanner runs ``nice`` by default.

Bane directive 2026-09-24: *"when gitreins is running and it's gitleaks it should
run with nice by default and configurable by environment or as a setting"*.

The claims this file pins, one per acceptance criterion:

* **AC1a** — the BUILT command strings name the prefix (hermetic assertions on
  ``_secrets_step_run`` / ``_lint_step_run`` / ``tier1_plan``).
* **AC1b** — the child process really gets the priority: a shim placed first on
  PATH records ``os.nice(0)`` from INSIDE the spawned scanner and then execs the
  real binary, so the number comes from the kernel, not from a string.
* **AC2** — level 0 is byte-identical to the pre-change builder. The reference
  text is transcribed verbatim in ``_prechange_*`` below (it is not a call back
  into the builder), so this is a real comparison, not a tautology.
* **AC3** — verdict semantics are unchanged at every level: a planted secret
  still FAILs the secrets leg and a clean tree still passes, at nice 10 and 19.
* **AC4** — a missing or non-runnable ``nice`` (PATH without it, or a shim that
  exits 127) still RUNS the scan, reports its result and says so exactly once.

RED/control split (proven by running this file against the pre-change engine):
the AC1/AC3/AC4 tests FAIL there (no prefix in the built string, no nice value in
the spawned process); the AC2 tests PASS there — they are controls, and their
passing is what validates the transcribed reference text.

Scratch repos are real ``git init`` trees (same shape as
``tests/test_guard_format_check.py``) so the guard lanes resolve their own staged
scope; no network, no dependence on the outer repo's index.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from engine import config as gitreins_config
from engine import scanner_nice
from engine.guard_manager import GuardManager, HARNESS_STATE_DIRS
from engine.pipeline import (
    SKIP_SENTINEL,
    _engine_root,
    _lint_step_run,
    _secrets_step_run,
    harness_scan_gitleaks_config,
    tier1_plan,
)

# The generated gitleaks config's allowlist path list, as the step renders it.
_EXCLUSIONS = ", ".join(f"{d}/**" for d in HARNESS_STATE_DIRS)

NICE = shutil.which("nice")
RUFF = shutil.which("ruff")
GITLEAKS = shutil.which("gitleaks")
# Captured before any monkeypatch so a stand-in can delegate back to it.
_REAL_WHICH = shutil.which


@pytest.fixture(autouse=True)
def _no_ambient_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test states its own level — never inherit one from the shell."""
    monkeypatch.delenv(scanner_nice.ENV_VAR, raising=False)


@pytest.fixture(autouse=True)
def _nice_headroom() -> None:
    """The live arms compare against this process's own nice value.

    A runner already sitting at or above the default level would make a
    prefixed and an unprefixed child indistinguishable, so the arms would pass
    vacuously — skip loudly instead of pretending they proved anything.
    """
    if os.nice(0) >= scanner_nice.DEFAULT_LEVEL:
        pytest.skip(
            f"test runner already nice={os.nice(0)} — the live priority arms "
            "cannot discriminate here"
        )


# ── helpers ─────────────────────────────────────────────────────────────────


def _base_nice() -> int:
    """This process's own nice value (the level a child without a prefix gets)."""
    return os.nice(0)


def _expected_child_nice(level: int) -> int:
    """What ``nice -n <level>`` produces for a child of this process."""
    return min(_base_nice() + level, scanner_nice.MAX_LEVEL)


def _shim_dir(tmp_path: Path, program: str, record: Path, real: str | None) -> Path:
    """PATH dir whose *program* records its own ``os.nice(0)``, then execs *real*.

    The record file is the live proof: it is written by the spawned scanner
    itself (AC1b), and because ``os.execv`` keeps the priority the value read
    there is the one the real scanner ran with. With no *real* binary the shim
    exits 0 after recording, so the test also runs on a host without the tool.
    """
    shim = tmp_path / "shims"
    shim.mkdir(exist_ok=True)
    tail = f"os.execv({real!r}, [{real!r}] + sys.argv[1:])\n" if real else "sys.exit(0)\n"
    script = shim / program
    script.write_text(
        f"#!{sys.executable}\n"
        "import os, sys\n"
        f"with open({str(record)!r}, 'a') as fh:\n"
        "    fh.write(str(os.nice(0)) + '\\n')\n" + tail
    )
    script.chmod(0o755)
    return shim


def _run_step(cmd: str, workdir: Path, path: str | None = None) -> subprocess.CompletedProcess:
    """Run a built step string the way ``Pipeline._run_script_step`` does."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.pop(scanner_nice.ENV_VAR, None)
    if path is not None:
        env["PATH"] = path
    return subprocess.run(
        cmd, shell=True, cwd=str(workdir), capture_output=True, text=True, timeout=180, env=env
    )


def _recorded_nice(record: Path) -> set[str]:
    assert record.exists(), "the scanner never ran — the shim recorded nothing"
    return set(record.read_text().split())


def _git_env() -> dict:
    return {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}


def _git(workdir: str, *args: str) -> None:
    subprocess.run(["git", *args], cwd=workdir, capture_output=True, check=True, env=_git_env())


def _scratch_repo(tmp_path: Path, files: dict[str, str], staged: dict[str, str]) -> str:
    """A real git repo with *files* committed and *staged* added to the index."""
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
    for name, content in staged.items():
        path = workdir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        _git(str(workdir), "add", name)
    return str(workdir)


# ── the pre-change builders, transcribed verbatim ────────────────────────────
# These reproduce the EXACT text before DF-GITREINS-POC-55 (verbatim from
# `git show <base>:engine/pipeline.py`). They are literals on purpose: the AC2
# claim is "byte-for-byte identical to the pre-change command", which cannot be
# checked by calling the thing under test.


def _prechange_secrets_step(workdir: str) -> str:
    exclusions = _EXCLUSIONS
    return (
        "if command -v gitleaks >/dev/null 2>&1; then "
        '_glcfg="$(mktemp -t gitreins-gitleaks-XXXXXX.toml)"; '
        "cat > \"$_glcfg\" <<'GITREINS_GITLEAKS_CFG'\n"
        f"{harness_scan_gitleaks_config(workdir)}\n"
        "GITREINS_GITLEAKS_CFG\n"
        f'echo "secrets: harness state excluded from gitleaks scope ({exclusions})"; '
        'echo "secrets: scanners=gitleaks+builtin cross-check"; '
        'gitleaks detect --source . --no-git --no-banner --no-color --config "$_glcfg"; '
        '_glrc=$?; rm -f "$_glcfg"; '
        'if [ "$_glrc" -eq 0 ]; then echo "secrets: gitleaks: clean"; '
        'else echo "secrets: gitleaks: findings found (exit $_glrc)"; fi; '
        "else _glrc=0; "
        'echo "secrets: scanners=builtin cross-check only (gitleaks not on PATH)"; '
        'echo "secrets: gitleaks: not on PATH"; fi; g1=$_glrc; '
        f'PYTHONPATH="{_engine_root()}" {sys.executable} -c "from engine.guard_manager import GuardManager; '
        "import sys; gm = GuardManager('.'); "
        "r = gm._builtin_secrets_scan(staged_only=False); "
        "print('secrets: builtin cross-check: ' + r.output); "
        "print('secrets: builtin cross-check status: ' "
        "+ (r.scanners[0][1] if r.scanners else 'clean')); "
        'sys.exit(1 if not r.passed else 0)"; '
        'g2=$?; [ "$g1" -eq 0 ] && [ "$g2" -eq 0 ]'
    )


def _prechange_lint_step(lint_cmd: str) -> str:
    binary = lint_cmd.split()[0]
    return (
        f"if command -v {binary} >/dev/null 2>&1; then {lint_cmd}; "
        f'else echo "{SKIP_SENTINEL} lint=no linter on PATH ({binary} not found)"; '
        f"exit 0; fi"
    )


# ── AC1a/AC2: the built commands ────────────────────────────────────────────


class TestBuiltCommandStrings:
    def test_secrets_step_names_the_default_prefix(self, tmp_workdir: str) -> None:
        cmd = _secrets_step_run(tmp_workdir)
        assert "nice -n 10 true >/dev/null 2>&1" in cmd
        assert "$_gr_nice gitleaks detect --source . --no-git --no-banner --no-color" in cmd
        assert "scanners: nice=nice -n 10" in cmd

    def test_secrets_step_honours_the_config_level(self, tmp_workdir: str) -> None:
        cmd = _secrets_step_run(tmp_workdir, {"guards": {"scanner_nice": 19}})
        assert "$_gr_nice gitleaks detect" in cmd
        assert "nice -n 19" in cmd

    def test_secrets_step_honours_the_env_level(
        self, tmp_workdir: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(scanner_nice.ENV_VAR, "7")
        cmd = _secrets_step_run(tmp_workdir, {"guards": {"scanner_nice": 19}})
        assert "nice -n 7" in cmd
        assert "nice -n 19" not in cmd

    def test_lint_step_carries_the_prefix(self) -> None:
        cmd = _lint_step_run("ruff check .")
        assert "$_gr_nice ruff check ." in cmd
        # The missing-linter skip must survive the prefix (DF-GITREINS-POC-16).
        assert "command -v ruff >/dev/null 2>&1" in cmd

    def test_tier1_plan_wraps_every_external_lane(self, tmp_path: Path) -> None:
        workdir = tmp_path / "wd"
        workdir.mkdir()
        (workdir / "module.py").write_text("x = 1\n", encoding="utf-8")
        steps, _marker = tier1_plan(str(workdir), {"guards": {"test_command": "pytest -x"}})
        by_id = {s["id"]: s["run"] for s in steps}
        assert "$_gr_nice gitleaks detect" in by_id["secrets"]
        assert "$_gr_nice" in by_id["lint"]
        # The opaque test command is wrapped WHOLE so a chain is covered.
        assert "$_gr_nice sh -c 'pytest -x'" in by_id["tests"]

    def test_shell_wrap_covers_a_chain(self) -> None:
        wrapped = scanner_nice.shell_wrap("a.py && b.py", 10)
        assert wrapped.endswith("$_gr_nice sh -c 'a.py && b.py'")

    def test_shell_wrap_quotes_embedded_single_quotes(self) -> None:
        wrapped = scanner_nice.shell_wrap("pytest -k 'not slow'", 10)
        assert wrapped.endswith("sh -c 'pytest -k '\\''not slow'\\'''")

    # ── AC2: level 0 is byte-identical to today ──
    # These two are CONTROLS as well as claims: driven through the single-arg
    # call and the env var, both exist on the pre-change tree, so they pass
    # there too — which is what validates the transcribed reference text.

    def test_level_zero_secrets_step_is_byte_identical(
        self, tmp_workdir: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(scanner_nice.ENV_VAR, "0")
        assert _secrets_step_run(tmp_workdir) == _prechange_secrets_step(tmp_workdir)

    def test_level_zero_lint_step_is_byte_identical(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(scanner_nice.ENV_VAR, "0")
        assert _lint_step_run("ruff check .") == _prechange_lint_step("ruff check .")

    def test_config_zero_secrets_step_is_byte_identical(self, tmp_workdir: str) -> None:
        assert _secrets_step_run(tmp_workdir, {"guards": {"scanner_nice": 0}}) == (
            _prechange_secrets_step(tmp_workdir)
        )

    def test_config_zero_lint_step_is_byte_identical(self) -> None:
        assert _lint_step_run("ruff check .", {"guards": {"scanner_nice": 0}}) == (
            _prechange_lint_step("ruff check .")
        )

    def test_level_zero_test_step_keeps_the_command(self, tmp_path: Path) -> None:
        workdir = tmp_path / "wd"
        workdir.mkdir()
        (workdir / "module.py").write_text("x = 1\n", encoding="utf-8")
        steps, _marker = tier1_plan(
            str(workdir), {"guards": {"scanner_nice": 0, "test_command": "pytest -x"}}
        )
        assert next(s for s in steps if s["id"] == "tests")["run"] == "pytest -x"

    def test_level_zero_shell_wrap_is_identity(self) -> None:
        assert scanner_nice.shell_wrap("a && b", 0) == "a && b"

    def test_level_zero_policy_has_no_prefix_and_no_note(self) -> None:
        resolved = scanner_nice.policy({"guards": {"scanner_nice": 0}})
        assert resolved.level == 0
        assert resolved.prefix == ()
        assert resolved.note == ""
        assert resolved.applied is False


# ── level resolution: env > config > default ────────────────────────────────


def _defaults_scanner_nice() -> int:
    """The typed default from engine/config.py (GitReinsDefaults)."""
    return gitreins_config.GitReinsDefaults().scanner_nice


class TestLevelResolution:
    def test_default_is_ten(self) -> None:
        assert scanner_nice.resolve_level(None, {}) == (10, "default")

    def test_env_beats_config(self) -> None:
        resolved = scanner_nice.resolve_level(
            {"guards": {"scanner_nice": 3}}, {scanner_nice.ENV_VAR: "19"}
        )
        assert resolved == (19, "env")

    def test_config_beats_default(self) -> None:
        assert scanner_nice.resolve_level({"guards": {"scanner_nice": 4}}, {}) == (4, "config")

    def test_bare_level_and_settings_object_are_accepted(self) -> None:
        assert scanner_nice.resolve_level(5, {}) == (5, "config")
        assert _defaults_scanner_nice() == 10
        assert scanner_nice.resolve_level(gitreins_config.GitReinsDefaults(), {}) == (10, "config")

    @pytest.mark.parametrize("value", ["", " ", "abc", "1.5", "-1", "20", "1000"])
    def test_unusable_env_values_fall_through_to_config(self, value: object) -> None:
        env = {scanner_nice.ENV_VAR: value}
        assert scanner_nice.resolve_level({"guards": {"scanner_nice": 6}}, env) == (6, "config")
        assert scanner_nice.resolve_level(None, env) == (10, "default")

    @pytest.mark.parametrize("value", [True, False, 20, -3, "x", None])
    def test_parse_level_rejects_unusable_values(self, value: object) -> None:
        assert scanner_nice.parse_level(value) is None

    @pytest.mark.parametrize("value,expected", [(0, 0), (19, 19), ("12", 12), (10, 10)])
    def test_parse_level_accepts_the_range(self, value: object, expected: object) -> None:
        assert scanner_nice.parse_level(value) == expected

    def test_config_loader_reads_the_guards_block(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        (repo / ".gitreins").mkdir(parents=True)
        (repo / ".gitreins" / "config.yaml").write_text(
            "guards:\n  scanner_nice: 19\n", encoding="utf-8"
        )
        assert gitreins_config.load_defaults(str(repo)).scanner_nice == 19

    def test_config_loader_rejects_an_out_of_range_level(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        (repo / ".gitreins").mkdir(parents=True)
        (repo / ".gitreins" / "config.yaml").write_text(
            "guards:\n  scanner_nice: 42\n", encoding="utf-8"
        )
        # Loud in the log, harmless on the tree: the default applies, and a
        # scheduling nicety can never fail a run (fail-open).
        assert gitreins_config.load_defaults(str(repo)).scanner_nice == 10

    def test_config_loader_tolerates_a_scalar_guards_block(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        (repo / ".gitreins").mkdir(parents=True)
        (repo / ".gitreins" / "config.yaml").write_text("guards: true\n", encoding="utf-8")
        assert gitreins_config.load_defaults(str(repo)).scanner_nice == 10

    def test_argv_prefix_is_withheld_for_a_missing_program(self) -> None:
        """`nice` EXECS its argv: a miss inside a prefix would hide the miss."""
        prefix, note = scanner_nice.argv_prefix(10, "definitely-no-such-scanner-xyz")
        assert prefix == ()
        assert note == ""

    def test_nice_note_text_is_stable(self) -> None:
        assert scanner_nice.note_applied(10) == "scanners: nice=nice -n 10"
        assert scanner_nice.note_unavailable("not on PATH") == (
            "scanners: nice unavailable (not on PATH); running at default priority"
        )


def scaner_defaults() -> int:
    """The typed default from engine/config.py (GitReinsDefaults)."""
    return gitreins_config.GitReinsDefaults().scanner_nice


# ── AC1b: the spawned process really is nice ────────────────────────────────


class TestLiveSpawnedPriority:
    def test_default_scan_runs_at_nice_10(self, tmp_path: Path) -> None:
        workdir = tmp_path / "wd"
        workdir.mkdir()
        record = tmp_path / "nice.txt"
        shim = _shim_dir(tmp_path, "gitleaks", record, GITLEAKS)
        proc = _run_step(
            _secrets_step_run(str(workdir)),
            workdir,
            path=f"{shim}:{os.environ['PATH']}",
        )
        assert _recorded_nice(record) == {str(_expected_child_nice(10))}
        assert "scanners: nice=nice -n 10" in proc.stdout
        assert proc.returncode == 0, proc.stdout + proc.stderr

    def test_config_level_19_reaches_the_child(self, tmp_path: Path) -> None:
        workdir = tmp_path / "wd"
        workdir.mkdir()
        record = tmp_path / "nice.txt"
        shim = _shim_dir(tmp_path, "gitleaks", record, GITLEAKS)
        proc = _run_step(
            _secrets_step_run(str(workdir), {"guards": {"scanner_nice": 19}}),
            workdir,
            path=f"{shim}:{os.environ['PATH']}",
        )
        assert _recorded_nice(record) == {str(_expected_child_nice(19))}
        assert "scanners: nice=nice -n 19" in proc.stdout

    def test_level_zero_child_stays_unprefixed(self, tmp_path: Path) -> None:
        workdir = tmp_path / "wd"
        workdir.mkdir()
        record = tmp_path / "nice.txt"
        shim = _shim_dir(tmp_path, "gitleaks", record, GITLEAKS)
        proc = _run_step(
            _secrets_step_run(str(workdir), {"guards": {"scanner_nice": 0}}),
            workdir,
            path=f"{shim}:{os.environ['PATH']}",
        )
        assert _recorded_nice(record) == {str(_base_nice())}
        assert "nice=" not in proc.stdout  # off means no note line either

    def test_test_command_runs_at_the_policy_level(self, tmp_path: Path) -> None:
        """The pytest lane: the whole configured command is wrapped, not its head."""
        workdir = tmp_path / "wd"
        workdir.mkdir()
        (workdir / "module.py").write_text("x = 1\n", encoding="utf-8")
        record = tmp_path / "nice.txt"
        script = tmp_path / "recorder.py"
        script.write_text(
            f"import os, sys\nopen({str(record)!r}, 'a').write(str(os.nice(0)) + '\\n')\n",
            encoding="utf-8",
        )
        steps, _marker = tier1_plan(
            str(workdir),
            {"guards": {"test_command": f"{sys.executable} {script} && {sys.executable} {script}"}},
        )
        proc = _run_step(next(s for s in steps if s["id"] == "tests")["run"], workdir)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        # BOTH halves of the chain are nice: a prefix on the head alone would
        # leave the second command — the actual runner in this repo's config —
        # at default priority.
        assert record.read_text().split() == [
            str(_expected_child_nice(10)),
            str(_expected_child_nice(10)),
        ]
        assert "scanners: nice=nice -n 10" in proc.stdout


# ── AC4: fail-open ──────────────────────────────────────────────────────────


class TestFailOpen:
    def test_broken_nice_shim_still_runs_the_scan(self, tmp_path: Path) -> None:
        """A `nice` that exits 127 must not turn a scan into a failure."""
        workdir = tmp_path / "wd"
        workdir.mkdir()
        record = tmp_path / "nice.txt"
        shim = _shim_dir(tmp_path, "gitleaks", record, GITLEAKS)
        broken = shim / "nice"
        broken.write_text("#!/bin/sh\nexit 127\n", encoding="utf-8")
        broken.chmod(0o755)

        proc = _run_step(
            _secrets_step_run(str(workdir)), workdir, path=f"{shim}:{os.environ['PATH']}"
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert _recorded_nice(record) == {str(_base_nice())}  # unprefixed, still ran
        assert proc.stdout.count("scanners: nice unavailable") == 1
        assert "did not run" in proc.stdout
        assert "scanners: nice=nice -n 10" not in proc.stdout

    def test_nice_absent_from_path_still_runs_the_scan(self, tmp_path: Path) -> None:
        workdir = tmp_path / "wd"
        workdir.mkdir()
        record = tmp_path / "nice.txt"
        shim = _shim_dir(tmp_path, "gitleaks", record, GITLEAKS)
        # Only the tools the step itself needs: no `nice` anywhere on PATH.
        for tool in ("mktemp", "cat", "rm"):
            resolved = shutil.which(tool)
            assert resolved, f"{tool} missing — cannot build the restricted PATH"
            (shim / tool).symlink_to(resolved)
        assert shutil.which("nice", path=str(shim)) is None

        proc = _run_step(_secrets_step_run(str(workdir)), workdir, path=str(shim))
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert _recorded_nice(record) == {str(_base_nice())}
        assert proc.stdout.count("scanners: nice unavailable") == 1

    def test_guard_lane_reports_the_unavailable_reason(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The argv path's fail-open: no crash, unprefixed spawn, honest note."""
        repo = _scratch_repo(tmp_path, {"mod.py": "x = 1\n"}, {"mod.py": "y = 2\n"})
        empty = tmp_path / "no-nice-here"
        empty.mkdir()
        # `nice` vanishes from PATH; the staged scope still resolves (git is
        # found through the rest of PATH).
        monkeypatch.setenv("PATH", f"{empty}:{os.environ['PATH']}")
        monkeypatch.setattr(scanner_nice.shutil, "which", _which_without_nice)
        manager = GuardManager(repo, {"guards": {"secrets": False}})
        assert manager._nice.level == 10
        assert manager._nice.applied is False
        assert manager._nice.note.startswith("scanners: nice unavailable")


def _which_without_nice(cmd: list[str], path: str = None) -> object:
    """``shutil.which`` with ``nice`` invisible — an absent-binary simulation."""
    if cmd == "nice":
        return None
    return _REAL_WHICH(cmd, path=path)


# ── AC3: verdict semantics unchanged at every level ─────────────────────────
# The planted token is assembled at run time so this file never contains a
# literal secret for the repo's own secrets lane to find.
PLANTED_SECRET = "ghp_" + "A1b2C3d4" * 5


class TestVerdictSemantics:
    @pytest.mark.parametrize("level", [10, 19])
    def test_clean_tree_passes(self, tmp_path: Path, level: str) -> None:
        workdir = tmp_path / "wd"
        workdir.mkdir()
        (workdir / "module.py").write_text("x = 1\n", encoding="utf-8")
        proc = _run_step(
            _secrets_step_run(str(workdir), {"guards": {"scanner_nice": level}}), workdir
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        if GITLEAKS:
            assert f"scanners: nice=nice -n {level}" in proc.stdout
        else:
            # No gitleaks on PATH (CI runners): the gitleaks half is skipped,
            # so no priority was ever applied — claiming one would be a lie.
            assert "gitleaks: not on PATH" in proc.stdout

    @pytest.mark.parametrize("level", [10, 19])
    def test_planted_secret_still_fails(self, tmp_path: Path, level: str) -> None:
        workdir = tmp_path / "wd"
        workdir.mkdir()
        (workdir / "settings.py").write_text(f'TOKEN = "{PLANTED_SECRET}"\n', encoding="utf-8")
        proc = _run_step(
            _secrets_step_run(str(workdir), {"guards": {"scanner_nice": level}}), workdir
        )
        assert proc.returncode != 0, proc.stdout + proc.stderr
        if GITLEAKS:
            assert f"scanners: nice=nice -n {level}" in proc.stdout
        else:
            assert "gitleaks: not on PATH" in proc.stdout
        assert "builtin cross-check status: 1 finding" in proc.stdout

    def test_planted_secret_fails_at_level_zero_too(self, tmp_path: Path) -> None:
        """The control: the verdict does not depend on the knob at all."""
        workdir = tmp_path / "wd"
        workdir.mkdir()
        (workdir / "settings.py").write_text(f'TOKEN = "{PLANTED_SECRET}"\n', encoding="utf-8")
        proc = _run_step(_secrets_step_run(str(workdir), {"guards": {"scanner_nice": 0}}), workdir)
        assert proc.returncode != 0


# ── the guard lanes (item 6) ────────────────────────────────────────────────


class TestGuardLanes:
    def test_lint_lane_spawns_ruff_behind_the_prefix(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = _scratch_repo(tmp_path, {"mod.py": "x = 1\n"}, {"mod.py": "y = 2\n"})
        record = tmp_path / "ruff.nice"
        shim = _shim_dir(tmp_path, "ruff", record, RUFF)
        monkeypatch.setenv("PATH", f"{shim}:{os.environ['PATH']}")
        manager = GuardManager(repo, {"guards": {"secrets": False}})
        result = manager._check_lint()
        assert result.passed is True, result.output
        assert _recorded_nice(record) == {str(_expected_child_nice(10))}
        assert result.nice_note == "scanners: nice=nice -n 10"

    def test_secrets_lane_spawns_gitleaks_behind_the_prefix(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = _scratch_repo(tmp_path, {"mod.py": "x = 1\n"}, {"mod.py": "y = 2\n"})
        record = tmp_path / "gitleaks.nice"
        shim = _shim_dir(tmp_path, "gitleaks", record, GITLEAKS)
        monkeypatch.setenv("PATH", f"{shim}:{os.environ['PATH']}")
        manager = GuardManager(repo, {"guards": {"lint": False, "tests": False}})
        result = manager._check_secrets()
        assert result.passed is True, result.output
        assert _recorded_nice(record) == {str(_expected_child_nice(10))}
        assert result.nice_note == "scanners: nice=nice -n 10"

    def test_lint_lane_knob_off_means_no_prefix(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = _scratch_repo(tmp_path, {"mod.py": "x = 1\n"}, {"mod.py": "y = 2\n"})
        record = tmp_path / "ruff.nice"
        shim = _shim_dir(tmp_path, "ruff", record, RUFF)
        monkeypatch.setenv("PATH", f"{shim}:{os.environ['PATH']}")
        manager = GuardManager(repo, {"guards": {"secrets": False, "scanner_nice": 0}})
        result = manager._check_lint()
        assert result.passed is True, result.output
        assert _recorded_nice(record) == {str(_base_nice())}
        assert result.nice_note == ""

    def test_missing_linter_still_skips_instead_of_failing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The invariant a naive prefix would break: `nice` hides a missing tool.

        With no linter on PATH the lane must still report TRUST-001's SKIP — a
        prefixed argv would have exited 127 with output and read as a FAIL.
        """
        repo = _scratch_repo(tmp_path, {"mod.py": "x = 1\n"}, {"mod.py": "y = 2\n"})
        git_only = tmp_path / "git-only"
        git_only.mkdir()
        (git_only / "git").symlink_to(shutil.which("git"))
        monkeypatch.setenv("PATH", str(git_only))
        manager = GuardManager(repo, {"guards": {"secrets": False}})
        result = manager._check_lint()
        assert result.skipped is True
        assert result.passed is True
        assert result.nice_note == ""
        assert "linter" in result.skip_reason

    def test_tier1_summary_names_the_prefix(self) -> None:
        from engine.types import GuardResult, Tier1Result

        summary = Tier1Result(
            passed=True,
            results=[
                GuardResult(
                    name="secrets",
                    passed=True,
                    output="gitleaks: clean",
                    nice_note="scanners: nice=nice -n 10",
                )
            ],
        ).summary
        assert "scanners: nice=nice -n 10" in summary
        assert summary.count("\n") == 0  # one line per step, still

    def test_run_log_records_the_prefix(self, tmp_path: Path) -> None:
        from engine.types import GuardResult, Tier1Result

        from engine.guard_manager import _guard_log_content

        content = _guard_log_content(
            str(tmp_path),
            Tier1Result(
                passed=True,
                results=[
                    GuardResult(
                        name="lint",
                        passed=True,
                        output="ruff: clean",
                        nice_note="scanners: nice=nice -n 10",
                    )
                ],
            ),
        )
        assert "scanner_nice: scanners: nice=nice -n 10" in content
