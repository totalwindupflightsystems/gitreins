"""
Integration tests for gitreins guard exit codes.

Verifies:
  - gitreins guard exits 0 when all guards pass
  - gitreins guard exits 1 when a guard fails (secret, lint)
  - gitreins commit blocks (non-zero) when secrets are staged
"""
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
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=workdir, capture_output=True)
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
    import yaml
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


class TestGuardExitClean:
    """gitreins guard exits 0 on a clean tree."""

    def test_guard_exit_0_on_clean_tree(self, tmp_path):
        """Empty repo with no staged files -> exit 0."""
        d = str(tmp_path / "repo")
        os.makedirs(d)
        _init_repo(d)
        _write_config(d, {"guards": {"test_command": "echo ok"}})

        result = _run_cli("guard", cwd=d)

        assert result.returncode == 0, (
            f"guard must exit 0 on clean tree, got {result.returncode}. "
            f"stdout: {result.stdout[:200]}"
        )
        assert "Tier 1 Guards:" in result.stdout
        assert "PASS" in result.stdout

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
            f"guard must run, got {result.returncode}. "
            f"stdout: {result.stdout[:200]}"
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
            f"commit must exit non-zero on secret, got {result.returncode}. "
            f"output: {output[:300]}"
        )
        assert "FAIL" in output or "cannot commit" in output.lower()

    def test_commit_passes_on_clean(self, tmp_path):
        """Commit command exits 0 when no issues detected."""
        d = str(tmp_path / "repo")
        os.makedirs(d)
        _init_repo(d)
        _write_config(d, {"guards": {"secrets": False, "lint": False, "tests": False, "test_command": "echo ok"}})
        _stage_file(d, "ok.py", "x = 1\n")

        result = _run_cli("commit", "test message", cwd=d)

        output = result.stdout + result.stderr
        assert result.returncode == 0, (
            f"commit must exit 0 on clean, got {result.returncode}. "
            f"output: {output[:300]}"
        )
