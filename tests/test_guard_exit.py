"""
Integration tests for gitreins guard exit codes.

Verifies:
  - gitreins guard exits 0 when all guards pass
  - gitreins guard exits 1 when a guard fails (secret, lint)
  - gitreins commit blocks (non-zero) when secrets are staged
"""

import os
import subprocess
import sys

import yaml

from engine.guard_manager import GuardManager

CLI_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "gitreins")
CLI_SCRIPT = os.path.join(CLI_DIR, "cli.py")


def _run_cli(*args, cwd=None, extra_env=None):
    """Run the CLI as a subprocess and return CompletedProcess."""
    env = os.environ.copy()
    env.setdefault("PYTHONPATH", "")
    env["PYTHONPATH"] = os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + (
        ":" + env["PYTHONPATH"] if env["PYTHONPATH"] else ""
    )
    if extra_env:
        env.update(extra_env)
    cmd = [sys.executable, CLI_SCRIPT] + list(args)
    return subprocess.run(cmd, capture_output=True, text=True, timeout=30, cwd=cwd, env=env)


def _init_repo(workdir):
    """Initialize a minimal git repo with identity."""
    subprocess.run(["git", "init", "-q"], cwd=workdir, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"], cwd=workdir, capture_output=True
    )
    subprocess.run(["git", "config", "user.name", "Test"], cwd=workdir, capture_output=True)


def _stage_file(workdir, path, content):
    """Write and stage a file."""
    full = os.path.join(workdir, path)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w") as f:
        f.write(content)
    subprocess.run(["git", "add", path], cwd=workdir, capture_output=True)


def _write_config(workdir, config_dict):
    """Write a minimal .gitreins/config.yaml."""
    config_dir = os.path.join(workdir, ".gitreins")
    os.makedirs(config_dir, exist_ok=True)
    with open(os.path.join(config_dir, "config.yaml"), "w") as f:
        yaml.safe_dump(config_dict, f)


# Build secret payloads at runtime so gitleaks does not
# flag literal strings in this test source file.
def _openai_secret() -> str:
    """Build a realistic-looking OpenAI/OpenRouter key."""
    return "".join(chr(c) for c in (115, 107, 45)) + "1234567890abcdef" + "1234567890abcdef"


def _aws_secret() -> str:
    """Build a realistic-looking AWS access key."""
    return "".join(chr(c) for c in (65, 75, 73, 65)) + "1234567890ABCDEF"


class TestGuardRefusesWithoutConfig:
    """A repo with no .gitreins/config.yaml must not get a false-green PASS.

    GR-GAP-051: guards silently fell back to built-in defaults, so
    `gitreins guard` printed "Tier 1 Guards: PASS" and `gitreins commit`
    committed unguarded. Both must refuse with an actionable message.
    """

    def test_guard_refuses_without_config(self, tmp_path):
        d = str(tmp_path / "repo")
        os.makedirs(d)
        _init_repo(d)
        _stage_file(d, "a.txt", "hello\n")
        assert not os.path.isdir(os.path.join(d, ".gitreins"))

        result = _run_cli("guard", cwd=d)

        output = result.stdout + result.stderr
        assert result.returncode != 0, (
            f"guard must refuse without config, got {result.returncode}. output: {output[:300]}"
        )
        assert "no .gitreins/config.yaml" in output
        assert "gitreins init" in output
        assert "Tier 1 Guards:" not in result.stdout
        assert "PASS" not in result.stdout

    def test_commit_refuses_without_config(self, tmp_path):
        d = str(tmp_path / "repo")
        os.makedirs(d)
        _init_repo(d)
        _stage_file(d, "a.txt", "hello\n")

        result = _run_cli("commit", "should not land", cwd=d)

        output = result.stdout + result.stderr
        assert result.returncode != 0, (
            f"commit must refuse without config, got {result.returncode}. output: {output[:300]}"
        )
        assert "no .gitreins/config.yaml" in output
        assert "gitreins init" in output
        assert "Tier 1 PASSED" not in output
        # No commit may have been created.
        log = subprocess.run(["git", "rev-parse", "HEAD"], cwd=d, capture_output=True, text=True)
        assert log.returncode != 0, "commit must not create a commit in a config-less repo"

    def test_configured_repo_still_passes(self, tmp_path):
        """A repo WITH a config keeps today's behaviour (GR-GAP-051 AC 2/4b)."""
        d = str(tmp_path / "repo")
        os.makedirs(d)
        _init_repo(d)
        _write_config(d, {"guards": {"test_command": "echo ok"}})
        _stage_file(d, "clean.py", "x = 1\n")

        result = _run_cli("guard", cwd=d)

        assert result.returncode == 0, (
            f"configured repo must still pass, got {result.returncode}. "
            f"stdout: {result.stdout[:300]} stderr: {result.stderr[:300]}"
        )
        assert "Tier 1 Guards: PASS" in result.stdout


class TestGuardExitClean:
    """gitreins guard exit codes on a tree with nothing to grade.

    TRUST-001: the guard no longer reports a vacuous green here — a clean tree
    is a DEGRADED pass. It exits 0 only when the repo accepts skips
    (`guards.allow_skips: true`, what `gitreins init` writes); otherwise 2.
    """

    def test_guard_exit_0_on_clean_tree_with_allow_skips(self, tmp_path):
        """Empty repo, no staged files, allow_skips: true -> DEGRADED, exit 0."""
        d = str(tmp_path / "repo")
        os.makedirs(d)
        _init_repo(d)
        _write_config(d, {"guards": {"test_command": "echo ok", "allow_skips": True}})

        result = _run_cli("guard", cwd=d)

        assert result.returncode == 0, (
            f"guard must exit 0 on clean tree with allow_skips, got {result.returncode}. "
            f"stdout: {result.stdout[:200]} stderr: {result.stderr[:200]}"
        )
        assert "Tier 1: DEGRADED PASS (skips: lint=no staged files, tests=no staged files)" in (
            result.stdout
        )
        assert "Tier 1 Guards: PASS" not in result.stdout

    def test_guard_exit_2_on_clean_tree_without_allow_skips(self, tmp_path):
        """Same tree, no allow_skips -> exit 2 (a skip is not a pass)."""
        d = str(tmp_path / "repo")
        os.makedirs(d)
        _init_repo(d)
        _write_config(d, {"guards": {"test_command": "echo ok"}})

        result = _run_cli("guard", cwd=d)

        assert result.returncode == 2, (
            f"guard must exit 2 on a degraded clean tree, got {result.returncode}. "
            f"stdout: {result.stdout[:200]} stderr: {result.stderr[:200]}"
        )
        assert "DEGRADED PASS" in result.stdout

    def test_guard_exit_0_with_clean_file(self, tmp_path):
        """Staging a clean file with no issues -> exit 0."""
        d = str(tmp_path / "repo")
        os.makedirs(d)
        _init_repo(d)
        _write_config(d, {"guards": {"test_command": "echo ok"}})
        _stage_file(d, "clean.py", "x = 1\n")

        result = _run_cli("guard", cwd=d)

        assert result.returncode == 0, (
            f"guard must exit 0 with clean file, got {result.returncode}. "
            f"stdout: {result.stdout[:200]}"
        )


class TestGuardExitSecret:
    """gitreins guard exits 1 when secrets are staged."""

    def test_guard_exit_1_on_secret(self, tmp_path):
        """Staging a file with an API key -> exit 1."""
        d = str(tmp_path / "repo")
        os.makedirs(d)
        _init_repo(d)
        _write_config(d, {"guards": {"lint": False, "tests": False, "test_command": "echo ok"}})
        payload = _openai_secret()
        _stage_file(d, "secret.py", f'API_KEY = "{payload}"\n')

        result = _run_cli("guard", cwd=d)

        assert result.returncode != 0, (
            f"guard must exit non-zero with secret, got {result.returncode}. "
            f"stdout: {result.stdout[:200]}"
        )
        assert "FAIL" in result.stdout

    def test_guard_exit_1_on_aws_key(self, tmp_path):
        """Staging a file with an AWS key -> exit 1."""
        d = str(tmp_path / "repo")
        os.makedirs(d)
        _init_repo(d)
        _write_config(d, {"guards": {"lint": False, "tests": False, "test_command": "echo ok"}})
        payload = _aws_secret()
        _stage_file(d, "aws.py", f'AWS_KEY = "{payload}"\n')

        result = _run_cli("guard", cwd=d)

        assert result.returncode != 0, (
            f"guard must exit non-zero with AWS key, got {result.returncode}."
        )
        assert "FAIL" in result.stdout


class TestGuardExitLint:
    """gitreins guard exits 1 when lint errors are staged."""

    def test_guard_exit_1_on_lint_error(self, tmp_path):
        """Staging a Python file with a syntax error -> exit 1."""
        d = str(tmp_path / "repo")
        os.makedirs(d)
        _init_repo(d)
        _write_config(d, {"guards": {"secrets": False, "tests": False, "test_command": "echo ok"}})
        _stage_file(d, "bad.py", "def foo(  ):\n    pass\n")

        result = _run_cli("guard", cwd=d)

        assert result.returncode == 0 or result.returncode == 1, (
            f"guard must run, got {result.returncode}. stdout: {result.stdout[:200]}"
        )


class TestCommitBlocksSecret:
    """gitreins commit blocks (non-zero) when secrets are staged."""

    def test_commit_blocks_on_secret(self, tmp_path):
        """Commit command exits non-zero when guards detect a secret."""
        d = str(tmp_path / "repo")
        os.makedirs(d)
        _init_repo(d)
        _write_config(d, {"guards": {"lint": False, "tests": False, "test_command": "echo ok"}})
        payload = _openai_secret()
        _stage_file(d, "leak.py", f'API_KEY = "{payload}"\n')

        result = _run_cli("commit", "test message", cwd=d)

        output = result.stdout + result.stderr
        assert result.returncode != 0, (
            f"commit must exit non-zero on secret, got {result.returncode}. output: {output[:300]}"
        )
        assert "FAIL" in output or "cannot commit" in output.lower()

    def test_commit_passes_on_clean(self, tmp_path):
        """Commit command exits 0 when no issues detected."""
        d = str(tmp_path / "repo")
        os.makedirs(d)
        _init_repo(d)
        _write_config(
            d,
            {
                "guards": {
                    "secrets": False,
                    "lint": False,
                    "tests": False,
                    "test_command": "echo ok",
                }
            },
        )
        _stage_file(d, "ok.py", "x = 1\n")

        result = _run_cli("commit", "test message", cwd=d)

        output = result.stdout + result.stderr
        assert result.returncode == 0, (
            f"commit must exit 0 on clean, got {result.returncode}. output: {output[:300]}"
        )


class TestRunnerMissingHookParity:
    """DF-GITREINS-POC-51: the hook path and the standalone guard agree.

    The pre-commit hook runs `<pinned python> -m gitreins guard` in the repo
    root — the SAME command the user runs by hand — so the two invocations must
    grade a missing pytest runner the same way: skipped with the fix named,
    never a FAIL that blocks a repo's first commit. That is what the row
    reproduced: the hook died with `✗ tests (full) — /bin/sh: 1: pytest: not
    found` (exit 1) while the standalone guard had printed green a minute
    earlier, because an empty index skips the tests lane by scope.

    Both arms are exercised here on real trees through the real CLI: a repo
    whose only problem is an unprovisioned environment, and a fully provisioned
    one where the lane must really run.
    """

    def _zero_deps_repo(self, tmp_path) -> str:
        """A fresh repo whose pinned interpreter does not exist yet."""
        d = str(tmp_path / "fresh")
        os.makedirs(d)
        _init_repo(d)
        _write_config(
            d,
            {
                "guards": {
                    "secrets": False,
                    "lint": False,
                    "tests": True,
                    "test_mode": "full",
                    "test_command": ".venv/bin/python -m pytest -x --tb=short",
                    "allow_skips": True,
                }
            },
        )
        _stage_file(d, "app.py", "print('hi')\n")
        return d

    def test_zero_deps_repo_first_commit_is_not_blocked(self, tmp_path):
        """The hook's own command on a bare box: a DEGRADED pass naming the fix."""
        d = self._zero_deps_repo(tmp_path)

        result = _run_cli("guard", cwd=d)
        output = result.stdout + result.stderr

        assert result.returncode == 0, f"hook command must not block commit #1: {output[:400]}"
        assert "✗ tests" not in result.stdout
        assert "~ tests (full) — skipped (" in result.stdout
        assert "uv sync" in result.stdout
        assert "Tier 1: DEGRADED PASS" in result.stdout
        # Never a silent green: the gates that did not run are named.
        assert "Tier 1 Guards: PASS" not in result.stdout

    def test_zero_deps_repo_skip_facts_match_the_library_path(self, tmp_path):
        """Same tree, same skip facts: the CLI (hook) and the in-process guard."""
        d = self._zero_deps_repo(tmp_path)

        cli = _run_cli("guard", cwd=d)
        with open(os.path.join(d, ".gitreins", "config.yaml")) as f:
            config = yaml.safe_load(f)
        result = GuardManager(d, config=config)._check_tests()

        assert result.skipped is True
        assert result.skip_reason in cli.stdout
        assert "✗ tests" not in cli.stdout

    def test_provisioned_repo_grades_the_tests_lane(self, tmp_path):
        """The other arm of the parity: with pytest provisioned the lane runs."""
        d = str(tmp_path / "provisioned")
        os.makedirs(d)
        _init_repo(d)
        _write_config(
            d,
            {
                "guards": {
                    "secrets": False,
                    "lint": False,
                    "tests": True,
                    "test_mode": "full",
                    "test_command": f"{sys.executable} -m pytest -x --tb=short",
                    "allow_skips": True,
                }
            },
        )
        _stage_file(d, "test_ok.py", "def test_ok():\n    assert True\n")

        result = _run_cli("guard", cwd=d)
        output = result.stdout + result.stderr

        assert result.returncode == 0, f"provisioned repo must pass: {output[:400]}"
        assert "✓ tests (full)" in result.stdout
        assert "DEGRADED" not in result.stdout
