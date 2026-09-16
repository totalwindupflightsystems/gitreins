"""
DF-018: guard run logs — persist the complete, untruncated guard output.

The guard console summary is deliberately bounded (`_bound_step_evidence` in
engine/pipeline.py, `MAX_STEP_EVIDENCE_CHARS`, and the tail-only slice in
`GuardManager._run_test_command`), so before this change the full pytest
traceback of a failed guard was unrecoverable: the only way to learn what
broke was to re-run pytest by hand.

These tests pin the whole contract: one timestamped log per run, the FULL
guard output (nothing the bounded console summary drops), per-guard
name/passed/exit_code, a UTC timestamp, retention pruning, the single-log
size cap, and the best-effort non-fatal failure path (never raises, never
changes the verdict). No network, no sleeps, and no reliance on the real
repo's git state.
"""

import os
import re
import shlex
import subprocess
import sys

import yaml

from engine import guard_manager
from engine.guard_manager import (
    GuardManager,
    guard_log_dir,
    newest_guard_log,
)
from engine.types import GuardResult

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLI_SCRIPT = os.path.join(PROJECT_ROOT, "gitreins", "cli.py")
SCRIPT_NAME = "probe_tests.py"

# Distinctive strings so a failure says exactly which part went missing.
EARLY_MARKER = "DF018-EARLY-HEAD-MARKER"
MID_MARKER = "DF018-MID-TRACEBACK-MARKER"
PASS_MARKER = "DF018-PASSING-OUTPUT"
LAST_FRAME = "traceback frame {last}"
FAILED_LINE = "FAILED tests/test_probe.py::test_boom - AssertionError: " + ("y" * 120)
_LOG_NAME_RE = re.compile(r"^guard-\d{8}T\d{6}\.\d{6}Z\.log$")
_TIMESTAMP_RE = re.compile(r"^run_utc: \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", re.MULTILINE)


# ── Helpers ───────────────────────────────────────────────────────


def _failing_script(padding: int = 300) -> str:
    """A stand-in test command: long multi-line traceback, non-zero exit."""
    lines = [
        "import sys",
        "print('=== session starts ===')",
        f"print({EARLY_MARKER!r})",
        f"for i in range({padding}):",
        "    print('traceback frame %d: %s' % (i, 'x' * 40))",
        f"print({MID_MARKER!r})",
        f"print({FAILED_LINE!r})",
        "print('1 failed, 0 passed in 0.01s')",
        "sys.exit(1)",
    ]
    return "\n".join(lines) + "\n"


def _passing_script() -> str:
    """A stand-in test command that passes."""
    return f"import sys\nprint({PASS_MARKER!r})\nprint('3 passed in 0.01s')\nsys.exit(0)\n"


def _probe_workdir(tmp_path, script: str | None = None) -> str:
    """A scratch workdir holding (optionally) the probe test command."""
    workdir = tmp_path / "probe"
    workdir.mkdir()
    if script is not None:
        (workdir / SCRIPT_NAME).write_text(script)
    return str(workdir)


def _guard_config(test_command: str, **overrides) -> dict:
    """Guards config running exactly one hermetic test command."""
    guards = {
        "secrets": False,
        "lint": False,
        "tests": True,
        "test_mode": "full",
        "test_on_clean": True,
        "test_command": test_command,
        "test_timeout": 60,
    }
    guards.update(overrides)
    return {"guards": guards}


def _manager(workdir: str, **overrides) -> GuardManager:
    cmd = f"{shlex.quote(sys.executable)} {SCRIPT_NAME}"
    return GuardManager(workdir, config=_guard_config(cmd, **overrides))


def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def _git_env() -> dict:
    """Environment without leaked GIT_* vars (DF-008).

    The guard strips GIT_INDEX_FILE/GIT_DIR before running the test command,
    but these tests can also run inside a PRE-COMMIT hook: inheriting the
    outer repo's GIT_* would make `git init`/`git add` in a scratch dir (and
    the CLI's `git rev-parse --show-toplevel`) operate on the wrong repo.
    """
    return {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}


def _git(workdir: str, *args: str) -> None:
    subprocess.run(["git", *args], cwd=workdir, capture_output=True, check=True, env=_git_env())


def _block_random_log_dir(log_dir: str) -> None:
    """Make *log_dir* uncreatable for ANY user, root included.

    A regular file where the directory must be makes ``os.makedirs`` fail
    with FileExistsError regardless of privileges — a chmod-based
    "unwritable" fixture passes silently when the tests run as root.
    """
    os.makedirs(os.path.dirname(log_dir), exist_ok=True)
    with open(log_dir, "w") as f:
        f.write("not a directory\n")


def _init_repo(workdir: str, config: dict) -> None:
    """Minimal real git repo with a GitReins config and one staged file."""
    _git(workdir, "init", "-q")
    _git(workdir, "config", "user.email", "probe@test.local")
    _git(workdir, "config", "user.name", "Probe")
    os.makedirs(os.path.join(workdir, ".gitreins"), exist_ok=True)
    with open(os.path.join(workdir, ".gitreins", "config.yaml"), "w") as f:
        yaml.safe_dump(config, f, sort_keys=False)
    with open(os.path.join(workdir, "README.md"), "w") as f:
        f.write("# probe\n")
    _git(workdir, "add", "README.md")


def _run_guard_cli(workdir: str) -> subprocess.CompletedProcess:
    env = _git_env()
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = PROJECT_ROOT + (":" + existing if existing else "")
    return subprocess.run(
        [sys.executable, CLI_SCRIPT, "guard"],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=workdir,
        env=env,
    )


# ── Full output persistence ──────────────────────────────────────


class TestLogPersistence:
    def test_failing_run_logs_the_full_untruncated_output(self, tmp_path):
        """The log carries what the bounded console summary cannot.

        The failing output is far longer than the 2000-char tail the
        GuardResult keeps, so the early head marker only survives if the
        untruncated output is what gets persisted.
        """
        workdir = _probe_workdir(tmp_path, _failing_script())
        result = _manager(workdir).run_all()

        assert result.passed is False
        log_path = result.extra["guard_log"]
        assert os.path.isfile(log_path)
        assert os.path.dirname(log_path) == guard_log_dir(workdir)
        assert _LOG_NAME_RE.match(os.path.basename(log_path)), os.path.basename(log_path)

        content = _read(log_path)
        assert EARLY_MARKER in content, "head of the output was dropped"
        assert MID_MARKER in content
        assert LAST_FRAME.format(last=299) in content, "tail of the output was dropped"
        assert FAILED_LINE in content, "FAILED line was truncated"
        assert "1 failed, 0 passed" in content

        # ...while the console summary stays bounded (the reason the log exists)
        assert EARLY_MARKER not in result.summary
        assert FAILED_LINE not in result.summary
        assert "1 failure(s)" in result.summary

        # The guard result itself is unchanged: bounded, failing, exit code kept.
        tests_result = result.results[0]
        assert tests_result.name == "tests (full)"
        assert tests_result.passed is False
        assert tests_result.exit_code == 1
        assert len(tests_result.output) <= 2000

    def test_log_records_per_guard_metadata_and_utc_timestamp(self, tmp_path):
        workdir = _probe_workdir(tmp_path, _failing_script())
        result = _manager(workdir).run_all()

        content = _read(result.extra["guard_log"])

        assert _TIMESTAMP_RE.search(content), "no UTC run timestamp in the header"
        assert f"workdir: {os.path.abspath(workdir)}" in content
        assert "test_mode: full" in content
        assert "test_targets: all (full mode)" in content
        assert "overall: FAIL" in content
        assert "guards: 1 (1 failed, 0 skipped)" in content
        # Per-guard: name, passed, exit_code.
        assert "[FAIL] tests (full)  passed=false  exit_code=1" in content

    def test_passing_run_is_logged_with_a_pass_verdict(self, tmp_path):
        workdir = _probe_workdir(tmp_path, _passing_script())
        result = _manager(workdir).run_all()

        assert result.passed is True
        content = _read(result.extra["guard_log"])
        assert "overall: PASS" in content
        assert "guards: 1 (0 failed, 0 skipped)" in content
        assert "[PASS] tests (full)  passed=true  exit_code=0" in content
        assert PASS_MARKER in content

    def test_failures_are_listed_before_passes(self, tmp_path, monkeypatch):
        workdir = _probe_workdir(tmp_path, _failing_script())
        gm = _manager(workdir, secrets=True)
        monkeypatch.setattr(
            gm,
            "_check_secrets",
            lambda: GuardResult("secrets", True, "gitleaks: clean", exit_code=0),
        )
        result = gm.run_all()

        assert result.passed is False
        content = _read(result.extra["guard_log"])
        assert "guards: 2 (1 failed, 0 skipped)" in content
        assert content.index("[FAIL] tests (full)") < content.index("[PASS] secrets")

    def test_skipped_steps_are_named_in_the_log(self, tmp_path):
        """TRUST-001: the log keeps the skip list and a DEGRADED overall line."""
        workdir = _probe_workdir(tmp_path, _passing_script())
        gm = _manager(workdir, secrets=True)
        gm._check_tests = lambda: GuardResult(  # type: ignore[method-assign]
            "tests", True, "No files staged — skipped", skipped=True, skip_reason="no staged files"
        )
        result = gm.run_all()

        content = _read(result.extra["guard_log"])
        assert "overall: PASS (DEGRADED — skipped checks)" in content
        assert "guards: 2 (0 failed, 1 skipped)" in content
        assert "skipped_steps:" in content
        assert "  - tests: no staged files" in content
        assert "[SKIP] tests" in content and "skip_reason=no staged files" in content

    def test_newest_log_path_comes_from_the_accessor(self, tmp_path):
        workdir = _probe_workdir(tmp_path, _passing_script())
        result = _manager(workdir).run_all()

        assert newest_guard_log(workdir) == result.extra["guard_log"]

    def test_diagnostics_block_names_the_failing_test_and_the_scanners(self, tmp_path, monkeypatch):
        """TRUST-003 (AC3): both console facts are persisted in the run log.

        The console line is bounded; the log is the post-mortem artifact, so
        the first failing test id and the secrets scanner attribution must be
        readable there without re-parsing the untruncated bodies below.
        """
        workdir = _probe_workdir(tmp_path, _failing_script())
        gm = _manager(workdir, secrets=True)
        monkeypatch.setattr(
            gm,
            "_check_secrets",
            lambda: GuardResult(
                "secrets",
                True,
                "gitleaks: clean",
                scanners=(("gitleaks", "clean"), ("builtin", "clean")),
            ),
        )

        result = gm.run_all()

        content = _read(result.extra["guard_log"])
        assert "diagnostics:" in content
        assert (
            "  first_failing_test: tests/test_probe.py::test_boom  (from tests (full))" in content
        )
        assert "  secrets_scanners: clean (gitleaks + builtin cross-check)" in content

    def test_diagnostics_log_a_finding_scanner_with_its_count(self, tmp_path, monkeypatch):
        """A secrets FAIL logs WHICH scanner found what — the POC-15 ambiguity."""
        workdir = _probe_workdir(tmp_path, _passing_script())
        gm = _manager(workdir, secrets=True)
        monkeypatch.setattr(
            gm,
            "_check_secrets",
            lambda: GuardResult(
                "secrets",
                False,
                'Potential secrets found:\n.env:1: [AWS access key] AWS_ACCESS_KEY_ID="***"',
                scanners=(("gitleaks", "clean"), ("builtin", "2 findings")),
            ),
        )

        result = gm.run_all()

        content = _read(result.extra["guard_log"])
        assert (
            "  secrets_scanners: FAIL (builtin cross-check: 2 findings; gitleaks: clean)" in content
        )

    def test_diagnostics_say_none_detected_when_there_is_nothing_to_name(self, tmp_path):
        """A clean run is explicit, never a silently missing diagnostic."""
        workdir = _probe_workdir(tmp_path, _passing_script())
        result = _manager(workdir).run_all()

        content = _read(result.extra["guard_log"])
        assert "  first_failing_test: none detected" in content
        assert "  secrets_scanners: none ran" in content


# ── Retention and size cap ───────────────────────────────────────


class TestRetentionAndSizeCap:
    def test_retention_keeps_only_the_newest_n(self, tmp_path, monkeypatch):
        monkeypatch.setattr(guard_manager, "GUARD_LOG_KEEP", 5)
        workdir = _probe_workdir(tmp_path, _passing_script())
        log_dir = guard_log_dir(workdir)
        os.makedirs(log_dir)
        stale = [f"guard-2020010{i:02d}T000000.000000Z.log" for i in range(12)]
        for name in stale:
            with open(os.path.join(log_dir, name), "w") as f:
                f.write("stale\n")

        result = _manager(workdir).run_all()

        remaining = sorted(os.listdir(log_dir))
        assert len(remaining) == 5, remaining
        assert stale[0] not in remaining and stale[7] not in remaining
        assert stale[11] in remaining, "pruning removed a newer log than the cap allows"
        new_name = os.path.basename(result.extra["guard_log"])
        assert new_name in remaining
        assert newest_guard_log(workdir) == result.extra["guard_log"]

    def test_size_cap_markers_a_pathological_log(self, tmp_path, monkeypatch):
        monkeypatch.setattr(guard_manager, "GUARD_LOG_MAX_BYTES", 1024)
        workdir = _probe_workdir(tmp_path, _failing_script(padding=2000))

        result = _manager(workdir).run_all()

        log_path = result.extra["guard_log"]
        assert os.path.getsize(log_path) <= 1024
        content = _read(log_path)
        assert "... [log truncated at 1024 bytes]" in content
        # Truncation is a cap on the FILE, not on the verdict.
        assert result.passed is False


# ── Best-effort, non-fatal failure path ──────────────────────────


class TestPersistenceFailuresAreNonFatal:
    def test_unwritable_target_does_not_raise_or_change_a_passing_verdict(self, tmp_path):
        workdir = _probe_workdir(tmp_path, _passing_script())
        _block_random_log_dir(guard_log_dir(workdir))

        result = _manager(workdir).run_all()  # must not raise

        assert result.passed is True
        assert "guard_log" not in result.extra
        assert result.extra["guard_log_error"], "failure reason must be surfaced"
        assert "logs" in result.extra["guard_log_error"]

    def test_unwritable_target_does_not_change_a_failing_verdict(self, tmp_path):
        workdir = _probe_workdir(tmp_path, _failing_script())
        _block_random_log_dir(guard_log_dir(workdir))

        result = _manager(workdir).run_all()

        assert result.passed is False
        assert "guard_log" not in result.extra
        assert result.extra["guard_log_error"]

    def test_non_process_guards_log_exit_code_na(self, tmp_path, monkeypatch):
        """A guard that ran no single subprocess is honest about the code."""
        workdir = _probe_workdir(tmp_path, _passing_script())
        gm = _manager(workdir, lsp=True)
        monkeypatch.setattr(gm, "_check_lsp", lambda: GuardResult("lsp", True, "pylsp — clean"))
        result = gm.run_all()

        content = _read(result.extra["guard_log"])
        assert "[PASS] tests (full)  passed=true  exit_code=0" in content
        assert "[PASS] lsp  passed=true  exit_code=n/a" in content


# ── Accessor semantics ───────────────────────────────────────────


class TestNewestGuardLogAccessor:
    def test_none_when_no_log_has_been_written(self, tmp_path):
        assert newest_guard_log(str(tmp_path)) is None

    def test_returns_the_chronologically_newest(self, tmp_path):
        log_dir = guard_log_dir(str(tmp_path))
        os.makedirs(log_dir)
        names = [
            "guard-20200101T000000.000000Z.log",
            "guard-20250101T000000.000000Z.log",
            "guard-20230101T000000.000000Z.log",
        ]
        for name in names:
            with open(os.path.join(log_dir, name), "w") as f:
                f.write("x\n")

        assert newest_guard_log(str(tmp_path)) == os.path.join(log_dir, names[1])


# ── Callers cite the path (pipeline + CLI) ───────────────────────


class TestCallersCiteTheLog:
    def test_tier1_pipeline_step_data_points_at_the_log(self, tmp_path):
        from engine.pipeline import Pipeline

        workdir = _probe_workdir(tmp_path, _passing_script())
        result = _manager(workdir).run_all()
        log_path = result.extra["guard_log"]

        config = {
            "pipeline": {
                "stages": [
                    {
                        "id": "tier1",
                        "parallel": True,
                        "on": ["pre-eval"],
                        "steps": [{"id": "tests", "type": "script", "run": "true"}],
                    },
                    {
                        "id": "tier2",
                        "parallel": False,
                        "on": ["pre-eval"],
                        "steps": [{"id": "other", "type": "script", "run": "true"}],
                    },
                ]
            }
        }
        out = Pipeline(config, workdir).run({"id": "t1", "criteria": []}, trigger="pre-eval")

        tier1_step = out["stages"]["tier1"]["steps"][0]
        assert tier1_step["data"]["guard_log"] == log_path
        assert os.path.isfile(tier1_step["data"]["guard_log"])
        # Only the tier-1 stage carries the raw guard evidence.
        assert "guard_log" not in out["stages"]["tier2"]["steps"][0]["data"]

    def test_tier1_step_without_a_log_carries_no_path(self, tmp_path):
        from engine.pipeline import Pipeline

        workdir = _probe_workdir(tmp_path)  # no guard run → no log

        config = {
            "pipeline": {
                "stages": [
                    {
                        "id": "tier1",
                        "on": ["pre-eval"],
                        "steps": [{"id": "tests", "type": "script", "run": "true"}],
                    }
                ]
            }
        }
        out = Pipeline(config, workdir).run({"id": "t1", "criteria": []}, trigger="pre-eval")

        assert "guard_log" not in out["stages"]["tier1"]["steps"][0]["data"]

    def test_cli_names_the_log_on_failure(self, tmp_path):
        workdir = str(tmp_path / "repo")
        os.makedirs(workdir)
        command = f"{shlex.quote(sys.executable)} {SCRIPT_NAME}"
        config = _guard_config(command)
        config["defaults"] = {"check_for_updates": False}
        _init_repo(workdir, config)
        with open(os.path.join(workdir, SCRIPT_NAME), "w") as f:
            f.write(_failing_script())

        out = _run_guard_cli(workdir)

        assert out.returncode == 1, out.stdout + out.stderr
        assert "Tier 1 Guards: FAIL" in out.stdout
        match = re.search(r"guard log: (\S+)", out.stdout)
        assert match, f"no guard log line in output:\n{out.stdout}"
        log_path = match.group(1)
        assert os.path.dirname(log_path) == guard_log_dir(workdir)
        assert FAILED_LINE in _read(log_path)

    def test_cli_names_the_log_on_success(self, tmp_path):
        workdir = str(tmp_path / "repo")
        os.makedirs(workdir)
        config = _guard_config("echo ok")
        config["defaults"] = {"check_for_updates": False}
        _init_repo(workdir, config)

        out = _run_guard_cli(workdir)

        assert out.returncode == 0, out.stdout + out.stderr
        assert "Tier 1 Guards: PASS" in out.stdout
        match = re.search(r"guard log: (\S+)", out.stdout)
        assert match, f"no guard log line in output:\n{out.stdout}"
        assert os.path.isfile(match.group(1))
        assert "overall: PASS" in _read(match.group(1))

    def test_cli_console_names_the_failing_test_and_the_scanners(self, tmp_path):
        """TRUST-003 end-to-end: the bounded console output the user reads.

        Both facts must reach the CLI's own summary — naming them only in the
        log would leave the dogfood friction (re-run the tools by hand) intact.
        """
        workdir = str(tmp_path / "repo")
        os.makedirs(workdir)
        command = f"{shlex.quote(sys.executable)} {SCRIPT_NAME}"
        config = _guard_config(command, secrets=True)
        config["defaults"] = {"check_for_updates": False}
        _init_repo(workdir, config)
        with open(os.path.join(workdir, SCRIPT_NAME), "w") as f:
            f.write(_failing_script())

        out = _run_guard_cli(workdir)

        assert out.returncode == 1, out.stdout + out.stderr
        assert (
            "FAIL (tests/test_probe.py::test_boom [first failing id]; 1 failure(s))" in out.stdout
        )
        # The scanner name is environment-independent: gitleaks when installed,
        # otherwise the built-in cross-check is named as the one that ran.
        assert re.search(r"secrets — clean \([^)]*cross-check", out.stdout), out.stdout


# ── Runtime artifacts stay out of git ────────────────────────────


class TestRuntimeArtifactsAreIgnored:
    def test_guard_log_directory_is_gitignored(self):
        with open(os.path.join(PROJECT_ROOT, ".gitignore"), "r") as f:
            entries = [line.strip() for line in f.read().splitlines()]

        assert ".gitreins/logs/" in entries

    def test_guard_logs_do_not_block_the_worktree_merge_gate(self, tmp_path, monkeypatch):
        """A run log is GitReins' own runtime artifact, never uncommitted work.

        WorktreeManager.merge() runs a guard INSIDE the task worktree and then
        re-checks ``_is_clean``; a repo without the logs entry in .gitignore
        would otherwise see the log as dirt and hold every fleet merge with
        "Git safety precondition changed before merge".
        """
        from pathlib import Path

        from engine.worktree_manager import WorktreeManager

        # Belt-and-braces DF-008 hardening: a leaked GIT_DIR/GIT_INDEX_FILE
        # (pre-commit hook environment) would make every `git` call here —
        # including worktree_manager's own — inspect the OUTER repo. The
        # guard strips GIT_* before running pytest; do the same in-process.
        for key in [k for k in os.environ if k.startswith("GIT_")]:
            monkeypatch.delenv(key, raising=False)

        workdir = _probe_workdir(tmp_path, _passing_script())
        (Path(workdir) / ".coding-hermes" / "board").mkdir(parents=True)
        for args in (
            ("init", "-q"),
            ("config", "user.name", "Probe"),
            ("config", "user.email", "probe@test.local"),
            ("add", SCRIPT_NAME),
            ("commit", "-qm", "init"),
        ):
            _git(workdir, *args)

        manager = WorktreeManager(workdir)
        assert manager._is_clean(Path(workdir)) is True

        result = _manager(workdir).run_all()
        assert result.extra["guard_log"]

        status = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=all"],
            cwd=workdir,
            capture_output=True,
            text=True,
            check=True,
            env=_git_env(),
        ).stdout
        assert ".gitreins/logs/" in status, "premise: the log IS an untracked artifact"
        assert manager._is_clean(Path(workdir)) is True
