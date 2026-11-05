"""Refuse pushes that would publish credentials already present in history."""

from __future__ import annotations

import re
import subprocess

_SECRET_PATTERNS = (
    (
        re.compile(r"-----BEGIN\s+(?:RSA\s+|EC\s+|OPENSSH\s+|DSA\s+|PGP\s+)?PRIVATE\s+KEY"),
        "private key",
    ),
    (re.compile(r"\bghp_[A-Za-z0-9]{28,}"), "GitHub personal access token"),
    (re.compile(r"\bgho_[A-Za-z0-9]{28,}"), "GitHub OAuth token"),
    (re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}"), "GitLab personal access token"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"), "API key"),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}"), "Slack token"),
    (
        re.compile(
            r"(?im)^\s*(?:[A-Z0-9_]*(?:PASSWORD|TOKEN|SECRET|API_KEY)[A-Z0-9_]*)\s*=\s*[^\s#]{12,}"
        ),
        "credential assignment",
    ),
)

_CONCAT_GLUE = re.compile(r"""["']\s*\+\s*["']""")


def _first_match(text: str) -> str | None:
    for pattern, label in _SECRET_PATTERNS:
        if pattern.search(text):
            return label
    return None


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


def _scan_push_range(workdir: str, local_sha: str, remote_sha: str) -> tuple[list[str], int, str]:
    """Scan introduced commit content; return findings, commit count, range label."""
    revision_args, range_label = _scan_scope(local_sha, remote_sha)
    result = subprocess.run(
        [
            "git",
            "log",
            "--no-ext-diff",
            "--diff-merges=first-parent",
            "-p",
            "--format=",
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
    findings: list[str] = []
    for line in result.stdout.splitlines():
        if not line.startswith("+") or line.startswith("+++"):
            continue
        content = line[1:]
        label = _first_match(content)
        if label is None:
            # A credential split across string concatenation ("ghp_" + "abc...")
            # defeats contiguous-body regexes; re-scan with the quote-plus-quote
            # glue removed so the chunks join into the real token.
            glued = _CONCAT_GLUE.sub("", content)
            if glued != content:
                label = _first_match(glued)
        if label is not None:
            findings.append(label)
    return list(dict.fromkeys(findings)), scanned, range_label


def scan_push_range(workdir: str, local_sha: str, remote_sha: str) -> list[str]:
    """Scan introduced commit content between remote and local refs."""
    findings, _scanned, _range_label = _scan_push_range(workdir, local_sha, remote_sha)
    return findings


def check_push_range(workdir: str, local_sha: str, remote_sha: str) -> int:
    """Print a safe refusal and return 1 when the outgoing history has secrets."""
    try:
        findings, scanned, range_label = _scan_push_range(workdir, local_sha, remote_sha)
    except RuntimeError as exc:
        print(f"PUSH REFUSED: secret scan could not verify the outgoing history: {exc}")
        return 1
    scope = f"scanned {scanned} {_plural(scanned)} in {range_label}"
    if not findings:
        print(f"PUSH SECRET CHECK: clean ({scope})")
        return 0
    for finding in findings:
        print(f"PUSH REFUSED: {finding} found in outgoing commit history.")
    print(f"Scanned {scanned} {_plural(scanned)} in {range_label}.")
    print(
        "Rotate the exposed credential and have a human rewrite the affected "
        "history before retrying."
    )
    return 1
