"""
Tests for secrets scanner — verify all leak types are detected.

New in v0.7.1: SSH ED25519/PKCS#8, AWS secret keys, GCP, DigitalOcean,
Stripe, Azure, Slack tokens.
"""

import os
import subprocess
import sys
import tempfile

import pytest

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
# Existing patterns (regression)
# ══════════════════════════════════════════════════════════════════

class TestExistingPatterns:
    def test_github_token(self):
        matches = _scan_text('GITHUB_TOKEN = "ghp_abc123def456ghi789jkl012mno345pqr678stu"')
        assert _any_match(matches, "GitHub personal access token")

    def test_openai_key(self):
        """sk-proj- key may be caught by api_key pattern (generic) or sk- pattern (specific). Both are correct."""
        matches = _scan_text('OPENAI_API_KEY = "sk-proj-abc123def456ghi789jkl012mno345pqr678stuvwxyz"')
        assert _any_match(matches, "OpenAI/OpenRouter API key", "hardcoded API key")

    def test_openrouter_key(self):
        matches = _scan_text('OPENROUTER_API_KEY = "sk-or-v1-abc123def456ghi789jkl012mno345pqr678stuvwxyz1234"')
        assert _any_match(matches, "OpenAI/OpenRouter API key", "hardcoded API key")

    def test_aws_access_key(self):
        """Use a realistic-looking access key ID (no EXAMPLE in value)."""
        matches = _scan_text('AWS_ACCESS_KEY_ID = "AKIAJG7KQ4M3N6P2R5TU"')
        assert _any_match(matches, "AWS access key")

    def test_hardcoded_jwt(self):
        matches = _scan_text('token = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"')
        assert _any_match(matches, "hardcoded JWT")

    def test_env_var_not_flagged(self):
        """os.getenv should NOT trigger secrets scanner."""
        matches = _scan_text('API_KEY = os.getenv("MY_API_KEY")')
        assert len(matches) == 0

    def test_empty_password_not_flagged(self):
        matches = _scan_text('PASSWORD = ""')
        assert len(matches) == 0

    def test_placeholder_not_flagged(self):
        matches = _scan_text('API_KEY = "sk-PLACEHOLDER-KEY-NOT-REAL-12345678901234567890"')
        assert len(matches) == 0

    def test_example_comment_not_flagged(self):
        """TODO/FIXME comments should not be flagged."""
        matches = _scan_text('# TODO: sk-add-real-key-here for testing')
        assert len(matches) == 0


# ══════════════════════════════════════════════════════════════════
# NEW patterns (v0.7.1)
# ══════════════════════════════════════════════════════════════════

class TestSSHPrivateKeys:
    def test_rsa_private_key(self):
        matches = _scan_text("""-----BEGIN RSA PRIVATE KEY-----
MIIEpAIBAAKCAQEA0Z3Yj5K7w8N2mQpL4xVfH6tR9sA1bC3dE5fG7hI9jK0L1mN2oP
-----END RSA PRIVATE KEY-----""")
        assert _any_match(matches, "private key block")

    def test_openssh_private_key(self):
        matches = _scan_text("""-----BEGIN OPENSSH PRIVATE KEY-----
b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAABAAABFwAAAAdzc2gtcn
-----END OPENSSH PRIVATE KEY-----""")
        assert _any_match(matches, "private key block")

    def test_pkcs8_private_key(self):
        """PKCS#8 generic format (used by ED25519): -----BEGIN PRIVATE KEY-----"""
        matches = _scan_text("""-----BEGIN PRIVATE KEY-----
MC4CAQAwBQYDK2VwBCIEIJ3tNqRx7Bm5LfHk8Ys2Dc0WvPqR4Sa6TbU9Ve0XfGhK
-----END PRIVATE KEY-----""")
        assert _any_match(matches, "private key block")

    def test_encrypted_private_key(self):
        matches = _scan_text("""-----BEGIN ENCRYPTED PRIVATE KEY-----
MIIFHDBOBgkqhkiG9w0BBQ0wQTApBgkqhkiG9w0BBQwwHAQIgV7nY2FxR9K0LoMCAggA
-----END ENCRYPTED PRIVATE KEY-----""")
        assert _any_match(matches, "private key block")

    def test_pgp_private_key(self):
        matches = _scan_text("""-----BEGIN PGP PRIVATE KEY BLOCK-----
lQdGBGcX9hEBEAC8Nq3k5J7mP2sV0wX4yB6cR8tU1nA3dF5hG9iK2lM4oQ6rS7vW0xZ
-----END PGP PRIVATE KEY BLOCK-----""")
        assert _any_match(matches, "private key block")


class TestAWSSecretKeys:
    def test_aws_secret_key(self):
        matches = _scan_text('AWS_SECRET_ACCESS_KEY = "wJalrXUtnFEMIK7MDENGbPxRfiCYZ9a4pQ3sT0vK2nL5mH8rD1wF6xJ3"')
        assert _any_match(matches, "AWS secret access key")

    def test_aws_secret_key_underscore_variant(self):
        matches = _scan_text('aws_secret = "wJalrXUtnFEMIK7MDENGbPxRfiCYZ9a4pQ3sT0vK2nL5mH8rD1wF6xJ3"')
        assert _any_match(matches, "AWS secret access key")

    def test_aws_secret_in_yaml(self):
        matches = _scan_text('secret_access_key: "wJalrXUtnFEMIK7MDENGbPxRfiCYZ9a4pQ3sT0vK2nL5mH8rD1wF6xJ3"')
        assert _any_match(matches, "AWS secret access key")


class TestGCPKeys:
    def test_gcp_api_key(self):
        matches = _scan_text('GCP_API_KEY = "AIzaSyD4i8HrK2mN9pQ5sT0vW1xF6jL3aB7cE9dG0fI4k"')
        assert _any_match(matches, "GCP API key")

    def test_gcp_key_in_config(self):
        matches = _scan_text('api_key: "AIzaSyD4i8HrK2mN9pQ5sT0vW1xF6jL3aB7cE9dG0fI4k"')
        assert _any_match(matches, "GCP API key", "hardcoded API key")


class TestDigitalOcean:
    def test_do_token(self):
        matches = _scan_text('DO_TOKEN = "dop_v1_abc123def456ghi789jkl012mno345pqr678stuvwxyz9012abcdef3456ghij7890klmn"')
        assert _any_match(matches, "DigitalOcean access token")


class TestStripe:
    def test_stripe_live_secret(self):
        # Build key at runtime to avoid literal 'sk_live_' in source (GitHub push protection)
        prefix = "sk_" + "live_"
        fake_key = prefix + "NOTAREALKEY000000000000000000000000000000000000"
        matches = _scan_text(f'STRIPE_SECRET_KEY = "{fake_key}"')
        assert _any_match(matches, "Stripe live secret key")

    def test_stripe_restricted_key(self):
        prefix = "rk_" + "live_"
        fake_key = prefix + "NOTAREALKEY000000000000000000000000000000000000"
        matches = _scan_text(f'STRIPE_KEY = "{fake_key}"')
        assert _any_match(matches, "Stripe restricted key")

    def test_stripe_test_key_not_caught(self):
        """Stripe test keys (sk_test_) are NOT secrets."""
        matches = _scan_text('STRIPE_KEY = "sk_test_51H0AbcDefGhijKlMnOpQrStUvWxYz1234567890AbCd"')
        assert not _any_match(matches, "Stripe live secret key", "Stripe restricted key")


class TestAzure:
    def test_azure_connection_string(self):
        matches = _scan_text('CONN_STR = "DefaultEndpointsProtocol=https;AccountName=mystorage;AccountKey=abcdefghijklmnopqrstuvwxyz1234567890ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890=="')
        assert _any_match(matches, "Azure storage connection string")

    def test_azure_account_key(self):
        matches = _scan_text('AccountKey = "abcdefghijklmnopqrstuvwxyz1234567890ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890=="')
        assert _any_match(matches, "Azure storage account key")


class TestSlack:
    def test_slack_bot_token(self):
        # Build key at runtime to avoid literal 'xoxb-' in source (GitHub push protection)
        prefix = "xox" + "b-"
        fake_token = prefix + "NOTAREAL-NOTAREAL-NOTAREALsynthetic0000000000000000"
        matches = _scan_text(f'SLACK_TOKEN = "{fake_token}"')
        assert _any_match(matches, "Slack API token")

    def test_slack_user_token(self):
        prefix = "xox" + "p-"
        fake_token = prefix + "NOTAREAL-NOTAREAL-NOTAREALsynthetic0000000000000000"
        matches = _scan_text(f'token = "{fake_token}"')
        assert _any_match(matches, "Slack API token")
