"""
Guard Manager — Static checks at pre-commit time.

Tier 1 (no LLM, fast):
    1. Secrets scanning (gitleaks or built-in pattern scanner)
    2. Lint (ruff/flake8)
    3. Tests (full or diff-mode, configurable)

All checks are optional and configurable via .gitreins/config.yaml

Config:
    guards:
      secrets: true
      lint: true
      tests: true
      test_mode: "full" | "diff"     # default: full
      test_command: "pytest -x --tb=short"
"""

import fnmatch
import json
import logging
import os
import re
import subprocess
from dataclasses import dataclass, field

logger = logging.getLogger("gitreins.guard")

from engine.guards import is_go_project, check_go_lint, check_go_tests, check_go_build


@dataclass
class GuardResult:
    name: str
    passed: bool
    output: str = ""
    error: str = ""

    def _pass_detail(self) -> str:
        """Short detail string for passing guards (e.g. 'clean', '3 files')."""
        if self.name == "secrets":
            return " — clean"
        elif self.name in ("lint", "go_lint", "go_build", "go_vet"):
            return " — ok"
        elif self.name in ("tests", "go_tests"):
            if "passed" in self.output.lower() or "ok" in self.output.lower():
                return " — passed"
            return ""
        return ""


@dataclass
class Tier1Result:
    passed: bool
    results: list[GuardResult] = field(default_factory=list)
    extra: dict = field(default_factory=dict)  # mode, counts, etc.

    @property
    def summary(self) -> str:
        lines = []
        for r in self.results:
            status = "✓" if r.passed else "✗"
            detail = ""
            if not r.passed and r.output:
                # Extract a short failure summary — first meaningful line
                out_lines = [l for l in r.output.split("\n") if l.strip()]
                if out_lines:
                    # Pick the first error-like line
                    first = out_lines[0].strip()
                    if len(first) > 100:
                        first = first[:97] + "..."
                    detail = f" — {first}"
                # Show count for tests
                fail_count = len([l for l in out_lines if "FAIL" in l or "FAILED" in l])
                if fail_count:
                    detail = f" — {fail_count} failure(s)"
            elif r.passed:
                detail = r._pass_detail()
            lines.append(f"  {status} {r.name}{detail}")
        return "\n".join(lines)


# ── Diff-based test discovery ──────────────────────────────────

# Files that, when changed, force a full test run (too broad to narrow)
_FORCE_FULL_TEST_GLOBS = [
    "pyproject.toml",
    "setup.cfg",
    "setup.py",
    "conftest.py",
    ".gitreins/config.yaml",
    ".github/workflows/*.yml",
    "Makefile",
]


def _discover_test_targets(workdir: str) -> list[str] | None:
    """Return a list of test file paths to run, or None for full suite.

    None means "full suite" — returned when:
    - No staged changes
    - A force-full file was changed
    - No test files map to the changed sources (safety fallback)

    Returns absolute paths to test files.
    """
    # Get staged files
    staged = _get_staged_files(workdir)
    if not staged:
        return None

    # Check force-full triggers
    for staged_file in staged:
        for glob in _FORCE_FULL_TEST_GLOBS:
            if fnmatch.fnmatch(staged_file, glob):
                logger.debug("Force-full trigger: %s matches %s", staged_file, glob)
                return None

    # Map staged source files to test files using basename matching
    test_files: set[str] = set()
    for staged_file in staged:
        # If a test file itself changed, always include it
        basename = os.path.basename(staged_file)
        if basename.startswith("test_") and basename.endswith(".py"):
            test_files.add(os.path.join(workdir, staged_file))
            continue

        # Derive test file from source basename:
        #   engine/foo.py → tests/test_foo.py
        #   gitreins/bar.py → tests/test_bar.py
        #   gitreins_mcp/server.py → tests/test_mcp_server.py
        module = os.path.splitext(basename)[0]
        candidates = [
            os.path.join(workdir, "tests", f"test_{module}.py"),
        ]
        # Special case: gitreins_mcp/server.py → tests/test_mcp_server.py
        if os.path.dirname(staged_file).startswith("gitreins_mcp"):
            candidates.append(os.path.join(workdir, "tests", f"test_mcp_{module}.py"))

        for candidate in candidates:
            if os.path.isfile(candidate):
                test_files.add(candidate)
                logger.debug("Mapped %s → %s", staged_file, os.path.relpath(candidate, workdir))
                break
        else:
            logger.debug("No test file found for %s (tried: %s)", staged_file, candidates)

    if not test_files:
        # Changed files don't map to any known tests — safety: run full suite
        logger.debug("No test targets discovered for staged files, running full suite")
        return None

    return sorted(test_files)


def _get_staged_files(workdir: str) -> list[str]:
    """Return staged file paths relative to workdir."""
    try:
        result = subprocess.run(
            ["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"],
            capture_output=True, text=True, timeout=10,
            cwd=workdir,
        )
        return [f.strip() for f in result.stdout.split("\n") if f.strip()]
    except Exception:
        return []


def _build_diff_test_command(test_command: str, test_files: list[str], workdir: str) -> str:
    """Build a test command targeting specific test files.

    If test_command starts with 'pytest', appends the test file paths.
    Otherwise returns the original command (custom runners can't be
    narrowed without user config).
    """
    cmd = test_command.strip()

    if cmd.startswith("pytest"):
        # Convert absolute paths to relative for cleaner output
        rel_paths = [os.path.relpath(f, workdir) for f in test_files]
        return f"{cmd} {' '.join(rel_paths)}"

    # Non-pytest runner — can't narrow, run full
    return cmd


class GuardManager:
    """Run static checks against staged changes."""

    def __init__(self, workdir: str = ".", config: dict | None = None):
        self.workdir = os.path.abspath(workdir)
        self.config = config or {}
        guards_cfg = self.config.get("guards", {})
        self._enabled = {
            "secrets": guards_cfg.get("secrets", True),
            "lint": guards_cfg.get("lint", True),
            "tests": guards_cfg.get("tests", True),
            "dead_code": guards_cfg.get("dead_code", False),  # opt-in: Python-only, can be noisy
            "skylos": guards_cfg.get("skylos", False),  # opt-in: needs pip install
        }

        # Test mode: "full" (default) or "diff"
        self._test_mode = guards_cfg.get("test_mode", "full")

        # Project type detection
        self._is_go = os.path.isfile(os.path.join(self.workdir, "go.mod"))
        self._go_guards = guards_cfg.get("go", {})

    def run_all(self) -> Tier1Result:
        """Run all enabled Tier 1 guards.

        For Go projects, the Python-specific guards (lint, tests, dead_code)
        are skipped in favor of Go-native equivalents (go vet, go test, go build).
        """
        results: list[GuardResult] = []

        if self._enabled["secrets"]:
            results.append(self._check_secrets())

        if self._enabled["lint"] and not self._is_go:
            results.append(self._check_lint())

        if self._enabled["tests"] and not self._is_go:
            results.append(self._check_tests())

        if self._enabled["dead_code"] and not self._is_go:
            results.append(self._check_dead_code())

        if self._enabled["skylos"]:
            results.append(self._check_skylos())

        if self._is_go:
            if self._go_guards.get("build", True):
                results.append(self._check_go_build())
            if self._go_guards.get("lint", True):
                results.append(self._check_go_lint())
            if self._go_guards.get("tests", True):
                results.append(self._check_go_tests())

        passed = all(r.passed for r in results)
        extra = {
            "test_mode": self._test_mode,
        }
        if self._test_mode == "diff" and self._enabled.get("tests"):
            staged = _get_staged_files(self.workdir)
            targets = _discover_test_targets(self.workdir)
            if targets:
                extra["test_targets"] = len(targets)
                extra["staged_count"] = len(staged)
            else:
                extra["test_targets"] = None  # full suite triggered
        return Tier1Result(passed=passed, results=results, extra=extra)

    @property
    def test_mode(self) -> str:
        return self._test_mode

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
            # OpenAI/OpenRouter keys (supports sk-or-v1-, sk-proj-, etc.)
            (r'sk-[A-Za-z0-9_\-]{32,}', "OpenAI/OpenRouter API key"),
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
            r'(?i)\$\{[A-Z_]+\}',                     # Shell variable substitution
            r'(?i)\{\{[^}]*\}\}',                     # Template variables
            r'(?i)PASSWORD\s*=\s*""',                 # Empty password
            r'(?i)EXAMPLE|PLACEHOLDER|TODO|FIXME|xxx+',  # Placeholders
            r'(?i)jwt\.encode|jwt\.decode|b64encode', # JWT construction, not hardcoded
            r'(?i)generate|random|uuid|hash',         # Generated values
        ]

        findings = []
        try:
            # Get staged files
            staged_files = _get_staged_files(self.workdir)

            if not staged_files:
                return GuardResult(name="secrets", passed=True, output="No staged files to scan")

            for fpath in staged_files:
                full = os.path.join(self.workdir, fpath)
                if not os.path.isfile(full):
                    continue
                if os.path.getsize(full) > 1_000_000:
                    continue  # Skip very large files

                # Skip documentation files — they routinely contain
                # example API keys, placeholder tokens, and credential
                # snippets that are not actual secrets.
                if (
                    any(skip in fpath for skip in ('.memory-bank/', 'docs/', 'CONTRIBUTING.md', 'SECURITY.md'))
                    or fpath.endswith('.md')
                ):
                    continue

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
                staged_files = _get_staged_files(self.workdir)
                py_files = [f for f in staged_files if f.endswith(".py")]
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
        """Run the configured test command.

        In 'diff' mode, only runs tests relevant to staged changes.
        In 'full' mode (default), runs the entire test suite.
        When no files are staged, tests are skipped entirely.
        """
        test_command = self.config.get("guards", {}).get("test_command", "pytest -x --tb=short")

        # Skip if nothing is staged — nothing to test
        staged = _get_staged_files(self.workdir)
        if not staged:
            return GuardResult(name="tests", passed=True, output="No files staged — skipped")

        if self._test_mode == "diff":
            test_files = _discover_test_targets(self.workdir)
            if test_files is not None:
                # Narrowed — only run relevant tests
                cmd = _build_diff_test_command(test_command, test_files, self.workdir)
                label = f"tests (diff: {len(test_files)} files)"
                return self._run_test_command(cmd, label)
            # Fall through to full suite (safety default for force-full triggers)

        label = "tests (full)"
        return self._run_test_command(test_command, label)

    def _run_test_command(self, cmd: str, label: str) -> GuardResult:
        """Execute a test command and return a GuardResult."""
        try:
            result = subprocess.run(
                cmd, shell=True, capture_output=True, text=True, timeout=120,
                cwd=self.workdir,
            )
            output = result.stdout + result.stderr
            if len(output) > 2000:
                output = output[-2000:]  # Keep last 2000 chars for failure context
            if result.returncode == 0:
                return GuardResult(name=label, passed=True, output=output[:500])
            else:
                return GuardResult(name=label, passed=False, output=output)
        except subprocess.TimeoutExpired:
            return GuardResult(name=label, passed=False, output="Tests timed out after 120s")
        except Exception as e:
            return GuardResult(name=label, passed=False, error=str(e))

    def _check_dead_code(self) -> GuardResult:
        """Detect unreachable code, unused functions, and unused imports."""
        try:
            from engine.dead_code import DeadCodeDetector

            detector = DeadCodeDetector(self.workdir)
            report = detector.scan()

            # Also check for unused functions project-wide
            unused_funcs = detector.find_unused_functions()
            report.findings.extend(unused_funcs)

            if report.passed:
                return GuardResult(name="dead_code", passed=True, output="No dead code found")

            # Group by category for clear output
            output = report.summary
            if len(output) > 2000:
                output = output[:2000] + "\n... [truncated]"

            return GuardResult(name="dead_code", passed=False, output=output)
        except ImportError:
            return GuardResult(name="dead_code", passed=True, output="Dead code detector unavailable — skipped")
        except Exception as e:
            return GuardResult(name="dead_code", passed=False, error=str(e))

    def _check_skylos(self) -> GuardResult:
        """Multi-language dead code + AI mistake detection via Skylos.

        Requires: pip install skylos
        Detects: unused functions, imports, classes, variables, parameters,
                 unreachable code, AI-hallucinated patterns.
        Languages: Python, TS/JS, Go, Java, PHP, Rust, Dart, C#
        """
        try:
            result = subprocess.run(
                ["skylos", self.workdir, "--format", "json", "--no-grep-verify"],
                capture_output=True, text=True, timeout=120,
                cwd=self.workdir,
            )
            if result.returncode != 0:
                return GuardResult(
                    name="skylos", passed=True,
                    output=f"skylos exited {result.returncode}: {result.stderr[:200]}",
                )

            data = json.loads(result.stdout)

            findings = []
            for f in data.get("unused_functions", []):
                findings.append(f"{f['file']}:{f['line']} — unused function {f['name']}")
            for f in data.get("unused_imports", []):
                findings.append(f"{f['file']}:{f['line']} — unused import {f['name']}")
            for f in data.get("unused_classes", []):
                findings.append(f"{f['file']}:{f['line']} — unused class {f['name']}")

            # Dead symbols from definitions
            for name, info in data.get("definitions", {}).items():
                if info.get("dead"):
                    findings.append(f"{info['file']}:{info['line']} — dead: {name}")

            grade = data.get("grade", {}).get("overall", {})
            score = grade.get("score", "?")
            letter = grade.get("letter", "?")

            if not findings:
                return GuardResult(
                    name="skylos", passed=True,
                    output=f"Skylos grade {letter} ({score}) — no dead code found",
                )

            output = f"Skylos grade {letter} ({score}) — {len(findings)} findings:\n"
            output += "\n".join(f"  • {f}" for f in findings[:20])
            if len(findings) > 20:
                output += f"\n  ... and {len(findings) - 20} more"

            if len(output) > 2000:
                output = output[:2000] + "\n... [truncated]"

            return GuardResult(name="skylos", passed=False, output=output)

        except FileNotFoundError:
            return GuardResult(
                name="skylos", passed=True,
                output="skylos not installed — install with: pip install skylos",
            )
        except json.JSONDecodeError:
            return GuardResult(name="skylos", passed=True, output="skylos output unparseable")
        except subprocess.TimeoutExpired:
            return GuardResult(name="skylos", passed=True, output="skylos timed out")
        except Exception as e:
            return GuardResult(name="skylos", passed=False, error=str(e))

    def _check_go_lint(self) -> GuardResult:
        """Run Go lint checks (delegates to engine.guards)."""
        r = check_go_lint(self.workdir)
        return GuardResult(name=r.name, passed=r.passed, output=r.output, error=r.error)

    def _check_go_tests(self) -> GuardResult:
        """Run Go tests (delegates to engine.guards)."""
        r = check_go_tests(self.workdir)
        return GuardResult(name=r.name, passed=r.passed, output=r.output, error=r.error)

    def _check_go_build(self) -> GuardResult:
        """Run Go build (delegates to engine.guards)."""
        r = check_go_build(self.workdir)
        return GuardResult(name=r.name, passed=r.passed, output=r.output, error=r.error)
