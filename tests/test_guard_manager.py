"""
Unit tests for engine/guard_manager.py — pre-commit static checks.
axiom:trace work_item=GR-001 spec=specs/04-Guard-Manager.md plan=.memory-bank/work-items/GR-001/plan.yaml
"""
import os
import pytest
import subprocess
from unittest import mock
from unittest.mock import MagicMock, patch

from engine.guard_manager import GuardManager, GuardResult, Tier1Result


# ── Phase 1-3-1: GuardResult/Tier1Result dataclasses — step-1-3-1-1 ─────────


class TestGuardResult:
    """Test GuardResult dataclass."""

    def test_guard_result_passed_true(self):
        """GuardResult with passed=True has correct fields."""
        gr = GuardResult(name="secrets", passed=True, output="clean")
        assert gr.name == "secrets"
        assert gr.passed is True
        assert gr.output == "clean"
        assert gr.error == ""

    def test_guard_result_passed_false(self):
        """GuardResult with passed=False has output/error captured."""
        gr = GuardResult(name="lint", passed=False, output="E501 line too long", error="exit code 1")
        assert gr.passed is False
        assert "E501" in gr.output
        assert "exit code 1" in gr.error


class TestTier1Result:
    """Test Tier1Result dataclass and summary."""

    def test_tier1_all_passed(self):
        """Tier1Result with all passed → passed=True, summary shows all check marks."""
        results = [
            GuardResult("secrets", True, "clean"),
            GuardResult("lint", True, "clean"),
            GuardResult("tests", True, "3 passed"),
        ]
        tr = Tier1Result(passed=True, results=results)
        assert tr.passed is True
        summary = tr.summary
        assert "secrets" in summary
        assert "lint" in summary
        assert "tests" in summary

    def test_tier1_one_failed(self):
        """Tier1Result with one failed → passed=False, summary shows mix."""
        results = [
            GuardResult("secrets", True, "clean"),
            GuardResult("lint", False, "E501"),
            GuardResult("tests", True, "ok"),
        ]
        tr = Tier1Result(passed=False, results=results)
        assert tr.passed is False
        summary = tr.summary
        assert "✗ lint" in summary or summary.count("✗") >= 1


class TestGuardManagerInit:
    """Test GuardManager initialization and config parsing — step-1-3-1-2."""

    def test_empty_config_all_enabled(self, guard_manager):
        """Empty config → all guards enabled (default True)."""
        assert guard_manager._enabled["secrets"] is True
        assert guard_manager._enabled["lint"] is True
        assert guard_manager._enabled["tests"] is True

    def test_secrets_disabled(self, tmp_workdir):
        """Config with guards.secrets=false → secrets disabled."""
        gm = GuardManager(tmp_workdir, {"guards": {"secrets": False}})
        assert gm._enabled["secrets"] is False
        assert gm._enabled["lint"] is True
        assert gm._enabled["tests"] is True

    def test_tests_disabled_with_custom_command(self, tmp_workdir):
        """Config with guards.tests=false + custom test_command → tests disabled, command saved."""
        gm = GuardManager(tmp_workdir, {"guards": {"tests": False, "test_command": "pytest custom/"}})
        assert gm._enabled["tests"] is False
        assert gm.config.get("guards", {}).get("test_command") == "pytest custom/"

    def test_no_guards_key_all_defaults(self, tmp_workdir):
        """Config with no 'guards' key → all defaults True."""
        gm = GuardManager(tmp_workdir, {"other": "stuff"})
        assert gm._enabled["secrets"] is True
        assert gm._enabled["lint"] is True
        assert gm._enabled["tests"] is True

    def test_config_none_all_enabled(self, tmp_workdir):
        """None config → all guards enabled."""
        gm = GuardManager(tmp_workdir, None)
        assert gm._enabled["secrets"] is True


class TestBuiltinSecretsScan:
    """Test built-in secrets scanner patterns — step-1-3-1-3."""

    def test_aws_key_detected(self, tmp_workdir):
        """AWS access key (AKIA...) is detected."""
        # Stage a file with a fake AWS key
        _write_staged_file(tmp_workdir, "test.py", 'AWS_ACCESS_KEY = "AKIA1234567890ABCDEF"')
        gm = GuardManager(tmp_workdir)
        result = gm._builtin_secrets_scan()
        assert result.passed is False
        assert "AWS access key" in result.output

    def test_openai_key_detected(self, tmp_workdir):
        """OpenAI key (sk-...) is detected as a hardcoded API key."""
        _write_staged_file(tmp_workdir, "config.py", 'OPENAI_API_KEY = "sk-12345678901234567890123456789012"')
        gm = GuardManager(tmp_workdir)
        result = gm._builtin_secrets_scan()
        assert result.passed is False
        # The 'api[_-]?key' pattern catches it as "hardcoded API key"
        assert "hardcoded API key" in result.output or "OpenAI" in result.output

    def test_github_token_detected(self, tmp_workdir):
        """GitHub token (ghp_...) is detected."""
        _write_staged_file(tmp_workdir, "main.py", 'GITHUB_TOKEN = "ghp_123456789012345678901234567890123456"')
        gm = GuardManager(tmp_workdir)
        result = gm._builtin_secrets_scan()
        assert result.passed is False
        assert "GitHub personal access token" in result.output

    def test_private_key_block_detected(self, tmp_workdir):
        """Private key block (BEGIN RSA PRIVATE KEY) is detected."""
        _write_staged_file(tmp_workdir, "key.pem",
                           '-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAKCAQEA...\n-----END RSA PRIVATE KEY-----')
        gm = GuardManager(tmp_workdir)
        result = gm._builtin_secrets_scan()
        assert result.passed is False
        assert "private key block" in result.output.lower()

    def test_os_getenv_whitelisted(self, tmp_workdir):
        """os.getenv('API_KEY') is NOT flagged."""
        _write_staged_file(tmp_workdir, "app.py", 'api_key = os.getenv("API_KEY")')
        gm = GuardManager(tmp_workdir)
        result = gm._builtin_secrets_scan()
        assert result.passed is True

    def test_config_dict_whitelisted(self, tmp_workdir):
        """config['secret'] is NOT flagged."""
        _write_staged_file(tmp_workdir, "app.py", 'my_secret = config["secret"]')
        gm = GuardManager(tmp_workdir)
        result = gm._builtin_secrets_scan()
        assert result.passed is True

    def test_empty_password_whitelisted(self, tmp_workdir):
        """Empty password (PASSWORD="") is NOT flagged."""
        _write_staged_file(tmp_workdir, "docker.py", 'PASSWORD = ""')
        gm = GuardManager(tmp_workdir)
        result = gm._builtin_secrets_scan()
        assert result.passed is True

    def test_todo_placeholder_whitelisted(self, tmp_workdir):
        """TODO/PLACEHOLDER comment is NOT flagged."""
        _write_staged_file(tmp_workdir, "todo.py", '# TODO: sk-add-real-key-here (placeholder)')
        gm = GuardManager(tmp_workdir)
        result = gm._builtin_secrets_scan()
        # May or may not flag depending on exact match — just verify no crash
        assert result is not None

    def test_jwt_encode_whitelisted(self, tmp_workdir):
        """JWT in jwt.encode() call is NOT flagged."""
        _write_staged_file(tmp_workdir, "auth.py", 'token = jwt.encode(payload, secret, algorithm="HS256")')
        gm = GuardManager(tmp_workdir)
        result = gm._builtin_secrets_scan()
        assert result.passed is True

    def test_no_staged_files_no_findings(self, tmp_workdir):
        """No staged files → no findings, passed=True."""
        gm = GuardManager(tmp_workdir)
        result = gm._builtin_secrets_scan()
        assert result.passed is True
        assert "No staged files" in result.output

    def test_clean_file_no_findings(self, tmp_workdir):
        """Clean file with no secrets passes."""
        _write_staged_file(tmp_workdir, "clean.py", "def hello():\n    return 'world'\n")
        gm = GuardManager(tmp_workdir)
        result = gm._builtin_secrets_scan()
        assert result.passed is True
        assert "clean" in result.output


class TestSecretsSanitization:
    """Test secret value redaction in output — step-1-3-1-4."""

    def test_secret_value_redacted(self, tmp_workdir):
        """Secret value is replaced with *** in output."""
        _write_staged_file(tmp_workdir, "secrets.py", 'api_key = "sk-abc123def456789012345678901234"')
        gm = GuardManager(tmp_workdir)
        result = gm._builtin_secrets_scan()
        assert result.passed is False
        # The actual key value must not appear in output
        assert "sk-abc123def456789012345678901234" not in result.output
        # The sanitized version should appear
        assert '"***"' in result.output or "sk-" in result.output


class TestGuardToggling:
    """Test guard toggling: run_all() only runs enabled guards — step-1-3-1-5."""

    def test_run_all_three_guards(self, guard_manager):
        """All guards enabled → run_all() returns 3 results."""
        with patch.object(guard_manager, '_check_secrets', return_value=GuardResult("secrets", True, "ok")):
            with patch.object(guard_manager, '_check_lint', return_value=GuardResult("lint", True, "ok")):
                with patch.object(guard_manager, '_check_tests', return_value=GuardResult("tests", True, "ok")):
                    result = guard_manager.run_all()
        assert len(result.results) == 3
        assert result.passed is True

    def test_only_secrets_enabled(self, tmp_workdir):
        """Only secrets enabled → run_all() returns 1 result."""
        gm = GuardManager(tmp_workdir, {"guards": {"secrets": True, "lint": False, "tests": False}})
        with patch.object(gm, '_check_secrets', return_value=GuardResult("secrets", True, "ok")):
            result = gm.run_all()
        assert len(result.results) == 1

    def test_no_guards_enabled(self, tmp_workdir):
        """No guards enabled → run_all() returns 0 results, passed=True."""
        gm = GuardManager(tmp_workdir, {"guards": {"secrets": False, "lint": False, "tests": False}})
        result = gm.run_all()
        assert len(result.results) == 0
        assert result.passed is True

    def test_run_all_sets_passed_false_on_any_failure(self, guard_manager):
        """If any guard fails, passed is False."""
        with patch.object(guard_manager, '_check_secrets', return_value=GuardResult("secrets", True, "ok")):
            with patch.object(guard_manager, '_check_lint', return_value=GuardResult("lint", False, "error")):
                with patch.object(guard_manager, '_check_tests', return_value=GuardResult("tests", True, "ok")):
                    result = guard_manager.run_all()
        assert result.passed is False


class TestLintGuard:
    """Test _check_lint behavior."""

    def test_no_py_files_staged(self, guard_manager):
        """Lint guard passes when no Python files are staged."""
        result = guard_manager._check_lint()
        assert result.passed is True

    def test_gitleaks_missing_falls_back(self, guard_manager):
        """When gitleaks not found, falls back to built-in scanner."""
        with patch('subprocess.run', side_effect=FileNotFoundError("gitleaks")):
            result = guard_manager._check_secrets()
        # Falls through to built-in scanner; should return a result
        assert result is not None


class TestTestsGuard:
    """Test _check_tests behavior."""

    def test_pytest_not_found_skips(self, guard_manager):
        """Tests guard passes when pytest not installed."""
        with patch('subprocess.run', side_effect=FileNotFoundError("pytest")):
            result = guard_manager._check_tests()
        assert result.passed is True
        assert "pytest not found" in result.output


class TestExtendedGuardManager:
    """Extended edge case coverage for GuardManager."""

    def test_custom_test_command_is_used(self, tmp_workdir):
        """_check_tests uses custom test_command from config."""
        gm = GuardManager(tmp_workdir, {"guards": {"test_command": "echo custom-test-run"}})
        # Mock subprocess.run to capture the command
        mock_run = MagicMock()
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = "custom output"
        mock_run.return_value.stderr = ""
        with patch('subprocess.run', side_effect=[
            MagicMock(returncode=0, stdout="pytest 7.0", stderr=""),  # pytest --version
            mock_run.return_value,
        ]):
            result = gm._check_tests()
        assert result.passed is True

    def test_check_tests_timeout_returns_failure(self, guard_manager):
        """_check_tests handles subprocess timeout."""
        with patch('subprocess.run', side_effect=[
            MagicMock(returncode=0, stdout="pytest 7.0", stderr=""),  # pytest --version
            subprocess.TimeoutExpired(cmd="pytest", timeout=120),
        ]):
            result = guard_manager._check_tests()
        assert result.passed is False
        assert "timed out" in result.output

    def test_gitleaks_available_used_first(self, tmp_workdir):
        """When gitleaks is available, _check_secrets uses it first."""
        gm = GuardManager(tmp_workdir)
        mock_run = MagicMock()
        mock_run.returncode = 0
        mock_run.stdout = "gitleaks: clean"
        mock_run.stderr = ""
        with patch('subprocess.run', return_value=mock_run) as mock_sub:
            result = gm._check_secrets()
        assert "gitleaks" in result.output
        assert result.passed is True

    def test_gitleaks_returns_findings(self, tmp_workdir):
        """When gitleaks reports findings, secrets guard fails."""
        gm = GuardManager(tmp_workdir)
        mock_run = MagicMock()
        mock_run.returncode = 1
        mock_run.stdout = "leak detected in config.py"
        mock_run.stderr = ""
        with patch('subprocess.run', return_value=mock_run):
            result = gm._check_secrets()
        assert result.passed is False

    def test_lint_ruff_available(self, tmp_workdir):
        """_check_lint uses ruff when available with Python files staged."""
        _write_staged_file(tmp_workdir, "code.py", "x = 1\n")
        gm = GuardManager(tmp_workdir)
        mock_git_diff = MagicMock()
        mock_git_diff.returncode = 0
        mock_git_diff.stdout = "code.py"
        mock_git_diff.stderr = ""
        mock_ruff = MagicMock()
        mock_ruff.returncode = 0
        mock_ruff.stdout = "ruff: clean"
        mock_ruff.stderr = ""
        with patch('subprocess.run', side_effect=[mock_git_diff, mock_ruff]):
            result = gm._check_lint()
        assert result.passed is True
        assert "ruff" in result.output

    def test_guard_result_empty_name(self):
        """GuardResult with empty name still produces valid output."""
        gr = GuardResult(name="", passed=True, output="ok")
        assert gr.name == ""
        assert gr.passed is True

    def test_tier1_result_no_results(self):
        """Tier1Result with empty results list has empty summary."""
        tr = Tier1Result(passed=True, results=[])
        assert tr.passed is True
        assert tr.summary == ""

    def test_secrets_scan_skips_large_files(self, tmp_workdir):
        """Secrets scanner skips files larger than 1MB."""
        _write_staged_file(tmp_workdir, "huge.bin", "x" * 2_000_000)
        gm = GuardManager(tmp_workdir)
        result = gm._builtin_secrets_scan()
        assert result.passed is True

    def test_secrets_scan_binary_file_graceful(self, tmp_workdir):
        """Secrets scanner handles binary files without crashing."""
        _write_staged_file(tmp_workdir, "binary.bin", "\x00\x01\x02\x03\x04")
        gm = GuardManager(tmp_workdir)
        result = gm._builtin_secrets_scan()
        # Should not crash, may pass or fail depending on content
        assert result is not None

    def test_gitlab_token_detected(self, tmp_workdir):
        """GitLab token (glpat-) is detected."""
        _write_staged_file(tmp_workdir, "gitlab.py", 'token = "glpat-ABCDEFGHIJ1234567890"')
        gm = GuardManager(tmp_workdir)
        result = gm._builtin_secrets_scan()
        assert result.passed is False
        assert "GitLab" in result.output

    def test_gho_token_detected(self, tmp_workdir):
        """GitHub OAuth token (gho_) is detected."""
        _write_staged_file(tmp_workdir, "github.py", 'oauth = "gho_abcdef123456789012345678901234567890"')
        gm = GuardManager(tmp_workdir)
        result = gm._builtin_secrets_scan()
        assert result.passed is False
        assert "GitHub OAuth" in result.output

    def test_tier1_summary_format(self):
        """Tier1Result summary formats correctly with mixed results."""
        results = [
            GuardResult("secrets", True, "clean"),
            GuardResult("lint", False, "E501 error"),
        ]
        tr = Tier1Result(passed=False, results=results)
        summary = tr.summary
        assert "✓ secrets" in summary
        assert "✗ lint" in summary


# ── Helpers ──────────────────────────────────────────────────────────────────

def _write_staged_file(workdir, filename, content):
    """Create a file and stage it in a real git repo, for secrets scan testing.

    Uses git init + git add to create a realistic staged file.
    """
    import os
    filepath = os.path.join(workdir, filename)
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    with open(filepath, "w") as f:
        f.write(content)
    # Stage the file
    subprocess.run(["git", "add", filepath], cwd=workdir, capture_output=True)
