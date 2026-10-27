"""Hermetic tests for ``scripts/check_fresh_host_clone.sh`` (DF-GITREINS-POC-87).

The row: every documented clone path named the repo and nothing else, while the
repo's own origin is SSH. On a brand-new box with no ``github.com`` entry in
``~/.ssh/known_hosts`` and no ssh-agent, ``git clone git@github.com:...`` dies
with ``Host key verification failed`` (dogfood 2026-10-09 run 18: ssh rc=128 in
3s, the https clone of the same public repo rc=0 in 3s). README.md now names
both ways out — trust github.com with ``ssh-keyscan``, or clone over https — and
``scripts/check_fresh_host_clone.sh`` reproduces both arms so the signpost cannot
rot into fiction.

Nothing here touches the network: each arm drives a STUB git through
``QA_FHC_GIT`` and asserts the probe's exit code plus the line that tells a
reader which arm broke. The last test is the docs gate itself — README.md must
keep naming both options, which is the drift this row exists to prevent.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE = REPO_ROOT / "scripts" / "check_fresh_host_clone.sh"
README = REPO_ROOT / "README.md"
HTTPS_URL = "https://github.com/totalwindupflightsystems/gitreins.git"

_HOST_KEY_FAILURE = """Host key verification failed.
fatal: Could not read from remote repository.
"""


def _stub(tmp_path: Path, ssh_body: str, https_body: str) -> Path:
    """Write an executable git stub whose behaviour depends on the clone URL.

    Any clone argument list containing a ``git@…`` URL is the ssh arm; anything
    containing ``https://`` is the https arm. Both bodies are bash fragments.
    """
    stub = tmp_path / "stub-git"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        'url=""\n'
        'for a in "$@"; do\n'
        '  case "$a" in git@*|ssh://*|https://*) url="$a" ;; esac\n'
        "done\n"
        'case "$url" in\n'
        f"  git@*|ssh://*) {ssh_body} ;;\n"
        f"  https://*) {https_body} ;;\n"
        "esac\n"
        'echo "stub-git: no arm matched ($*)" >&2\n'
        "exit 99\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)
    return stub


def _https_ok_body() -> str:
    """Clone 'succeeds': create the destination the last argument names."""
    return (
        'for a in "$@"; do dest="$a"; done\n'
        'mkdir -p "$dest/.git" && touch "$dest/README.md" && exit 0'
    )


def _ssh_hostkey_body() -> str:
    return f'printf "%s" {_quote(_HOST_KEY_FAILURE)} >&2; exit 128'


def _quote(text: str) -> str:
    """Single-quote a multi-line string for a bash stub."""
    return "'" + text.replace("'", "'\\''") + "'"


def _run_probe(
    tmp_path: Path, stub: Path | None, timeout: int = 120
) -> subprocess.CompletedProcess:
    """Run the probe with an isolated HOME/PATH and a stubbed (or absent) git."""
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path / "operator-home"),
        "TMPDIR": str(tmp_path),
        "QA_FHC_TIMEOUT": "20",
    }
    if stub is not None:
        env["QA_FHC_GIT"] = str(stub)
    return subprocess.run([str(PROBE)], capture_output=True, text=True, env=env, timeout=timeout)


# ── the file itself ────────────────────────────────────────────────────────────


def test_probe_is_shipped_and_executable() -> None:
    """An unexecutable probe is a probe nobody runs."""
    assert PROBE.is_file(), f"missing {PROBE}"
    assert os.access(PROBE, os.X_OK), f"{PROBE} is not executable"
    shebang = PROBE.read_text(encoding="utf-8").splitlines()[0]
    assert shebang.startswith("#!") and "bash" in shebang


def test_both_arms_reproduce_and_grade_ok(tmp_path: Path) -> None:
    """The documented shape: ssh fails on host keys, https clone succeeds → 0."""
    stub = _stub(tmp_path, _ssh_hostkey_body(), _https_ok_body())
    result = _run_probe(tmp_path, stub)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "arm1 OK" in result.stdout and "arm2 OK" in result.stdout
    # Both arms must be *named* as observed — a bare "OK" line would hide which
    # half of the documented contract went unmeasured.
    assert "ssh clone without known_hosts fails on host keys" in result.stdout
    assert "https clone succeeded" in result.stdout


def test_ssh_arm_succeeding_is_a_fail_not_a_pass(tmp_path: Path) -> None:
    """If the ssh path stops failing, the documented prerequisite is unverified."""
    stub = _stub(tmp_path, "exit 0", _https_ok_body())
    result = _run_probe(tmp_path, stub)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "SUCCEEDED" in result.stdout
    assert "not isolating" in result.stdout


def test_ssh_arm_failing_for_another_reason_is_a_fail(tmp_path: Path) -> None:
    """A different ssh error means the documented prerequisite is not the one hit."""
    stub = _stub(
        tmp_path,
        'echo "git@github.com: Permission denied (publickey)." >&2; exit 128',
        _https_ok_body(),
    )
    result = _run_probe(tmp_path, stub)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "Permission denied" in result.stdout
    assert "NOT with 'Host key verification failed'" in result.stdout


def test_unreachable_network_is_an_info_gap_never_a_pass(tmp_path: Path) -> None:
    """No network is not evidence about the docs: report a gap, never a green."""
    stub = _stub(
        tmp_path,
        'echo "ssh: Could not resolve hostname github.com" >&2; exit 255',
        _https_ok_body(),
    )
    result = _run_probe(tmp_path, stub)

    assert result.returncode == 2, result.stdout + result.stderr
    assert "environment gap" in result.stdout


def test_https_failure_while_the_host_is_reachable_is_a_fail(tmp_path: Path) -> None:
    """ssh reaching the host proves the network: an https failure is the docs' problem."""
    stub = _stub(
        tmp_path,
        _ssh_hostkey_body(),
        'echo "fatal: unable to access the repository" >&2; exit 128',
    )
    result = _run_probe(tmp_path, stub)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "the documented clone path does not work" in result.stdout


def test_https_rc0_without_a_clone_is_a_fail(tmp_path: Path) -> None:
    """A success that ships nothing is not a clone."""
    stub = _stub(tmp_path, _ssh_hostkey_body(), "exit 0")
    result = _run_probe(tmp_path, stub)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "left no clone behind" in result.stdout


# ── the docs gate this row exists for ─────────────────────────────────────────


def test_readme_names_both_fresh_host_clone_options() -> None:
    """README must keep the https clone URL AND the ssh-keyscan prerequisite.

    This is the drift that opened DF-GITREINS-POC-87: a documented clone path
    that looked complete while pushing every fresh user into the ssh wall.
    """
    text = README.read_text(encoding="utf-8")

    assert HTTPS_URL in text, "README no longer names the https clone URL"
    assert re.search(r"ssh-keyscan\s+github\.com\s*>>\s*~?/?\.ssh/known_hosts", text), (
        "README no longer names `ssh-keyscan github.com >> ~/.ssh/known_hosts`"
    )
    assert "Host key verification failed" in text, (
        "README no longer names the failure the prerequisite prevents"
    )
    assert "scripts/check_fresh_host_clone.sh" in text, (
        "README no longer points at the probe that asserts both arms"
    )
