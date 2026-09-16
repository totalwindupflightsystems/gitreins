"""
Tests for secrets scanner — verify all leak types are detected.

New in v0.7.1: SSH ED25519/PKCS#8, AWS secret keys, GCP, DigitalOcean,
Stripe, Azure, Slack tokens.
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile

import pytest


sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.guard_manager import GuardManager


def _scan_text(text: str) -> list[tuple[str, str]]:
    """Run the built-in scanner against a single string of text. Returns list of (label, match) tuples."""
    from engine.guard_manager import GuardManager
    import yaml

    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run(["git", "init", "-q"], cwd=tmp, capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "test@test.com"], cwd=tmp, capture_output=True
        )
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
        matches = _scan_text(
            'OPENAI_API_KEY = "sk-proj-abc123def456ghi789jkl012mno345pqr678stuvwxyz"'
        )
        assert _any_match(matches, "OpenAI/OpenRouter API key", "hardcoded API key")

    def test_openrouter_key(self):
        matches = _scan_text(
            'OPENROUTER_API_KEY = "sk-or-v1-abc123def456ghi789jkl012mno345pqr678stuvwxyz1234"'
        )
        assert _any_match(matches, "OpenAI/OpenRouter API key", "hardcoded API key")

    def test_aws_access_key(self):
        """Use a realistic-looking access key ID (no EXAMPLE in value)."""
        # Built at runtime to avoid gitleaks false positive on synthetic test key.
        p1 = "AK"
        p2 = "IA"
        s = "JG7KQ4M3N6P2R5TU"
        fake_key = p1 + p2 + s
        matches = _scan_text(f'AWS_ACCESS_KEY_ID = "{fake_key}"')
        assert _any_match(matches, "AWS access key")

    def test_hardcoded_jwt(self):
        matches = _scan_text(
            'token = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"'
        )
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
        matches = _scan_text("# TODO: sk-add-real-key-here for testing")
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
        matches = _scan_text(
            'AWS_SECRET_ACCESS_KEY = "wJalrXUtnFEMIK7MDENGbPxRfiCYZ9a4pQ3sT0vK2nL5mH8rD1wF6xJ3"'
        )
        assert _any_match(matches, "AWS secret access key")

    def test_aws_secret_key_underscore_variant(self):
        matches = _scan_text(
            'aws_secret = "wJalrXUtnFEMIK7MDENGbPxRfiCYZ9a4pQ3sT0vK2nL5mH8rD1wF6xJ3"'
        )
        assert _any_match(matches, "AWS secret access key")

    def test_aws_secret_in_yaml(self):
        matches = _scan_text(
            'secret_access_key: "wJalrXUtnFEMIK7MDENGbPxRfiCYZ9a4pQ3sT0vK2nL5mH8rD1wF6xJ3"'
        )
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
        matches = _scan_text(
            'DO_TOKEN = "dop_v1_abc123def456ghi789jkl012mno345pqr678stuvwxyz9012abcdef3456ghij7890klmn"'
        )
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
        matches = _scan_text(
            'CONN_STR = "DefaultEndpointsProtocol=https;AccountName=mystorage;AccountKey=abcdefghijklmnopqrstuvwxyz1234567890ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890=="'
        )
        assert _any_match(matches, "Azure storage connection string")

    def test_azure_account_key(self):
        matches = _scan_text(
            'AccountKey = "abcdefghijklmnopqrstuvwxyz1234567890ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890=="'
        )
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


# ══════════════════════════════════════════════════════════════════
# GR-GAP-039: venv-like dirs pruned from the judge workdir scan
# ══════════════════════════════════════════════════════════════════


def _write_workdir_file(workdir: str, relpath: str, content: str) -> str:
    """Write a file under the workdir, creating parent dirs. Returns full path."""
    full = os.path.join(workdir, relpath)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w") as f:
        f.write(content)
    return full


class TestVenvDirExclusion:
    """The judge's whole-workdir secrets scan (staged_only=False, DF-012) must
    not report findings from vendored venv/site-packages code.

    On docs-only commit 690a389 the workdir walk entered
    .venv312/lib/python3.12/site-packages/... and tripped danger patterns
    in vendored third-party code (jedi RECORD lines with AKIA + sha256 hex,
    cryptography private-key markers, pydantic/starlette/httpx example
    password literals) — 7 false positives while gitleaks was clean.
    Any .venv*/venv* dir must be pruned, plus site-packages/dist-packages
    as a belt-and-braces for oddly-named venvs.
    """

    # Fixture content mimicking vendored third-party code: every line below
    # trips a built-in danger pattern if the file is scanned.
    VENDORED = (
        "AKIAABCDEFGHIJKLMNOP\t"
        "4a5b6c7d8e9f0a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1d2e3f4\n"
        'password = "supersecret123"\n'
        "-----BEGIN RSA PRIVATE KEY-----\n"
    )

    def test_workdir_files_excludes_dotted_venv_dir(self, tmp_workdir):
        """.venv312/lib/python3.12/site-packages/<pkg>/mod.py is not enumerated."""
        _write_workdir_file(
            tmp_workdir,
            ".venv312/lib/python3.12/site-packages/jedi/mod.py",
            self.VENDORED,
        )
        _write_workdir_file(tmp_workdir, "src/app.py", "x = 1\n")

        gm = GuardManager(tmp_workdir)
        files = gm._workdir_files()

        assert not any(f.startswith(".venv312") for f in files)
        assert "src/app.py" in files

    def test_workdir_files_excludes_plain_venv_dir(self, tmp_workdir):
        """A plain 'venv' dir (no dot prefix) is also pruned."""
        _write_workdir_file(
            tmp_workdir,
            "venv/lib/python3.12/site-packages/cryptography/mod.py",
            self.VENDORED,
        )
        _write_workdir_file(tmp_workdir, "main.py", "x = 1\n")

        gm = GuardManager(tmp_workdir)
        files = gm._workdir_files()

        assert not any(f.startswith("venv/") for f in files)
        assert "main.py" in files

    def test_workdir_files_excludes_any_venv_prefix(self, tmp_workdir):
        """venv311, .venvs, etc. all match the prefix filter."""
        for venv in ("venv311", ".venvs", "venvs", ".venv312"):
            _write_workdir_file(
                tmp_workdir,
                f"{venv}/lib/python3.12/site-packages/pkg/mod.py",
                self.VENDORED,
            )
        _write_workdir_file(tmp_workdir, "src/app.py", "x = 1\n")

        gm = GuardManager(tmp_workdir)
        files = gm._workdir_files()

        assert files == ["src/app.py"]

    def test_workdir_files_excludes_site_and_dist_packages(self, tmp_workdir):
        """Belt-and-braces: site-packages/dist-packages are pruned even under
        an unusual venv root name."""
        _write_workdir_file(
            tmp_workdir,
            "pyenv312/lib/python3.12/site-packages/httpx/mod.py",
            self.VENDORED,
        )
        _write_workdir_file(
            tmp_workdir,
            "pyenv312/lib/python3.12/dist-packages/starlette/mod.py",
            self.VENDORED,
        )
        _write_workdir_file(tmp_workdir, "src/app.py", "x = 1\n")

        gm = GuardManager(tmp_workdir)
        files = gm._workdir_files()

        assert files == ["src/app.py"]

    def test_workdir_scan_clean_with_vendored_venv(self, tmp_workdir):
        """staged_only=False scan passes when findings exist only under a venv."""
        _write_workdir_file(
            tmp_workdir,
            ".venv312/lib/python3.12/site-packages/jedi/mod.py",
            self.VENDORED,
        )
        _write_workdir_file(tmp_workdir, "src/app.py", "x = 1\n")

        gm = GuardManager(tmp_workdir)
        result = gm._builtin_secrets_scan(staged_only=False)

        assert result.passed is True
        assert "clean" in result.output

    def test_workdir_scan_still_flags_finding_outside_venv(self, tmp_workdir):
        """Over-skip guard: a genuine finding in normal source still trips."""
        _write_workdir_file(
            tmp_workdir,
            ".venv312/lib/python3.12/site-packages/jedi/mod.py",
            self.VENDORED,
        )
        _write_workdir_file(tmp_workdir, "src/app.py", 'aws_key = "AKIAABCDEFGHIJKLMNOP"\n')

        gm = GuardManager(tmp_workdir)
        result = gm._builtin_secrets_scan(staged_only=False)

        assert result.passed is False
        assert "AWS access key" in result.output
        assert ".venv312" not in result.output


# ══════════════════════════════════════════════════════════════════
# POC-17 / TRUST-002: the harness's own state dir is never graded
# ══════════════════════════════════════════════════════════════════
#
# `.gitreins/` holds the harness's config, verdict history, guard run logs
# and disposable-worktree bookkeeping. Guard logs persist raw scanner output
# (DF-018) and history artifacts embed prior evidence, so a scan of that
# directory fails on canary/fixture tokens that are NOT in the repo's code
# and cannot be removed from a failing tree. Tick 285 lost a diagnosis cycle
# to exactly that (tier1 `secrets` FAIL while every source file was clean).

# Built this way so the literal token never sits in the repo's own source
# (GitHub push protection / the scanner's own fixtures).
HARNESS_CANARY = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"


def _git_repo(root: str) -> str:
    """Turn *root* into a real git repo (staged-file scans need an index)."""
    os.makedirs(root, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True, capture_output=True)
    for key, val in (("user.email", "test@test.com"), ("user.name", "test")):
        subprocess.run(["git", "config", key, val], cwd=root, capture_output=True)
    return root


def _tier1_secrets_command(workdir: str) -> str:
    """The exact shell the judge's Tier 1 secrets step runs."""
    from engine.pipeline import tier1_plan

    steps, _marker = tier1_plan(workdir, {})
    return next(s["run"] for s in steps if s["id"] == "secrets")


class TestHarnessStateExcludedFromBuiltinScan:
    """Criterion 1: the builtin workdir scan skips `.gitreins/**`."""

    def test_workdir_files_prunes_all_harness_state(self, tmp_workdir):
        """Config, logs and history are all pruned from the enumeration."""
        _write_workdir_file(tmp_workdir, ".gitreins/config.yaml", "guards:\n  secrets: true\n")
        _write_workdir_file(tmp_workdir, ".gitreins/logs/guard-1.log", HARNESS_CANARY)
        _write_workdir_file(
            tmp_workdir, ".gitreins/history/2026-09-16/abcd/verdict.json", HARNESS_CANARY
        )
        _write_workdir_file(tmp_workdir, ".gitreins/disposable.json", '{"id": "x"}\n')
        _write_workdir_file(tmp_workdir, "src/app.py", "x = 1\n")

        gm = GuardManager(tmp_workdir)
        files = gm._workdir_files()

        assert not any(f.startswith(".gitreins/") for f in files)
        assert files == ["src/app.py"]

    def test_workdir_scan_ignores_canary_in_harness_state(self, tmp_workdir):
        """Canary in `.gitreins/logs/x.log` does NOT fail the scan, and the
        evidence names the exclusion (criterion 3)."""
        _write_workdir_file(
            tmp_workdir, ".gitreins/logs/guard-1.log", f"fixture = {HARNESS_CANARY}\n"
        )
        _write_workdir_file(tmp_workdir, "src/app.py", "x = 1\n")

        gm = GuardManager(tmp_workdir)
        result = gm._builtin_secrets_scan(staged_only=False)

        assert result.passed is True
        assert ".gitreins/**" in result.output
        assert "excluded harness state" in result.output

    def test_workdir_scan_still_flags_same_canary_in_source(self, tmp_workdir):
        """Criterion 2 (MUST half): the identical canary in a source file
        still fails — the exclusion is scoped, not a blanket relaxation."""
        _write_workdir_file(
            tmp_workdir, ".gitreins/logs/guard-1.log", f"fixture = {HARNESS_CANARY}\n"
        )
        _write_workdir_file(tmp_workdir, "src/app.py", f"token = {HARNESS_CANARY}\n")

        gm = GuardManager(tmp_workdir)
        result = gm._builtin_secrets_scan(staged_only=False)

        assert result.passed is False
        assert "src/app.py" in result.output
        assert ".gitreins" not in result.output

    def test_staged_scan_ignores_tracked_gitreins_state(self, tmp_path):
        """`.gitreins/config.yaml` and `history/` are TRACKED in a real repo,
        so the staged path needs the same exclusion."""
        repo = _git_repo(str(tmp_path / "repo"))
        os.makedirs(os.path.join(repo, ".gitreins", "history"))
        _write_workdir_file(repo, ".gitreins/config.yaml", f"note: {HARNESS_CANARY}\n")
        _write_workdir_file(repo, "src/app.py", "x = 1\n")
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)

        gm = GuardManager(repo)
        assert gm._builtin_secrets_scan().passed is True

        _write_workdir_file(repo, "src/app.py", f"token = {HARNESS_CANARY}\n")
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
        staged = GuardManager(repo)._builtin_secrets_scan()
        assert staged.passed is False
        assert "src/app.py" in staged.output


class TestTier1SecretsStepHarnessScope:
    """Criterion 2 end-to-end: the JUDGE's secrets step, both scanners."""

    def _make_repo(self, root: str) -> str:
        repo = _git_repo(root)
        _write_workdir_file(repo, ".gitreins/logs/guard-1.log", f"fixture = {HARNESS_CANARY}\n")
        _write_workdir_file(repo, "src/app.py", "def add(a, b):\n    return a + b\n")
        return repo

    def test_step_passes_with_canary_only_in_harness_state(self, tmp_path):
        repo = self._make_repo(str(tmp_path / "repo"))

        proc = subprocess.run(
            _tier1_secrets_command(repo), shell=True, cwd=repo, capture_output=True, text=True
        )

        assert proc.returncode == 0, proc.stdout + proc.stderr
        # Criterion 3: the exclusion is part of the step's evidence.
        assert ".gitreins/**" in proc.stdout

    def test_step_fails_when_same_canary_reaches_a_source_file(self, tmp_path):
        repo = self._make_repo(str(tmp_path / "repo"))
        _write_workdir_file(repo, "src/app.py", f"token = {HARNESS_CANARY}\n")

        proc = subprocess.run(
            _tier1_secrets_command(repo), shell=True, cwd=repo, capture_output=True, text=True
        )

        assert proc.returncode != 0
        assert "src/app.py" in proc.stdout


class TestTier1SecretsStepNamesScanners:
    """TRUST-003: the JUDGE's secrets step says which scanners it used.

    The step is a shell script, so the attribution is echoed into the step
    output that reaches tier1 evidence and verdict.json — a judge FAIL on
    `secrets` no longer leaves "secrets" ambiguous.
    """

    def test_step_output_names_the_scanner_set(self, tmp_path):
        repo = _git_repo(str(tmp_path / "repo"))
        _write_workdir_file(repo, "src/app.py", "def add(a, b):\n    return a + b\n")

        proc = subprocess.run(
            _tier1_secrets_command(repo), shell=True, cwd=repo, capture_output=True, text=True
        )

        assert proc.returncode == 0, proc.stdout + proc.stderr
        # The built-in cross-check always runs, so its line is always present...
        assert "secrets: builtin cross-check status: clean" in proc.stdout
        # ...and the scanner set is named, with or without gitleaks installed.
        assert "secrets: scanners=" in proc.stdout
        assert re.search(
            r"secrets: scanners=(gitleaks\+builtin cross-check|builtin cross-check only)",
            proc.stdout,
        ), proc.stdout
        assert re.search(r"secrets: gitleaks: (clean|not on PATH)", proc.stdout), proc.stdout

    def test_step_output_names_the_offending_scanner_on_failure(self, tmp_path):
        repo = _git_repo(str(tmp_path / "repo"))
        _write_workdir_file(repo, "src/app.py", f"token = {HARNESS_CANARY}\n")

        proc = subprocess.run(
            _tier1_secrets_command(repo), shell=True, cwd=repo, capture_output=True, text=True
        )

        assert proc.returncode != 0
        assert "secrets: builtin cross-check status: 1 finding" in proc.stdout


class TestGitleaksHarnessExclusionConfig:
    """The generated gitleaks config is what keeps `--no-git` off harness
    state; gitleaks version present → also prove it live."""

    def test_config_extends_repo_config_when_present(self, tmp_path):
        from engine.pipeline import harness_scan_gitleaks_config

        repo = str(tmp_path / "repo")
        os.makedirs(repo)
        _write_workdir_file(repo, ".gitleaks.toml", "[extend]\nuseDefault = true\n")

        cfg = harness_scan_gitleaks_config(repo)

        assert f"path = '{os.path.join(repo, '.gitleaks.toml')}'" in cfg
        assert "useDefault" not in cfg
        assert r"(^|/)\.gitreins/.*" in cfg

    def test_config_falls_back_to_default_ruleset(self, tmp_path):
        from engine.pipeline import harness_scan_gitleaks_config

        repo = str(tmp_path / "repo")
        os.makedirs(repo)

        cfg = harness_scan_gitleaks_config(repo)

        assert "path = " not in cfg
        assert "useDefault = true" in cfg
        assert r"(^|/)\.gitreins/.*" in cfg

    @pytest.mark.skipif(
        shutil.which("gitleaks") is None, reason="gitleaks not installed on this host"
    )
    def test_gitleaks_scan_scope_excludes_harness_state(self, tmp_path):
        """Bare `--no-git` flags the harness canary; the generated config
        does not — and still flags the same canary in a source file."""
        from engine.pipeline import harness_scan_gitleaks_config

        repo = _git_repo(str(tmp_path / "repo"))
        _write_workdir_file(repo, ".gitreins/logs/guard-1.log", f"fixture = {HARNESS_CANARY}\n")
        _write_workdir_file(repo, "src/app.py", "x = 1\n")

        def gl(cfg_path: str | None) -> int:
            cmd = ["gitleaks", "detect", "--source", ".", "--no-git", "--no-banner"]
            if cfg_path:
                cmd += ["--config", cfg_path]
            return subprocess.run(cmd, cwd=repo, capture_output=True, text=True).returncode

        cfg_path = os.path.join(str(tmp_path), "harness-scan.toml")
        with open(cfg_path, "w") as f:
            f.write(harness_scan_gitleaks_config(repo))

        # Pre-fix behaviour: the harness's own log alone fails the scan.
        assert gl(None) != 0
        # Scoped: clean on the same tree.
        assert gl(cfg_path) == 0
        # Still real: the same canary in source code fails.
        _write_workdir_file(repo, "src/app.py", f"token = {HARNESS_CANARY}\n")
        assert gl(cfg_path) != 0
