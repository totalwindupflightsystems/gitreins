#!/usr/bin/env python3
"""Check README.md/CONTRIBUTING.md drift against their authorities (GR-GAP-052, GR-GAP-061).

Two checks, both fail-closed:

Check A (version): the README release banner version must equal the version
declared in pyproject.toml ([project] table).

Check B (counts): the live test collection — run here with
``pytest --collect-only -q --override-ini=addopts=`` (exactly the CI
collection; pytest is invoked via subprocess, never imported) — is compared
against EVERY ``N tests pass`` / ``N tests across`` / ``N test files`` claim in
README.md AND CONTRIBUTING.md. A claim that disagrees with the live collection,
or with a sibling claim in the same document, fails with FILE:LINE and both
numbers. Unmeasurable collection is a FAIL, never a green: a gate must never
certify a metric it did not measure. ``--static`` skips the live comparison and
says so in its message (it then only checks internal claim agreement).

GR-GAP-061: the live comparison used to live ONLY in a bash block in
.github/workflows/ci.yml, so this script printed "counts consistent" without
ever running pytest. The bash block is gone — CI now delegates to this script.

Standard library only; compatible with Python 3.10 (pyproject.toml is parsed
with a line scan, not tomllib).

Usage:
    python scripts/check_docs_drift.py [--repo-root PATH] [--static]

Exit code 0 on success (one summary line), 1 on any mismatch or unmeasurable
collection (specific, actionable message naming both values).
"""

import argparse
import re
import subprocess
import sys
from pathlib import Path

# Matches a PEP 440-ish version string inside a **v...** bold span on the
# banner line, e.g. "**v0.12.1**" or "**v1.2.3rc1**".
_BANNER_VERSION_RE = re.compile(r"\*\*v(\d+\.\d+\.\d+(?:[^\s*]*)?)\*\*")
_PYPROJECT_VERSION_RE = re.compile(r'^version\s*=\s*"([^"]+)"\s*$')

# Whitespace-tolerant claim patterns (\s+, not a literal space): a documented
# phrase may wrap across a newline — CONTRIBUTING.md's "**<N> tests across <M>\ntest files**"
# is exactly that shape, and a literal-space regex silently skips it.
_CLAIM_PATTERNS = {
    "tests pass": re.compile(r"(\d+)\s+tests\s+pass"),
    "tests across": re.compile(r"(\d+)\s+tests\s+across(?:\s+(\d+)\s+(?:test\s+)?files)?"),
    "test files": re.compile(r"(\d+)\s+test\s+files"),
}
# The unique-test-file paths in a `--collect-only -q` report, same regex the CI
# bash block used so the two cannot disagree.
_TEST_PATH_RE = re.compile(r"^tests/[a-zA-Z0-9_./-]+\.py")
# The collected-total footer pytest prints on the last stdout line:
# "<N> tests collected in 0.47s" / "<N> tests collected" / "1 error".
_COLLECTED_RE = re.compile(r"^(\d+)\s+(?:tests?|test)\s+collected\b")


def resolve_repo_root(argv=None):
    """Return the repo root: --repo-root override, else scripts/'s parent.

    Parses argv leniently so the same helper serves both the strict argparse
    pass in main() and the tolerant legacy call shape.
    """
    argv = list(argv) if argv is not None else []
    for i, arg in enumerate(argv):
        if arg == "--repo-root" and i + 1 < len(argv):
            return Path(argv[i + 1])
        if arg.startswith("--repo-root="):
            return Path(arg.split("=", 1)[1])
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


def collect_live_counts(repo_root):
    """Run the CI collection in repo_root. Return (count, files) or (None, reason).

    Executes ``[sys.executable, "-m", "pytest", "--collect-only", "-q",
    "--override-ini=addopts="]`` via subprocess with cwd=repo_root (pytest is
    NEVER imported here — stdlib only), captures stdout+stderr and the exit
    code, and parses the numbers the same way the CI bash block did: COUNT is
    the leading integer of the last non-empty stdout line, FILES is the number
    of unique ``tests/...py`` paths matched by the exact CI regex. A missing
    pytest, a non-zero exit, or an unparseable total returns (None, reason) —
    callers must treat that as a FAIL, never as a green.
    """
    cmd = [
        sys.executable,
        "-m",
        "pytest",
        "--collect-only",
        "-q",
        "--override-ini=addopts=",
    ]
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(repo_root),
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        return None, f"could not execute {cmd[0]}: {exc}"

    if proc.returncode != 0:
        tail = _output_tail(proc)
        return None, (
            f"pytest exited {proc.returncode} (live collection is not measurable); "
            f"tail of output: {tail}"
        )

    stdout_lines = proc.stdout.splitlines()
    nonempty = [ln for ln in stdout_lines if ln.strip()]
    if not nonempty:
        return None, "pytest produced no stdout to parse the collected total from"

    last = nonempty[-1].strip()
    count_match = _COLLECTED_RE.match(last)
    if count_match is None:
        count_match = re.match(r"^(\d+)\b", last)
    if count_match is None:
        return None, (
            f"could not parse a collected-test count from pytest's last stdout line ({last!r})"
        )
    count = int(count_match.group(1))
    files = len({m.group(0) for ln in stdout_lines if (m := _TEST_PATH_RE.match(ln))})
    return (count, files), None


def _output_tail(proc, limit=300):
    """Compressed tail of a failed pytest run's output, for the FAIL message."""
    combined = (proc.stdout + proc.stderr).strip()
    if not combined:
        return "<no output>"
    tail = "\n".join(combined.splitlines()[-5:])
    return tail[-limit:]


def collect_doc_claims(doc_path):
    """Every count claim in doc_path: list of (phrase, value, line, snippet).

    Matching is whitespace-tolerant over the whole text, so a claim split
    across a newline is still caught; the 1-indexed line number is derived
    from the match position. The ``N tests across`` pattern also records the
    trailing ``N files`` value as a ``test files`` claim.
    """
    try:
        text = doc_path.read_text(encoding="utf-8")
    except OSError:
        return []
    claims = []
    for phrase, pattern in _CLAIM_PATTERNS.items():
        for match in pattern.finditer(text):
            line = text[: match.start()].count("\n") + 1
            snippet = " ".join(match.group(0).split())
            claims.append((phrase, match.group(1), line, snippet))
            if phrase == "tests across" and match.group(2) is not None:
                claims.append(("test files", match.group(2), line, snippet))
    return claims


def collect_readme_counts(readme_path):
    """Map each count phrase to the set of N values claimed in README.md."""
    claims = collect_doc_claims(readme_path)
    return {
        phrase: sorted({value for claim_phrase, value, _, _ in claims if claim_phrase == phrase})
        for phrase in _CLAIM_PATTERNS
    }


def _sibling_conflict(doc_name, claims, phrase):
    """Message when one doc states different values for the same phrase."""
    values = {}
    for claim_phrase, value, line, _snippet in claims:
        if claim_phrase == phrase:
            values.setdefault(value, []).append(line)
    if len(values) <= 1:
        return None
    parts = ", ".join(
        f"{value} (line(s) {', '.join(str(n) for n in sorted(set(lines)))})"
        for value, lines in sorted(values.items())
    )
    return (
        f"FAIL: {doc_name} test-count drift — conflicting '{phrase}' claims in the same "
        f"document: {parts}. All mentions must state the same number."
    )


def _cross_doc_conflict(docs):
    """Message when docs (or claims across docs) state numbers that disagree.

    ``N tests pass`` and ``N tests across`` must state the same test count, and
    every ``N test files`` claim must state the same file count. The offending
    claim is named with its FILE:LINE and both values.
    """
    pass_claims = [
        (doc, line, snippet, value)
        for doc, claims in docs
        for phrase, value, line, snippet in claims
        if phrase == "tests pass"
    ]
    if pass_claims:
        reference = pass_claims[0]
        for doc, line, snippet, value in pass_claims[1:] + [
            (doc, line, snippet, value)
            for doc, claims in docs
            for phrase, value, line, snippet in claims
            if phrase == "tests across"
        ]:
            if value != reference[3]:
                return (
                    f"FAIL: {doc}:{line} test-count drift — documents '{snippet}' but the "
                    f"'tests pass' / 'tests across' claims disagree: '{reference[2]}' "
                    f"(at {reference[0]}:{reference[1]}) states {reference[3]} tests, "
                    f"this claim states {value}. Update {doc}:{line}."
                )
    file_claims = [
        (doc, line, snippet, value)
        for doc, claims in docs
        for phrase, value, line, snippet in claims
        if phrase == "test files"
    ]
    if file_claims:
        reference = file_claims[0]
        for doc, line, snippet, value in file_claims[1:]:
            if value != reference[3]:
                return (
                    f"FAIL: {doc}:{line} test-file-count drift — documents '{snippet}' but "
                    f"'{reference[2]}' (at {reference[0]}:{reference[1]}) states "
                    f"{reference[3]} files. Update {doc}:{line}."
                )
    return None


def check_docs_drift(repo_root, static_only=False):
    """Run both drift checks. Return (exit_code, message).

    static_only=True skips the live pytest collection and only checks internal
    agreement of the documented claims; the success message then says the live
    collection was NOT compared. Default: live comparison (the gate's whole
    point — it never certifies a number it did not measure).
    """
    pyproject_path = repo_root / "pyproject.toml"
    readme_path = repo_root / "README.md"
    contributing_path = repo_root / "CONTRIBUTING.md"

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

    # Gather claims from every machine-checked doc. A missing CONTRIBUTING.md
    # (e.g. a throwaway fixture tree) is tolerated; README.md is not.
    docs = [(readme_path.name, collect_doc_claims(readme_path))]
    if contributing_path.exists():
        docs.append((contributing_path.name, collect_doc_claims(contributing_path)))

    readme_claims = docs[0][1]
    if len(docs) > 1 and not docs[1][1]:
        return (
            1,
            "FAIL: CONTRIBUTING.md states no test-count claim, so its numbers are not "
            "machine-checked. Add the live collection count (see README.md).",
        )

    if static_only:
        # No measurement exists, so internal agreement is the strongest claim
        # available. Presence of each phrase is checked per phrase, interleaved
        # with the same-doc conflict check (GR-GAP-052 precedence: a conflicting
        # 'tests pass' claim is reported before a missing 'tests across' claim).
        for phrase in _CLAIM_PATTERNS:
            if not any(cp == phrase for cp, _v, _l, _s in readme_claims):
                return 1, f"FAIL: README.md contains no '{phrase}' claim (expected at least one)."
            conflict = _sibling_conflict(readme_path.name, readme_claims, phrase)
            if conflict:
                return 1, conflict
        for doc_name, claims in docs[1:]:
            for phrase in _CLAIM_PATTERNS:
                conflict = _sibling_conflict(doc_name, claims, phrase)
                if conflict:
                    return 1, conflict
        cross = _cross_doc_conflict(docs)
        if cross:
            return 1, cross
        doc_names = " + ".join(name for name, _ in docs)
        return (
            0,
            f"docs drift check (static only): version {pyproject_version} matches README "
            f"banner; {doc_names} claims agree with each other — the live pytest "
            f"collection was NOT compared, so the numbers are unverified.",
        )

    # Live path: the collection is the authority and it is checked FIRST, so a
    # stale claim is reported against the measured numbers (GR-GAP-061).
    # Internal-agreement checks are subsumed on this path — claims that all
    # equal the live collection cannot disagree with each other.
    live, reason = collect_live_counts(repo_root)
    if live is None:
        return (
            1,
            f"FAIL: live test collection could not be measured, so the documented test "
            f"counts are unverified — {reason}",
        )
    live_count, live_files = live
    measured = f"{live_count} tests in {live_files} files"

    for doc_name, claims in docs:
        for phrase in ("tests pass", "tests across", "test files"):
            expected = str(live_files if phrase == "test files" else live_count)
            for claim_phrase, value, line, snippet in claims:
                if claim_phrase == phrase and value != expected:
                    kind = "test-file-count" if phrase == "test files" else "test-count"
                    return (
                        1,
                        f"FAIL: {doc_name}:{line} {kind} drift — documents '{snippet}' "
                        f"but pytest collects {measured}. Update {doc_name}:{line}.",
                    )

    for phrase in _CLAIM_PATTERNS:
        if not any(cp == phrase for cp, _v, _l, _s in readme_claims):
            return 1, f"FAIL: README.md contains no '{phrase}' claim (expected at least one)."

    doc_names = " + ".join(name for name, _ in docs)
    return (
        0,
        f"docs drift check OK: version {pyproject_version} matches README banner; "
        f"{doc_names} test counts match the live collection "
        f"({live_count} tests / {live_files} test files)",
    )


def main(argv=None):
    argv = list(argv) if argv is not None else sys.argv[1:]
    parser = argparse.ArgumentParser(
        description="Check README/CONTRIBUTING version and test-count drift.",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=None,
        help="Repository root (default: parent of the scripts/ directory containing this file).",
    )
    parser.add_argument(
        "--static",
        action="store_true",
        help="Skip the live pytest collection; check only that documented claims agree "
        "with each other (the success message says the live collection was not compared).",
    )
    args = parser.parse_args(argv)
    repo_root = args.repo_root if args.repo_root is not None else resolve_repo_root()
    exit_code, message = check_docs_drift(repo_root, static_only=args.static)
    print(message)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
