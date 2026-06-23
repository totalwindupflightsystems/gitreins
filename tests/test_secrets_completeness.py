"""
Secrets scanner completeness — verifies ALL danger pattern types are detected
and ALL whitelisted patterns are correctly ignored.

Requirement checklist:
  - Pattern completeness: AWS AKIA, Stripe sk_live_, GitHub ghp_, GitLab glpat-,
    OpenAI sk-, Slack xox, GCP AIza, private key blocks, hardcoded passwords,
    JWTs as literals, OpenRouter sk-or-v1- (with dashes)
  - Whitelist correctness: os.getenv(), config['key'], jwt.encode(), shell vars,
    template vars, placeholders, empty passwords
  - Output sanitization: actual key values replaced with ***
  - Documentation skip: .md files, .memory-bank/, docs/ are ignored
"""

import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _scan_text(text: str) -> list[tuple[str, str]]:
    """Run the built-in scanner against a single string of text. Returns list of (label, match) tuples."""
    from engine.guard_manager import GuardManager
    import yaml

    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run(["git", "init", "-q"], cwd=tmp, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=tmp, capture_output=True)
        subprocess.run(["git", "config", "user.name", "test"], cwd=tmp, capture_output=True)

        os.makedirs(os.path.join(tmp, ".gitreins"), exist_ok=True)
        with open(os.path.join(tmp, ".gitreins", "config.yaml"), "w") as f:
            yaml.dump({"guards": {"secrets": True, "lint": False, "tests": False}}, f)

        fpath = os.path.join(tmp, "test.py")
        with open(fpath, "w") as f:
            f.write(text)
        subprocess.run(["git", "add", "test.py"], cwd=tmp, capture_output=True)

        gm = GuardManager(tmp)
        result = gm._builtin_secrets_scan()

        matches = []
        for line in result.output.split("\n"):
            for pattern in [
                "private key block",
                "AWS access key",
                "AWS secret access key",
                "GCP API key",
                "DigitalOcean access token",
                "Stripe live secret key",
                "Stripe restricted key",
                "Azure storage connection string",
                "Azure storage account key",
                "Slack API token",
                "GitHub personal access token",
                "GitHub OAuth token",
                "GitLab personal access token",
                "OpenAI/OpenRouter API key",
                "hardcoded API key",
                "hardcoded JWT",
                "hardcoded password",
                "hardcoded secret",
            ]:
                if pattern in line:
                    matches.append((pattern, line.strip()))
        return matches


def _scan_file(fixture_relpath: str) -> list[tuple[str, str]]:
    """Run the built-in scanner against a fixture file. Returns list of (label, match) tuples."""
    from engine.guard_manager import GuardManager
    import yaml

    basedir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    fixture_path = os.path.join(basedir, fixture_relpath)

    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run(["git", "init", "-q"], cwd=tmp, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=tmp, capture_output=True)
        subprocess.run(["git", "config", "user.name", "test"], cwd=tmp, capture_output=True)

        os.makedirs(os.path.join(tmp, ".gitreins"), exist_ok=True)
        with open(os.path.join(tmp, ".gitreins", "config.yaml"), "w") as f:
            yaml.dump({"guards": {"secrets": True, "lint": False, "tests": False}}, f)

        # Copy fixture file into the temp git repo
        relname = os.path.basename(fixture_path)
        dest = os.path.join(tmp, relname)
        with open(fixture_path) as src_f:
            with open(dest, "w") as dst_f:
                dst_f.write(src_f.read())
        subprocess.run(["git", "add", relname], cwd=tmp, capture_output=True)

        gm = GuardManager(tmp)
        result = gm._builtin_secrets_scan()

        matches = []
        for line in result.output.split("\n"):
            for pattern in [
                "private key block",
                "AWS access key",
                "AWS secret access key",
                "GCP API key",
                "DigitalOcean access token",
                "Stripe live secret key",
                "Stripe restricted key",
                "Azure storage connection string",
                "Azure storage account key",
                "Slack API token",
                "GitHub personal access token",
                "GitHub OAuth token",
                "GitLab personal access token",
                "OpenAI/OpenRouter API key",
                "hardcoded API key",
                "hardcoded JWT",
                "hardcoded password",
                "hardcoded secret",
            ]:
                if pattern in line:
                    matches.append((pattern, line.strip()))
        return matches


def _any_match(matches: list, *labels: str) -> bool:
    """True if any of the given labels appear in matches."""
    match_labels = {m[0] for m in matches}
    return bool(match_labels & set(labels))


# ══════════════════════════════════════════════════════════════════
#   PATTERN COMPLETENESS — every danger type must be detected
# ══════════════════════════════════════════════════════════════════

class TestPatternCompleteness:
    """All 11+ danger pattern types must be detected."""

    def test_aws_access_key(self):
        p1, p2 = "AK", "IA"
        key = p1 + p2 + "JG7KQ4M3N6P2R5TU"
        matches = _scan_text(f'AWS_ACCESS_KEY_ID = "{key}"')
        assert _any_match(matches, "AWS access key")

    def test_aws_secret_key(self):
        matches = _scan_text(
            'AWS_SECRET_ACCESS_KEY = "wJalrXUtnFEMIK7MDENGbPxRfiCYZ9a4pQ3sT0vK2nL5mH8rD1wF6xJ3"'
        )
        assert _any_match(matches, "AWS secret access key")

    def test_stripe_live_secret(self):
        prefix = "sk_" + "live_"
        key = prefix + "NOTAREALKEY000000000000000000000000000000000000"
        matches = _scan_text(f'STRIPE_SECRET_KEY = "{key}"')
        assert _any_match(matches, "Stripe live secret key")

    def test_stripe_restricted_key(self):
        prefix = "rk_" + "live_"
        key = prefix + "NOTAREALKEY000000000000000000000000000000000000"
        matches = _scan_text(f'STRIPE_KEY = "{key}"')
        assert _any_match(matches, "Stripe restricted key")

    def test_github_token(self):
        matches = _scan_text('GITHUB_TOKEN = "ghp_abc123def456ghi789jkl012mno345pqr678stu"')
        assert _any_match(matches, "GitHub personal access token")

    def test_github_oauth_token(self):
        matches = _scan_text('GITHUB_TOKEN = "gho_abc123def456ghi789jkl012mno345pqr678stu"')
        assert _any_match(matches, "GitHub personal access token", "GitHub OAuth token")

    def test_gitlab_token(self):
        matches = _scan_text('GITLAB_TOKEN = "glpat-abc123def456ghi789jkl012mno345"')
        assert _any_match(matches, "GitLab personal access token")

    def test_openai_key(self):
        matches = _scan_text(
            'OPENAI_API_KEY = "sk-proj-abc123def456ghi789jkl012mno345pqr678stuvwxyz"'
        )
        assert _any_match(matches, "OpenAI/OpenRouter API key", "hardcoded API key")

    def test_openrouter_key_with_dashes(self):
        """sk-or-v1- keys (OpenRouter format with dashes) must be detected."""
        matches = _scan_text(
            'OPENROUTER_API_KEY = "sk-or-v1-abc123def456ghi789jkl012mno345pqr678stuvwxyz1234"'
        )
        assert _any_match(matches, "OpenAI/OpenRouter API key", "hardcoded API key")

    def test_slack_bot_token(self):
        prefix = "xox" + "b-"
        token = prefix + "NOTAREAL-NOTAREAL-NOTAREALsynthetic0000000000000000"
        matches = _scan_text(f'SLACK_TOKEN = "{token}"')
        assert _any_match(matches, "Slack API token")

    def test_slack_user_token(self):
        prefix = "xox" + "p-"
        token = prefix + "NOTAREAL-NOTAREAL-NOTAREALsynthetic0000000000000000"
        matches = _scan_text(f'token = "{token}"')
        assert _any_match(matches, "Slack API token")

    def test_gcp_api_key(self):
        matches = _scan_text('GCP_API_KEY = "AIzaSyD4i8HrK2mN9pQ5sT0vW1xF6jL3aB7cE9dG0fI4k"')
        assert _any_match(matches, "GCP API key")

    def test_private_key_rsa(self):
        matches = _scan_text("""-----BEGIN RSA PRIVATE KEY-----
MIIEpAIBAAKCAQEA0Z3Yj5K7w8N2mQpL4xVfH6tR9sA1bC3dE5fG7hI9jK0L1mN2oP
-----END RSA PRIVATE KEY-----""")
        assert _any_match(matches, "private key block")

    def test_private_key_openssh(self):
        matches = _scan_text("""-----BEGIN OPENSSH PRIVATE KEY-----
b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAABAAABFwAAAAdzc2gtcn
-----END OPENSSH PRIVATE KEY-----""")
        assert _any_match(matches, "private key block")

    def test_private_key_pkcs8(self):
        matches = _scan_text("""-----BEGIN PRIVATE KEY-----
MC4CAQAwBQYDK2VwBCIEIJ3tNqRx7Bm5LfHk8Ys2Dc0WvPqR4Sa6TbU9Ve0XfGhK
-----END PRIVATE KEY-----""")
        assert _any_match(matches, "private key block")

    def test_private_key_encrypted(self):
        matches = _scan_text("""-----BEGIN ENCRYPTED PRIVATE KEY-----
MIIFHDBOBgkqhkiG9w0BBQ0wQTApBgkqhkiG9w0BBQwwHAQIgV7nY2FxR9K0LoMCAggA
-----END ENCRYPTED PRIVATE KEY-----""")
        assert _any_match(matches, "private key block")

    def test_private_key_pgp(self):
        matches = _scan_text("""-----BEGIN PGP PRIVATE KEY BLOCK-----
lQdGBGcX9hEBEAC8Nq3k5J7mP2sV0wX4yB6cR8tU1nA3dF5hG9iK2lM4oQ6rS7vW0xZ
-----END PGP PRIVATE KEY BLOCK-----""")
        assert _any_match(matches, "private key block")

    def test_hardcoded_password(self):
        matches = _scan_text('DB_PASSWORD = "SuperSecret123!"')
        assert _any_match(matches, "hardcoded password")

    def test_hardcoded_passwd(self):
        """passwd= assignment must be caught."""
        matches = _scan_text('passwd = "MyP@ssw0rdIsStr0ng!"')
        assert _any_match(matches, "hardcoded password")

    def test_hardcoded_pwd(self):
        """pwd= assignment must be caught (new in v0.7.5)."""
        matches = _scan_text('pwd = "Str0ngP@ssw0rd!"')
        assert _any_match(matches, "hardcoded password")

    def test_hardcoded_jwt(self):
        matches = _scan_text(
            'token = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"'
        )
        assert _any_match(matches, "hardcoded JWT")

    def test_digitalocean_token(self):
        matches = _scan_text(
            'DO_TOKEN = "dop_v1_abc123def456ghi789jkl012mno345pqr678stuvwxyz9012abcdef3456ghij7890klmn"'
        )
        assert _any_match(matches, "DigitalOcean access token")

    def test_azure_connection_string(self):
        matches = _scan_text(
            'CONN_STR = "DefaultEndpointsProtocol=https;AccountName=mystorage;'
            'AccountKey=abcdefghijklmnopqrstuvwxyz1234567890ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890=="'
        )
        assert _any_match(matches, "Azure storage connection string")

    def test_azure_account_key(self):
        matches = _scan_text(
            'AccountKey = "abcdefghijklmnopqrstuvwxyz1234567890ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890=="'
        )
        assert _any_match(matches, "Azure storage account key")


# ══════════════════════════════════════════════════════════════════
#   WHITELIST CORRECTNESS — false positives must NOT be flagged
# ══════════════════════════════════════════════════════════════════

class TestWhitelist:
    """All whitelisted patterns must be ignored."""

    def test_os_getenv_not_flagged(self):
        matches = _scan_text('API_KEY = os.getenv("MY_API_KEY")')
        assert len(matches) == 0

    def test_os_environ_not_flagged(self):
        matches = _scan_text('SECRET = os.environ.get("MY_SECRET")')
        assert len(matches) == 0

    def test_config_dict_access_not_flagged(self):
        """config['key'] must not trigger."""
        matches = _scan_text('config_key = config["api_key"]')
        assert len(matches) == 0

    def test_settings_dict_access_not_flagged(self):
        """settings['key'] must not trigger."""
        matches = _scan_text('settings_key = settings["secret"]')
        assert len(matches) == 0

    def test_jwt_encode_not_flagged(self):
        matches = _scan_text('token = jwt.encode(payload, secret_key, algorithm="HS256")')
        assert len(matches) == 0

    def test_jwt_decode_not_flagged(self):
        matches = _scan_text('decoded = jwt.decode(token, secret_key, algorithms=["HS256"])')
        assert len(matches) == 0

    def test_b64encode_not_flagged(self):
        matches = _scan_text('encoded = b64encode(data)')
        assert len(matches) == 0

    def test_shell_var_braces_not_flagged(self):
        matches = _scan_text('export API_KEY=${SECRET_KEY}')
        assert len(matches) == 0

    def test_shell_var_no_braces_not_flagged(self):
        """$KEY (without braces) must not trigger."""
        matches = _scan_text('export PASSWORD=$DB_PASS')
        assert len(matches) == 0

    def test_template_double_braces_not_flagged(self):
        """{{ key }} must not trigger."""
        matches = _scan_text('api_key: {{ secrets.API_KEY }}')
        assert len(matches) == 0

    def test_template_percent_braces_not_flagged(self):
        """{% key %} must not trigger."""
        matches = _scan_text('password: {% raw %}{{ DB_PASSWORD }}{% endraw %}')
        assert len(matches) == 0

    def test_placeholder_not_flagged(self):
        matches = _scan_text('API_KEY = "YOUR_API_KEY_HERE"')
        assert len(matches) == 0

    def test_placeholder_angle_brackets_not_flagged(self):
        """<your-key> must not trigger."""
        matches = _scan_text('SECRET = "<your-secret-key>"')
        assert len(matches) == 0

    def test_changeme_not_flagged(self):
        matches = _scan_text('PASSWORD = "changeme"')
        assert len(matches) == 0

    def test_empty_password_not_flagged(self):
        matches = _scan_text('PASSWORD = ""')
        assert len(matches) == 0

    def test_empty_password_lowercase_not_flagged(self):
        """password = \"\" (lowercase) must not trigger."""
        matches = _scan_text('password = ""')
        assert len(matches) == 0

    def test_empty_passwd_not_flagged(self):
        matches = _scan_text('passwd = ""')
        assert len(matches) == 0

    def test_empty_pwd_not_flagged(self):
        matches = _scan_text('pwd = ""')
        assert len(matches) == 0

    def test_todo_comment_not_flagged(self):
        matches = _scan_text('# TODO: sk-add-real-key-here for testing')
        assert len(matches) == 0

    def test_fixme_comment_not_flagged(self):
        matches = _scan_text('# FIXME: add-real-api-key-12345678901234567890')
        assert len(matches) == 0

    def test_example_label_not_flagged(self):
        matches = _scan_text('AWS_ACCESS_KEY_ID = "AKIAIOSFODNN7EXAMPLE"')
        assert len(matches) == 0


# ══════════════════════════════════════════════════════════════════
#   OUTPUT SANITIZATION — actual key values must be ***'d out
# ══════════════════════════════════════════════════════════════════

class TestOutputSanitization:
    """Guard output must replace secret values with ***."""

    def test_sanitized_github_token(self):
        from engine.guard_manager import GuardManager
        import yaml

        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run(["git", "init", "-q"], cwd=tmp, capture_output=True)
            subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=tmp, capture_output=True)
            subprocess.run(["git", "config", "user.name", "test"], cwd=tmp, capture_output=True)

            os.makedirs(os.path.join(tmp, ".gitreins"), exist_ok=True)
            with open(os.path.join(tmp, ".gitreins", "config.yaml"), "w") as f:
                yaml.dump({"guards": {"secrets": True, "lint": False, "tests": False}}, f)

            fpath = os.path.join(tmp, "test.py")
            with open(fpath, "w") as f:
                f.write('GITHUB_TOKEN = "ghp_abc123def456ghi789jkl012mno345pqr678stu"')
            subprocess.run(["git", "add", "test.py"], cwd=tmp, capture_output=True)

            gm = GuardManager(tmp)
            result = gm._builtin_secrets_scan()

            # The actual token value ghp_abc123... must NOT appear in output
            assert "ghp_abc123def456ghi789jkl012mno345pqr678stu" not in result.output
            # The sanitized placeholder must appear
            assert "***" in result.output

    def test_sanitized_aws_secret(self):
        from engine.guard_manager import GuardManager
        import yaml

        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run(["git", "init", "-q"], cwd=tmp, capture_output=True)
            subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=tmp, capture_output=True)
            subprocess.run(["git", "config", "user.name", "test"], cwd=tmp, capture_output=True)

            os.makedirs(os.path.join(tmp, ".gitreins"), exist_ok=True)
            with open(os.path.join(tmp, ".gitreins", "config.yaml"), "w") as f:
                yaml.dump({"guards": {"secrets": True, "lint": False, "tests": False}}, f)

            fpath = os.path.join(tmp, "test.py")
            with open(fpath, "w") as f:
                f.write(
                    'AWS_SECRET = "wJalrXUtnFEMIK7MDENGbPxRfiCYZ9a4pQ3sT0vK2nL5mH8rD1wF6xJ3"'
                )
            subprocess.run(["git", "add", "test.py"], cwd=tmp, capture_output=True)

            gm = GuardManager(tmp)
            result = gm._builtin_secrets_scan()

            assert "wJalrXUtnFEMIK7MDENGbPxRfiCYZ9a4pQ3sT0vK2nL5mH8rD1wF6xJ3" not in result.output
            assert "***" in result.output


# ══════════════════════════════════════════════════════════════════
#   DOCUMENTATION SKIP — .md, .memory-bank/, docs/ must be skipped
# ══════════════════════════════════════════════════════════════════

class TestDocumentationSkip:
    """Documentation files must not trigger false positives."""

    def test_markdown_example_keys_not_flagged(self):
        """.md files must be skipped even if they contain example secrets."""
        matches = _scan_file("tests/fixtures/secrets/negative_documentation.md")
        assert len(matches) == 0

    def test_docs_directory_skipped(self):
        """Files under docs/ must be skipped."""
        from engine.guard_manager import GuardManager
        import yaml

        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run(["git", "init", "-q"], cwd=tmp, capture_output=True)
            subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=tmp, capture_output=True)
            subprocess.run(["git", "config", "user.name", "test"], cwd=tmp, capture_output=True)

            os.makedirs(os.path.join(tmp, "docs"), exist_ok=True)
            os.makedirs(os.path.join(tmp, ".gitreins"), exist_ok=True)
            with open(os.path.join(tmp, ".gitreins", "config.yaml"), "w") as f:
                yaml.dump({"guards": {"secrets": True, "lint": False, "tests": False}}, f)

            fpath = os.path.join(tmp, "docs", "keys.md")
            with open(fpath, "w") as f:
                f.write('API_KEY = "ghp_abc123def456ghi789jkl012mno345pqr678stu"')
            subprocess.run(["git", "add", "docs/keys.md"], cwd=tmp, capture_output=True)

            gm = GuardManager(tmp)
            result = gm._builtin_secrets_scan()

            assert result.passed, "docs/ should be skipped — no secrets flagged"
            assert "ghp_" not in result.output

    def test_memory_bank_directory_skipped(self):
        """Files under .memory-bank/ must be skipped."""
        from engine.guard_manager import GuardManager
        import yaml

        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run(["git", "init", "-q"], cwd=tmp, capture_output=True)
            subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=tmp, capture_output=True)
            subprocess.run(["git", "config", "user.name", "test"], cwd=tmp, capture_output=True)

            os.makedirs(os.path.join(tmp, ".memory-bank"), exist_ok=True)
            os.makedirs(os.path.join(tmp, ".gitreins"), exist_ok=True)
            with open(os.path.join(tmp, ".gitreins", "config.yaml"), "w") as f:
                yaml.dump({"guards": {"secrets": True, "lint": False, "tests": False}}, f)

            fpath = os.path.join(tmp, ".memory-bank", "secrets.md")
            with open(fpath, "w") as f:
                f.write('API_KEY = "ghp_abc123def456ghi789jkl012mno345pqr678stu"')
            subprocess.run(["git", "add", ".memory-bank/secrets.md"], cwd=tmp, capture_output=True)

            gm = GuardManager(tmp)
            result = gm._builtin_secrets_scan()

            assert result.passed, ".memory-bank/ should be skipped — no secrets flagged"
            assert "ghp_" not in result.output


# ══════════════════════════════════════════════════════════════════
#   FIXTURE FILE TESTS — scan real fixture files
# ══════════════════════════════════════════════════════════════════

class TestFixtureFiles:
    """Positive fixture files must produce findings; negative must not."""

    def test_positive_aws_caught(self):
        matches = _scan_file("tests/fixtures/secrets/positive_aws.py")
        assert _any_match(matches, "AWS access key", "AWS secret access key")

    def test_positive_stripe_caught(self):
        matches = _scan_file("tests/fixtures/secrets/positive_stripe.py")
        assert _any_match(matches, "Stripe live secret key", "Stripe restricted key")

    def test_positive_github_caught(self):
        matches = _scan_file("tests/fixtures/secrets/positive_github.py")
        assert _any_match(matches, "GitHub personal access token")

    def test_positive_gitlab_caught(self):
        matches = _scan_file("tests/fixtures/secrets/positive_gitlab.py")
        assert _any_match(matches, "GitLab personal access token")

    def test_positive_openai_caught(self):
        matches = _scan_file("tests/fixtures/secrets/positive_openai.py")
        assert _any_match(matches, "OpenAI/OpenRouter API key", "hardcoded API key")

    def test_positive_openrouter_caught(self):
        matches = _scan_file("tests/fixtures/secrets/positive_openrouter.py")
        assert _any_match(matches, "OpenAI/OpenRouter API key", "hardcoded API key")

    def test_positive_slack_caught(self):
        matches = _scan_file("tests/fixtures/secrets/positive_slack.py")
        assert _any_match(matches, "Slack API token")

    def test_positive_gcp_caught(self):
        matches = _scan_file("tests/fixtures/secrets/positive_gcp.py")
        assert _any_match(matches, "GCP API key", "hardcoded API key")

    def test_positive_private_keys_caught(self):
        matches = _scan_file("tests/fixtures/secrets/positive_private_keys.py")
        assert _any_match(matches, "private key block")

    def test_positive_passwords_caught(self):
        matches = _scan_file("tests/fixtures/secrets/positive_passwords.py")
        assert _any_match(matches, "hardcoded password")

    def test_positive_jwt_caught(self):
        matches = _scan_file("tests/fixtures/secrets/positive_jwt.py")
        assert _any_match(matches, "hardcoded JWT")

    def test_negative_os_getenv_clean(self):
        matches = _scan_file("tests/fixtures/secrets/negative_os_getenv.py")
        assert len(matches) == 0

    def test_negative_jwt_encode_clean(self):
        matches = _scan_file("tests/fixtures/secrets/negative_jwt_encode.py")
        assert len(matches) == 0

    def test_negative_shell_vars_clean(self):
        matches = _scan_file("tests/fixtures/secrets/negative_shell_vars.py")
        assert len(matches) == 0

    def test_negative_template_vars_clean(self):
        matches = _scan_file("tests/fixtures/secrets/negative_template_vars.py")
        assert len(matches) == 0

    def test_negative_placeholders_clean(self):
        matches = _scan_file("tests/fixtures/secrets/negative_placeholders.py")
        assert len(matches) == 0

    def test_negative_empty_passwords_clean(self):
        matches = _scan_file("tests/fixtures/secrets/negative_empty_passwords.py")
        assert len(matches) == 0

    def test_negative_documentation_clean(self):
        matches = _scan_file("tests/fixtures/secrets/negative_documentation.md")
        assert len(matches) == 0
