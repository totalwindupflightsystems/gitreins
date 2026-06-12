"""
Guard Manager — Static checks at pre-commit time.

Tier 1 (no LLM, fast):
    1. Secrets scanning (gitleaks or built-in pattern scanner)
    2. Lint (ruff/flake8)
    3. Staged tests (pytest on changed files)

All checks are optional and configurable via .gitreins/config.yaml
"""

import logging
import os
import re
import subprocess
from dataclasses import dataclass, field

logger = logging.getLogger("gitreins.guard")


@dataclass
class GuardResult:
    name: str
    passed: bool
    output: str = ""
    error: str = ""


@dataclass
class Tier1Result:
    passed: bool
    results: list[GuardResult] = field(default_factory=list)

    @property
    def summary(self) -> str:
        lines = []
        for r in self.results:
            status = "✓" if r.passed else "✗"
            lines.append(f"  {status} {r.name}")
        return "\n".join(lines)


class GuardManager:
    """Run static checks against staged changes."""

    def __init__(self, workdir: str = ".", config: dict | None = None):
        self.workdir = os.path.abspath(workdir)
        self.config = config or {}
        self._enabled = {
            "secrets": self.config.get("guards", {}).get("secrets", True),
            "lint": self.config.get("guards", {}).get("lint", True),
            "tests": self.config.get("guards", {}).get("tests", True),
        }

    def run_all(self) -> Tier1Result:
        """Run all enabled Tier 1 guards."""
        results: list[GuardResult] = []

        if self._enabled["secrets"]:
            results.append(self._check_secrets())

        if self._enabled["lint"]:
            results.append(self._check_lint())

        if self._enabled["tests"]:
            results.append(self._check_tests())

        passed = all(r.passed for r in results)
        return Tier1Result(passed=passed, results=results)

    def _check_secrets(self) -> GuardResult:
        """Scan staged changes for secrets using gitleaks or built-in scanner."""
        # Try gitleaks first
        try:
            result = subprocess.run(
                ["gitleaks", "detect", "--source", self.workdir, "--no-git", "--verbose"],
                capture_output=True, text=True, timeout=30,
                cwd=self.workdir,
            )
            if result.returncode == 0:
                return GuardResult(name="secrets", passed=True, output="gitleaks: clean")
            else:
                return GuardResult(name="secrets", passed=False, output=result.stdout + result.stderr)
        except FileNotFoundError:
            logger.debug("gitleaks not found — using built-in scanner")
        except Exception as e:
            logger.warning("gitleaks failed: %s — falling back to built-in scanner", e)

        return self._builtin_secrets_scan()

    def _builtin_secrets_scan(self) -> GuardResult:
        """
        Built-in secrets scanner with whitelist patterns.

        Detects likely secrets (API keys, tokens, private keys) while
        ignoring common false positives like environment variable loading,
        form field access, and credential construction.
        """
        # Patterns that LIKELY represent actual secrets (high confidence)
        danger_patterns = [
            # Literal-looking API keys assigned as values
            (r'(?i)(api[_-]?key|apikey)\s*[:=]\s*["\']([A-Za-z0-9_\-]{20,})["\']', "hardcoded API key"),
            # Private key blocks
            (r'(?i)-----BEGIN\s+(RSA|DSA|EC|OPENSSH|PGP)\s+PRIVATE\s+KEY', "private key block"),
            # GitHub tokens (literal-looking)
            (r'ghp_[A-Za-z0-9]{36,}', "GitHub personal access token"),
            (r'gho_[A-Za-z0-9]{36,}', "GitHub OAuth token"),
            # GitLab tokens
            (r'glpat-[A-Za-z0-9_\-]{20,}', "GitLab personal access token"),
            # OpenAI keys (literal)
            (r'sk-[A-Za-z0-9]{32,}', "OpenAI API key"),
            # AWS keys (literal)
            (r'(?i)AKIA[0-9A-Z]{16}', "AWS access key"),
            # JWTs assigned as literal strings
            (r'(?i)(token|jwt)\s*[:=]\s*["\']eyJ[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{10,}["\']', "hardcoded JWT"),
            # Passwords with literal-looking values (not function calls or env vars)
            (r'(?i)(password|passwd)\s*[:=]\s*["\'][^"\'$]{8,}["\']', "hardcoded password"),
            # Generic tokens with high-entropy looking values
            (r'(?i)(secret|token)\s*[:=]\s*["\'][A-Za-z0-9+/=]{32,}["\']', "hardcoded secret"),
        ]

        # Patterns we explicitly IGNORE (common false positives)
        whitelist_patterns = [
            r'(?i)(api[_-]?key|apikey|secret|token|password|passwd)\s*[:=]\s*(os\.getenv|os\.environ|getenv|environ\[|request\.form|request\.args|\.env|config\[|settings\[)',
            r'(?i)\$\{[A-Z_]+}',                     # Shell variable substitution
            r'(?i)\{\{[^}]*\}\}',                     # Template variables
            r'(?i)PASSWORD\s*=\s*""',                 # Empty password
            r'(?i)EXAMPLE|PLACEHOLDER|TODO|FIXME|xxx+',  # Placeholders
            r'(?i)jwt\.encode|jwt\.decode|b64encode', # JWT construction, not hardcoded
            r'(?i)generate|random|uuid|hash',         # Generated values
        ]

        findings = []
        try:
            # Get staged files
            result = subprocess.run(
                ["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"],
                capture_output=True, text=True, timeout=10,
                cwd=self.workdir,
            )
            staged_files = [f for f in result.stdout.strip().split("\n") if f]

            if not staged_files:
                return GuardResult(name="secrets", passed=True, output="No staged files to scan")

            for fpath in staged_files:
                full = os.path.join(self.workdir, fpath)
                if not os.path.isfile(full):
                    continue
                if os.path.getsize(full) > 1_000_000:
                    continue  # Skip very large files

                try:
                    with open(full, "r", errors="replace") as f:
                        lines = f.readlines()
                except Exception:
                    continue

                for i, line in enumerate(lines, 1):
                    # Skip whitelisted lines
                    if any(re.search(wp, line) for wp in whitelist_patterns):
                        continue

                    # Check danger patterns
                    for pattern, label in danger_patterns:
                        if re.search(pattern, line):
                            # Suppress the actual value in output
                            sanitized = re.sub(r'["\'][^"\']{6,}["\']', '"***"', line.rstrip())
                            findings.append(f"{fpath}:{i}: [{label}] {sanitized}")
                            break  # One finding per line

            if findings:
                logger.warning("Secrets scan: %d potential findings", len(findings))
                return GuardResult(
                    name="secrets",
                    passed=False,
                    output="Potential secrets found:\n" + "\n".join(findings[:20]),
                )
            return GuardResult(name="secrets", passed=True, output=f"Scanned {len(staged_files)} files — clean")

        except Exception as e:
            logger.exception("Secrets scan failed")
            return GuardResult(name="secrets", passed=False, error=str(e))

    def _check_lint(self) -> GuardResult:
        """Run linter on staged Python files."""
        linters = ["ruff", "flake8"]
        for linter in linters:
            try:
                # Get staged Python files
                result = subprocess.run(
                    ["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"],
                    capture_output=True, text=True, timeout=10,
                    cwd=self.workdir,
                )
                py_files = [f for f in result.stdout.strip().split("\n") if f.endswith(".py")]
                if not py_files:
                    return GuardResult(name="lint", passed=True, output="No Python files staged")

                lint_result = subprocess.run(
                    [linter, "check", *py_files] if linter == "ruff" else [linter, *py_files],
                    capture_output=True, text=True, timeout=30,
                    cwd=self.workdir,
                )
                output = lint_result.stdout + lint_result.stderr
                if len(output) > 2000:
                    output = output[:2000] + "\n... [truncated]"

                if lint_result.returncode == 0:
                    return GuardResult(name="lint", passed=True, output=f"{linter}: clean")
                else:
                    return GuardResult(name="lint", passed=False, output=output)
            except FileNotFoundError:
                continue

        return GuardResult(name="lint", passed=True, output="No linter found — skipped")

    def _check_tests(self) -> GuardResult:
        """Run tests related to staged changes."""
        try:
            subprocess.run(["pytest", "--version"], capture_output=True, timeout=5)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return GuardResult(name="tests", passed=True, output="pytest not found — skipped")

        cmd = self.config.get("guards", {}).get("test_command", "pytest -x --tb=short")
        try:
            result = subprocess.run(
                cmd, shell=True, capture_output=True, text=True, timeout=120,
                cwd=self.workdir,
            )
            output = result.stdout + result.stderr
            if len(output) > 2000:
                output = output[-2000:]  # Keep last 2000 chars for failure context
            if result.returncode == 0:
                return GuardResult(name="tests", passed=True, output=output[:500])
            else:
                return GuardResult(name="tests", passed=False, output=output)
        except subprocess.TimeoutExpired:
            return GuardResult(name="tests", passed=False, output="Tests timed out after 120s")
        except Exception as e:
            return GuardResult(name="tests", passed=False, error=str(e))
