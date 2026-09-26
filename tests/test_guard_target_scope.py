"""DF-GITREINS-POC-67 — the diff-mode scope banner must tell the truth.

The dogfood run (2026-09-25c, F2) caught a self-contradicting pre-commit
output: the banner printed ``full suite — safety trigger`` while the very
next line read ``~ tests — skipped (no test files match the changed sources
(diff mode))``. Mechanism: ``_discover_test_targets`` returns ``None`` for
the REAL full-suite fallback (no changes, or a force-full file changed) but
``[]`` when the changed sources map to zero test files, and
``run_all`` collapsed both into ``extra["test_targets"] = None``. The CLI
banner and the run log's ``_log_test_scope`` both rendered that shared
``None`` as "full suite (safety trigger)", so anyone grepping logs for
full-suite coverage over-counted.

Contract pinned here:

- A diff-mode run whose tests lane matched zero test files reports the skip
  ("no test files matched — diff mode skipped") in the banner AND the run
  log — never "full suite — safety trigger".
- A genuine full-suite safety-trigger fallback (no mapped files possible:
  a force-full glob changed) still reports "full suite — safety trigger" in
  both surfaces.
- The two states are distinguished by ``extra["test_scope"]`` (a separate
  extra key; the legacy ``test_targets`` count/None shape is preserved for
  existing consumers), and ``_log_test_scope`` renders each state
  truthfully.
- A clean tree (empty change set) keeps its lane-level skip and never
  claims the full suite either.
"""

import os

from engine.guard_manager import (
    GuardManager,
    _discover_test_targets,
    _log_test_scope,
    newest_guard_log,
    write_guard_log,
)


def _repo(tmp_path, name="repo"):
    """A git scratch repo with one committed source file and its test."""
    import subprocess

    workdir = tmp_path / name
    workdir.mkdir()
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    subprocess.run(["git", "init", "-q"], cwd=workdir, capture_output=True, check=True, env=env)
    subprocess.run(
        ["git", "config", "user.email", "t@example.invalid"],
        cwd=workdir,
        capture_output=True,
        check=True,
        env=env,
    )
    subprocess.run(
        ["git", "config", "user.name", "T"],
        cwd=workdir,
        capture_output=True,
        check=True,
        env=env,
    )
    return str(workdir)


def _stage(workdir, relpath, content):
    import subprocess

    full = os.path.join(workdir, relpath)
    os.makedirs(os.path.dirname(full) or workdir, exist_ok=True)
    with open(full, "w") as f:
        f.write(content)
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    subprocess.run(["git", "add", relpath], cwd=workdir, capture_output=True, check=True, env=env)


def _guards(**overrides):
    guards = {"test_command": "echo ok", "lint": False, "test_mode": "diff"}
    guards.update(overrides)
    return {"guards": guards}


def _run_cli(*args, cwd):
    """Run the real CLI (same pattern as tests/test_guard_degraded.py)."""
    import subprocess
    import sys

    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = os.environ.copy()
    env["PYTHONPATH"] = project_root + (":" + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    cmd = [sys.executable, os.path.join(project_root, "gitreins", "cli.py")] + list(args)
    return subprocess.run(cmd, capture_output=True, text=True, timeout=60, cwd=cwd, env=env)


def _read_run_log(workdir) -> str:
    log = newest_guard_log(workdir)
    assert log, "the run must persist a guard log (DF-018)"
    with open(log) as f:
        return f.read()


class TestDiscoverTestTargetsStates:
    """The three discovery states the sentinel must distinguish."""

    def test_no_mapping_returns_empty_list_not_none(self, tmp_path):
        """Changed sources that map to no test file → [] (skip), not None."""
        workdir = _repo(tmp_path)
        _stage(workdir, "pkg.py", "x = 1\n")
        assert _discover_test_targets(workdir) == []

    def test_force_full_file_returns_none(self, tmp_path):
        """A force-full glob (conftest.py) changed → None (real fallback)."""
        workdir = _repo(tmp_path)
        _stage(workdir, "conftest.py", "")
        assert _discover_test_targets(workdir) is None

    def test_mapped_test_file_returned(self, tmp_path):
        workdir = _repo(tmp_path)
        _stage(workdir, "pkg.py", "x = 1\n")
        _stage(workdir, "tests/test_pkg.py", "def test_x():\n    pass\n")
        targets = _discover_test_targets(workdir)
        assert targets and targets[0].endswith("test_pkg.py")


def _tests_result(result):
    """The tests-lane GuardResult (names: 'tests', 'tests (full)', 'tests (diff: N files)')."""
    return next(r for r in result.results if r.name == "tests" or r.name.startswith("tests ("))


class TestRunAllTestScopeSentinel:
    """AC 3: the two None-lookalike states get distinct extra keys."""

    def test_no_match_sets_no_match_scope(self, tmp_path):
        """Zero-mapped changed sources → test_scope 'no-match', tests skip."""
        workdir = _repo(tmp_path)
        _stage(workdir, "pkg.py", "x = 1\n")
        result = GuardManager(workdir, config=_guards()).run_all()

        tests = _tests_result(result)
        assert tests.skipped is True
        assert "no test files match" in (tests.skip_reason or "")
        assert result.extra["test_scope"] == "no-match"

    def test_force_full_sets_full_fallback_scope(self, tmp_path):
        """conftest.py staged → real safety-trigger fallback, distinct scope."""
        workdir = _repo(tmp_path)
        _stage(workdir, "conftest.py", "")
        result = GuardManager(workdir, config=_guards()).run_all()

        tests = _tests_result(result)
        assert tests.skipped is False, "the safety trigger runs the full command"
        assert result.extra["test_scope"] == "full-fallback"
        assert result.extra["test_targets"] is None

    def test_clean_tree_gets_no_changes_scope(self, tmp_path):
        """Empty change set: the lane-level skip stands; the scope key marks
        ``no-changes`` so neither surface claims the full suite."""
        workdir = _repo(tmp_path)
        result = GuardManager(workdir, config=_guards()).run_all()

        tests = next(r for r in result.results if r.name == "tests")
        assert tests.skipped is True
        assert tests.skip_reason == "no staged files"
        assert result.extra["test_scope"] == "no-changes"
        assert result.extra["test_targets"] is None

    def test_narrowed_run_gets_no_scope_key(self, tmp_path):
        """A normal diff run reports the count, not a sentinel scope."""
        workdir = _repo(tmp_path)
        _stage(workdir, "pkg.py", "x = 1\n")
        _stage(workdir, "tests/test_pkg.py", "def test_x():\n    pass\n")
        result = GuardManager(workdir, config=_guards()).run_all()

        assert "test_scope" not in result.extra
        assert result.extra["test_targets"] == 1

    def test_full_mode_gets_no_scope_key(self, tmp_path):
        """full mode never enters the sentinel contract."""
        workdir = _repo(tmp_path)
        _stage(workdir, "pkg.py", "x = 1\n")
        result = GuardManager(workdir, config=_guards(test_mode="full")).run_all()

        assert "test_scope" not in result.extra
        assert "test_targets" not in result.extra


class TestLogTestScopeRenders:
    """AC 3: _log_test_scope renders each state truthfully."""

    def test_no_match_scope(self):
        assert _log_test_scope({"test_mode": "diff", "test_scope": "no-match"}) == (
            "no test files matched (diff mode skipped)"
        )

    def test_full_fallback_scope(self):
        assert _log_test_scope({"test_mode": "diff", "test_scope": "full-fallback"}) == (
            "full suite (safety trigger)"
        )

    def test_legacy_none_still_renders_full_suite(self):
        """Old-shape extras (count/None, no test_scope) render as before."""
        assert _log_test_scope({"test_mode": "diff", "test_targets": None}) == (
            "full suite (safety trigger)"
        )
        assert _log_test_scope({"test_mode": "diff", "test_targets": 3}) == "3 file(s)"

    def test_no_targets_key_unchanged(self):
        assert _log_test_scope({"test_mode": "full"}) == "all (full mode)"
        assert _log_test_scope({"test_mode": "diff"}) == "unknown"


class TestBannerAndRunLogTruth:
    """AC 1 + 2: banner and run log never contradict the tests lane."""

    def test_no_match_banner_names_the_skip_never_full_suite(self, tmp_path):
        """The F2 contradiction, pinned: staged source with no mapped tests."""
        workdir = _repo(tmp_path)
        _stage(workdir, "pkg.py", "x = 1\n")
        _write_allow_skips(workdir)

        result = _run_cli("guard", cwd=workdir)

        assert result.returncode == 0, f"stdout={result.stdout} stderr={result.stderr}"
        assert "full suite" not in result.stdout, result.stdout
        assert "no test files matched — diff mode skipped" in result.stdout
        assert "~ tests — skipped (no test files match the changed sources (diff mode))" in (
            result.stdout
        )

    def test_no_match_run_log_never_claims_full_suite(self, tmp_path):
        workdir = _repo(tmp_path)
        _stage(workdir, "pkg.py", "x = 1\n")
        _write_allow_skips(workdir)
        _run_cli("guard", cwd=workdir)

        content = _read_run_log(workdir)
        assert "full suite" not in content
        assert "test_targets: no test files matched (diff mode skipped)" in content

    def test_force_full_banner_and_log_still_report_safety_trigger(self, tmp_path):
        """AC 2: the REAL fallback keeps its wording on both surfaces."""
        workdir = _repo(tmp_path)
        _stage(workdir, "conftest.py", "")
        _write_allow_skips(workdir)

        result = _run_cli("guard", cwd=workdir)

        assert result.returncode == 0, f"stdout={result.stdout} stderr={result.stderr}"
        assert "full suite — safety trigger" in result.stdout
        log_content = _read_run_log(workdir)
        assert "test_targets: full suite (safety trigger)" in log_content

    def test_clean_tree_banner_never_claims_full_suite(self, tmp_path):
        """The empty-stage `gitreins commit` hook run: same false claim, gone."""
        workdir = _repo(tmp_path)
        _write_allow_skips(workdir)

        result = _run_cli("guard", cwd=workdir)

        assert result.returncode == 0, f"stdout={result.stdout} stderr={result.stderr}"
        assert "full suite" not in result.stdout, result.stdout
        assert "Tier 1: DEGRADED PASS" in result.stdout
        assert "~ tests — skipped (no staged files)" in result.stdout


def _write_allow_skips(workdir):
    """allow_skips: true so the CLI exits 0 on these skip-shaped runs."""
    import yaml

    cfg_dir = os.path.join(workdir, ".gitreins")
    os.makedirs(cfg_dir, exist_ok=True)
    with open(os.path.join(cfg_dir, "config.yaml"), "w") as f:
        yaml.safe_dump(
            {
                "guards": {
                    "test_command": "echo ok",
                    "test_mode": "diff",
                    "lint": False,
                    "allow_skips": True,
                }
            },
            f,
        )


class TestRunLogRenderingOnResult:
    """The run-log header (write_guard_log path) carries the same truth."""

    def test_write_guard_log_renders_no_match(self, tmp_path):
        workdir = _repo(tmp_path)
        _stage(workdir, "pkg.py", "x = 1\n")
        result = GuardManager(workdir, config=_guards()).run_all()

        write_guard_log(workdir, result)
        content = _read_run_log(workdir)

        assert "test_targets: no test files matched (diff mode skipped)" in content
        assert "full suite" not in content

    def test_write_guard_log_renders_full_fallback(self, tmp_path):
        workdir = _repo(tmp_path)
        _stage(workdir, "conftest.py", "")
        result = GuardManager(workdir, config=_guards()).run_all()

        write_guard_log(workdir, result)
        content = _read_run_log(workdir)

        assert "test_targets: full suite (safety trigger)" in content
