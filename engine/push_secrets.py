"""Refuse pushes that would publish credentials already present in history.

GR-GAP-066 changes, all deliberate and documented here:

* Path allowlist parity (A): the scan mirrors the repo's own ``.gitleaks.toml``
  ``[allowlist].paths`` regexes in addition to the built-in ``.coding-hermes/``
  pattern, so the push gate enforces the same path policy as the commit-time
  secrets guard. Any failure to load the config (missing file, parse error,
  wrong types, bad regex) degrades to the built-in allowlist only and never
  raises.
* Value-shape grading (B): a ``credential assignment`` finding now requires the
  right-hand side to be a credential-SHAPED literal, evaluated after the same
  concat-glue reassembly the token patterns use. Prose values
  (``"working-tree"``, ``"DEEPSEEK (fallback)"``) are no longer refused.
* Fixture-shaped placeholder check (C): token matches whose body is a pure
  repetition of a short period, or contains a run of 8+ identical characters,
  are treated as deliberate fixtures, not credentials. This is a
  fixture-shaped-value relaxation, NOT a general allowlist: a real random token
  is neither periodic nor run-heavy and is still refused. The private-key class
  is never relaxed. The same placeholder gate applies when the assignment
  value-shape check grades a token-shaped value.
* Attribution (D): refusal lines name the class, path and commit — one line per
  (class, path, commit) group with an occurrence count — while never echoing
  the matched value.

The gate intentionally still refuses randomly-shaped credentials: rotate them,
do not commit them.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from typing import NamedTuple

# Token literal patterns carry a capture group for the body after the prefix;
# placeholder-capable ones are graded by _looks_like_placeholder. The
# private-key pattern is never relaxed.
_TOKEN_PATTERNS: tuple[tuple[re.Pattern[str], str, bool], ...] = (
    (
        re.compile(r"-----BEGIN\s+(?:RSA\s+|EC\s+|OPENSSH\s+|DSA\s+|PGP\s+)?PRIVATE\s+KEY"),
        "private key",
        False,
    ),
    (re.compile(r"\bghp_([A-Za-z0-9]{28,})"), "GitHub personal access token", True),
    (re.compile(r"\bgho_([A-Za-z0-9]{28,})"), "GitHub OAuth token", True),
    (re.compile(r"\bglpat-([A-Za-z0-9_-]{20,})"), "GitLab personal access token", True),
    (re.compile(r"\bsk-([A-Za-z0-9_-]{20,})"), "API key", True),
    (re.compile(r"\bxox[baprs]-([A-Za-z0-9-]{20,})"), "Slack token", True),
)

# GR-GAP-064b: a call expression on the right-hand side is code, not a
# credential value — `_SECRETS_FINDING_LINE = re.compile(r"...")` in
# engine/types.py was refused as a "credential assignment" on every push whose
# range re-added that line. A real secret is a literal (quoted or bare), never
# `identifier(...)`, so the lookahead rejects only that shape and leaves the
# value class otherwise unchanged. GR-GAP-066 (B) additionally grades the value
# by shape via _credential_shaped.
_CREDENTIAL_ASSIGNMENT: re.Pattern[str] = re.compile(
    r"(?im)^\s*(?:[A-Z0-9_]*(?:PASSWORD|TOKEN|SECRET|API_KEY)[A-Z0-9_]*)\s*=\s*(?!\w+(?:\.\w+)*\()[^\s#]{12,}"
)

_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str, bool], ...] = _TOKEN_PATTERNS + (
    (_CREDENTIAL_ASSIGNMENT, "credential assignment", False),
)

_CONCAT_GLUE: re.Pattern[str] = re.compile(r"""["']\s*\+\s*["']""")

# GR-GAP-066 (B): a credential-shaped literal is 20+ characters drawn only from
# this set (token-prefix bodies, hex, base64-ish) — prose values contain
# spaces, parentheses or other punctuation and never qualify.
_CREDENTIAL_VALUE_CHARS: re.Pattern[str] = re.compile(r"^[A-Za-z0-9_\-+/=.:]+$")

# Paths whose added lines are skipped by the push scan. The built-in pattern
# mirrors the commit-time gitleaks board allowlist origin (GR-GAP-005): board
# fixture rows carry token-shaped REPRO STRINGS that match no live credential.
# GR-GAP-066 (A) extends this at scan time with the repo's own .gitleaks.toml
# [allowlist].paths so both gates enforce the same path policy.
_BUILTIN_ALLOWLISTED_PATH_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^\.coding-hermes/"),
)


def _load_gitleaks_path_allowlist(workdir: str) -> tuple[re.Pattern[str], ...]:
    """Load ``[allowlist].paths`` regex patterns from ``<workdir>/.gitleaks.toml``.

    Durability rule (GR-GAP-066 A): the push gate must keep working on a repo
    without a ``.gitleaks.toml``, so ANY failure — missing file, TOML parse
    error, wrong types, non-string entry, bad regex — degrades to an empty
    tuple and never raises.
    """
    config_path = os.path.join(workdir, ".gitleaks.toml")
    try:
        if sys.version_info >= (3, 11):
            import tomllib
        else:  # pragma: no cover — Python 3.10 fallback (tomli is a declared dep)
            import tomli as tomllib

        with open(config_path, "rb") as handle:
            data: object = tomllib.load(handle)
        allowlist = data.get("allowlist") if isinstance(data, dict) else None
        paths = allowlist.get("paths") if isinstance(allowlist, dict) else None
        if not isinstance(paths, list):
            return ()
        patterns: list[re.Pattern[str]] = []
        for entry in paths:
            if not isinstance(entry, str):
                return ()
            patterns.append(re.compile(entry))
        return tuple(patterns)
    except Exception:
        # Missing config, parse error, wrong types, bad regex: scan on with the
        # built-in allowlist only.
        return ()


def _is_allowlisted_path(path: str, patterns: tuple[re.Pattern[str], ...]) -> bool:
    """Return True when a changed file's path is skipped by the push scan."""
    return any(pattern.search(path) for pattern in patterns)


def _diff_new_path(line: str) -> str | None:
    """Extract the new-file path from a ``+++ `` diff header line.

    Strips the ``b/`` prefix, the surrounding double quotes git adds for paths
    with special characters, and maps ``/dev/null`` (added-file edge cases) to
    None, meaning "no path".
    """
    payload = line[len("+++ ") :]
    if len(payload) >= 2 and payload.startswith('"') and payload.endswith('"'):
        payload = payload[1:-1]
    if payload.startswith("b/"):
        payload = payload[len("b/") :]
    return None if payload == "/dev/null" else payload


def _looks_like_placeholder(body: str) -> bool:
    """Return True when a token body is a deliberate fixture shape (GR-GAP-066 C).

    A body is fixture-shaped when it is a pure repetition of a short period
    (2 <= p <= max(2, len//3)) or contains a run of 8+ identical characters.
    A real random token satisfies neither. This is a relaxation for
    fixture-shaped values ONLY — never applied to the private-key class.
    """
    length = len(body)
    if length == 0:
        return False
    max_period = max(2, length // 3)
    for period in range(2, max_period + 1):
        reps = (length + period - 1) // period
        if (body[:period] * reps)[:length] == body:
            return True
    return bool(re.search(r"(.)\1{7,}", body))


def _has_qualifying_token_match(pattern: re.Pattern[str], text: str, relaxable: bool) -> bool:
    """Return True when ``pattern`` matches ``text`` with a non-placeholder body."""
    for match in pattern.finditer(text):
        if not relaxable:
            return True
        body: object = match.group(1)
        if isinstance(body, str) and not _looks_like_placeholder(body):
            return True
    return False


def _credential_shaped(value: str) -> bool:
    """Return True when a credential-assignment value looks like a real credential.

    GR-GAP-066 (B): either the value carries a token literal shape (ghp_, gho_,
    glpat-, sk-, xox, private-key header — token-shaped values still pass
    through the GR-GAP-066 (C) placeholder gate), or it is 20+ chars drawn only
    from _CREDENTIAL_VALUE_CHARS with at least two character classes. A value
    that is itself a repeated-block fixture shape fails the (b) arm as well.
    """
    for pattern, _label, relaxable in _TOKEN_PATTERNS:
        if _has_qualifying_token_match(pattern, value, relaxable):
            return True
    if len(value) < 20 or not _CREDENTIAL_VALUE_CHARS.match(value):
        return False
    has_lower = any("a" <= char <= "z" for char in value)
    has_upper = any("A" <= char <= "Z" for char in value)
    has_digit = any("0" <= char <= "9" for char in value)
    if sum([has_lower, has_upper, has_digit]) < 2:
        return False
    return not _looks_like_placeholder(value)


def _assignment_rhs(text: str) -> str | None:
    """Extract the assignment right-hand side as the string literal it builds.

    Cuts a trailing `` #`` comment, reassembles quote-plus-quote concatenation
    with the same _CONCAT_GLUE rule the token patterns use, and strips one pair
    of surrounding matching quotes.
    """
    _head, sep, rhs = text.partition("=")
    if not sep:
        return None
    rhs = rhs.strip()
    comment = rhs.find(" #")
    if comment != -1:
        rhs = rhs[:comment]
    glued = _CONCAT_GLUE.sub("", rhs)
    if len(glued) >= 2 and glued[0] == glued[-1] and glued[0] in ("'", '"'):
        return glued[1:-1]
    return glued


def _classify_line(text: str) -> str | None:
    """Return the finding label for a diff line, or None when it is clean."""
    for pattern, label, relaxable in _TOKEN_PATTERNS:
        if _has_qualifying_token_match(pattern, text, relaxable):
            return label
    if _CREDENTIAL_ASSIGNMENT.search(text) is not None:
        value = _assignment_rhs(text)
        if value is not None and _credential_shaped(value):
            return "credential assignment"
    return None


class _PushScanResult(NamedTuple):
    labels: list[str]
    scanned: int
    range_label: str
    skipped_files: int
    # One entry per (class, path, commit) group, in first-seen order, with the
    # number of flagged added lines in the group. Values are never included.
    groups: list[tuple[str, str, str, int]]


def _scan_scope(local_sha: str, remote_sha: str) -> tuple[list[str], str]:
    """Return the revision args and a human-readable label for the scan range."""
    zero = bool(remote_sha) and set(remote_sha) == {"0"}
    if zero:
        # All-zero remote sha = brand-new branch. Scan only the commits not
        # reachable from any remote-tracking ref, so long-published history
        # (e.g. a repo's own scanner fixtures) cannot block a clean push.
        return [local_sha, "--not", "--remotes"], f"{local_sha[:12]} --not --remotes"
    return [f"{remote_sha}..{local_sha}"], f"{remote_sha[:12]}..{local_sha[:12]}"


def _plural(count: int) -> str:
    return "commit" if count == 1 else "commits"


def _scan_push_range(workdir: str, local_sha: str, remote_sha: str) -> _PushScanResult:
    """Scan introduced commit content.

    Returns the de-duplicated finding labels, commit count, range label, the
    number of distinct allowlisted files whose added lines were skipped, and
    the per-(class, path, commit) finding groups for attribution.
    """
    revision_args, range_label = _scan_scope(local_sha, remote_sha)
    allowlist = _BUILTIN_ALLOWLISTED_PATH_PATTERNS + _load_gitleaks_path_allowlist(workdir)
    result = subprocess.run(
        [
            "git",
            "log",
            "--no-ext-diff",
            "--diff-merges=first-parent",
            "-p",
            "--format=commit %H",
            *revision_args,
        ],
        cwd=workdir,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "could not inspect push range")
    counted = subprocess.run(
        ["git", "rev-list", "--count", *revision_args],
        cwd=workdir,
        capture_output=True,
        text=True,
        check=False,
    )
    if counted.returncode:
        raise RuntimeError(counted.stderr.strip() or "could not count the outgoing commits")
    try:
        scanned = int(counted.stdout.strip())
    except ValueError:
        raise RuntimeError("could not parse the outgoing commit count") from None
    skipped_paths: set[str] = set()
    current_path: str | None = None
    current_commit: str | None = None
    groups: dict[tuple[str, str, str], int] = {}
    for line in result.stdout.splitlines():
        if line.startswith("commit "):
            # GR-GAP-066 (D): attribute findings to their commit; reset the
            # current path so it cannot leak across the commit boundary.
            sha = line[len("commit ") :].strip()
            current_commit = sha[:8] if sha else None
            current_path = None
            continue
        if line.startswith("+++ "):
            current_path = _diff_new_path(line)
            continue
        if not line.startswith("+") or line.startswith("+++"):
            continue
        if current_path is not None and _is_allowlisted_path(current_path, allowlist):
            skipped_paths.add(current_path)
            continue
        content = line[1:]
        label = _classify_line(content)
        if label is None:
            # A credential split across string concatenation ("ghp_" + "abc...")
            # defeats contiguous-body regexes; re-scan with the quote-plus-quote
            # glue removed so the chunks join into the real token.
            glued = _CONCAT_GLUE.sub("", content)
            if glued != content:
                label = _classify_line(glued)
        if label is not None:
            path = current_path if current_path is not None else "(unknown path)"
            commit = current_commit if current_commit is not None else "(unknown commit)"
            key = (label, path, commit)
            groups[key] = groups.get(key, 0) + 1
    labels = list(dict.fromkeys(key[0] for key in groups))
    return _PushScanResult(
        labels=labels,
        scanned=scanned,
        range_label=range_label,
        skipped_files=len(skipped_paths),
        groups=[(key[0], key[1], key[2], count) for key, count in groups.items()],
    )


def _plural_files(count: int) -> str:
    return "file" if count == 1 else "files"


def _plural_places(count: int) -> str:
    return "place" if count == 1 else "places"


def scan_push_range(workdir: str, local_sha: str, remote_sha: str) -> list[str]:
    """Scan introduced commit content between remote and local refs."""
    scan = _scan_push_range(workdir, local_sha, remote_sha)
    return scan.labels


def check_push_range(workdir: str, local_sha: str, remote_sha: str) -> int:
    """Print a safe refusal and return 1 when the outgoing history has secrets."""
    try:
        scan = _scan_push_range(workdir, local_sha, remote_sha)
    except RuntimeError as exc:
        print(f"PUSH REFUSED: secret scan could not verify the outgoing history: {exc}")
        return 1
    scope = f"scanned {scan.scanned} {_plural(scan.scanned)} in {scan.range_label}"
    if not scan.labels:
        skipped_note = (
            f"; allowlisted paths skipped: {scan.skipped_files} {_plural_files(scan.skipped_files)}"
            if scan.skipped_files
            else ""
        )
        print(f"PUSH SECRET CHECK: clean ({scope}{skipped_note})")
        return 0
    for label, path, commit, count in scan.groups:
        # GR-GAP-066 (D): name the commit and path, count the occurrences,
        # never echo the matched value.
        print(f"PUSH REFUSED: {label} in {path} @ {commit} ({count} {_plural_places(count)})")
    print(f"Scanned {scan.scanned} {_plural(scan.scanned)} in {scan.range_label}.")
    print(
        "Rotate the exposed credential and have a human rewrite the affected "
        "history before retrying."
    )
    return 1
