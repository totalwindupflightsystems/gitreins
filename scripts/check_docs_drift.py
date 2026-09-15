#!/usr/bin/env python3
"""Check README.md version/count drift against their authorities (GR-GAP-052).

Two checks, both fail-closed:

Check A (version): the README release banner version must equal the version
declared in pyproject.toml ([project] table).

Check B (counts, static only — pytest is never run here): every "N tests pass",
"N tests across", and "N test files" claim in README.md must carry the same N
across all mentions.

Standard library only; compatible with Python 3.10 (pyproject.toml is parsed
with a line scan, not tomllib).

Usage:
    python scripts/check_docs_drift.py [--repo-root PATH]

Exit code 0 on success (one summary line), 1 on any mismatch (specific,
actionable message naming both values).
"""

import argparse
import re
import sys
from pathlib import Path

# Matches a PEP 440-ish version string inside a **v...** bold span on the
# banner line, e.g. "**v0.12.1**" or "**v1.2.3rc1**".
_BANNER_VERSION_RE = re.compile(r"\*\*v(\d+\.\d+\.\d+(?:[^\s*]*)?)\*\*")
_PYPROJECT_VERSION_RE = re.compile(r'^version\s*=\s*"([^"]+)"\s*$')
_COUNT_RES = {
    "tests pass": re.compile(r"(\d+) tests pass"),
    "tests across": re.compile(r"(\d+) tests across"),
    "test files": re.compile(r"(\d+) test files"),
}


def resolve_repo_root(argv=None):
    """Return the repo root: --repo-root override, else scripts/'s parent."""
    parser = argparse.ArgumentParser(
        description="Check README version/count drift against pyproject.toml."
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=None,
        help="Repository root (default: parent of the scripts/ directory containing this file).",
    )
    args = parser.parse_args(argv)
    if args.repo_root is not None:
        return args.repo_root
    return Path(__file__).resolve().parent.parent


def read_pyproject_version(pyproject_path):
    """Read the [project] version from pyproject.toml. None when absent."""
    try:
        lines = pyproject_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    in_project = False
    for line in lines:
        stripped = line.strip()
        if stripped == "[project]":
            in_project = True
            continue
        if stripped.startswith("["):
            in_project = stripped == "[project]"
            continue
        if in_project:
            match = _PYPROJECT_VERSION_RE.match(stripped)
            if match:
                return match.group(1)
    return None


def read_banner_version(readme_path):
    """Read the release-banner version from README.md. None when absent."""
    try:
        lines = readme_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        if line.startswith("> ") and "**v" in line:
            match = _BANNER_VERSION_RE.search(line)
            if match:
                return match.group(1)
    return None


def collect_readme_counts(readme_path):
    """Map each count phrase to the set of N values claimed in README.md."""
    try:
        text = readme_path.read_text(encoding="utf-8")
    except OSError:
        return {}
    counts = {}
    for phrase, pattern in _COUNT_RES.items():
        counts[phrase] = sorted(set(pattern.findall(text)))
    return counts


def check_docs_drift(repo_root):
    """Run both drift checks. Return (exit_code, message)."""
    pyproject_path = repo_root / "pyproject.toml"
    readme_path = repo_root / "README.md"

    if not pyproject_path.exists():
        return 1, f"FAIL: pyproject.toml not found at {pyproject_path}"
    if not readme_path.exists():
        return 1, f"FAIL: README.md not found at {readme_path}"

    pyproject_version = read_pyproject_version(pyproject_path)
    if pyproject_version is None:
        return 1, f'FAIL: no version = "..." found in the [project] table of {pyproject_path}'

    banner_version = read_banner_version(readme_path)
    if banner_version is None:
        return (
            1,
            f"FAIL: no release banner version found in {readme_path} "
            f'(expected a line starting with "> " containing "**v<version>**")',
        )

    if banner_version != pyproject_version:
        return (
            1,
            f"FAIL: version drift — README.md banner says v{banner_version} "
            f"but pyproject.toml says {pyproject_version}. "
            f"Update the README.md release banner to v{pyproject_version}.",
        )

    counts = collect_readme_counts(readme_path)
    for phrase, values in counts.items():
        if not values:
            return 1, f"FAIL: README.md contains no '{phrase}' claim (expected at least one)."
        if len(values) > 1:
            lines = _lines_with_phrase(readme_path, phrase)
            return (
                1,
                f"FAIL: README.md test-count drift — conflicting '{phrase}' claims "
                f"({', '.join(values)}) on line(s) {lines}. "
                f"All mentions must state the same N.",
            )

    tests_n = counts["tests pass"][0]
    across_n = counts["tests across"][0]
    files_n = counts["test files"][0]
    if across_n != tests_n:
        return (
            1,
            f"FAIL: README.md test-count drift — '{across_n} tests across' "
            f"disagrees with '{tests_n} tests pass'. Both must state {tests_n}.",
        )

    return (
        0,
        f"docs drift check OK: version {pyproject_version} matches README banner; "
        f"README test counts consistent ({tests_n} tests / {files_n} test files)",
    )


def _lines_with_phrase(readme_path, phrase):
    """1-indexed line numbers in README.md containing the count phrase."""
    try:
        lines = readme_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return "?"
    hits = [str(i) for i, line in enumerate(lines, start=1) if phrase in line]
    return ", ".join(hits) if hits else "?"


def main(argv=None):
    repo_root = resolve_repo_root(argv)
    exit_code, message = check_docs_drift(repo_root)
    print(message)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
