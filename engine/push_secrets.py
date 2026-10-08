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


def scan_push_range(workdir: str, local_sha: str, remote_sha: str) -> list[str]:
    """Scan introduced commit content between remote and local refs."""
    zero = bool(remote_sha) and set(remote_sha) == {"0"}
    revision = local_sha if zero else f"{remote_sha}..{local_sha}"
    result = subprocess.run(
        ["git", "log", "--no-ext-diff", "--diff-merges=first-parent", "-p", "--format=", revision],
        cwd=workdir,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "could not inspect push range")
    findings: list[str] = []
    for line in result.stdout.splitlines():
        if not line.startswith("+") or line.startswith("+++"):
            continue
        for pattern, label in _SECRET_PATTERNS:
            if pattern.search(line[1:]):
                findings.append(label)
                break
    return list(dict.fromkeys(findings))


def check_push_range(workdir: str, local_sha: str, remote_sha: str) -> int:
    """Print a safe refusal and return 1 when the outgoing history has secrets."""
    try:
        findings = scan_push_range(workdir, local_sha, remote_sha)
    except RuntimeError as exc:
        print(f"PUSH REFUSED: secret scan could not verify the outgoing history: {exc}")
        return 1
    if not findings:
        print("PUSH SECRET CHECK: clean")
        return 0
    for finding in findings:
        print(f"PUSH REFUSED: {finding} found in outgoing commit history.")
    print(
        "Rotate the exposed credential and have a human rewrite the affected "
        "history before retrying."
    )
    return 1
