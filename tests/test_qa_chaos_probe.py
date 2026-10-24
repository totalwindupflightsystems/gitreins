"""Hermetic tests for ``.qa/chaos-probes/errorpath.sh`` (QA-GITREINS-2).

The fleet battery's chaos-errorpath cell has two arms (``~/.hermes/scripts/bunker-qa.sh``):
a generic missing-config probe, and — when the repo ships one — an OPT-IN project
probe at ``.qa/chaos-probes/errorpath.sh`` whose exit code IS the cell grade
(0 = OK, 1 = FAIL, 2 = INFO, 124 = the harness's own wrap = FAIL "probe HANGS").

This repo ships that probe because the generic arm cannot observe the property it
is named after here: the auto-detector picks the first long-running subcommand in
``gitreins --help`` (``serve``, the judgment browser), which needs no config at
all, so the probe started a service, blocked, and was killed at its bound with
rc=124 — the cell reported "missing-config start probe timed out — error-path
behavior UNVERIFIED" on every run (2026-10-23 evidence under docs/dogfood/).

So the grade contract is what these tests pin, entirely offline and with no
dependency on the real CLI: each arm drives a STUB ``gitreins`` through
``QA_EP_BIN`` and asserts the probe's exit code plus the line the harness turns
into the cell's detail. One arm runs the repo's own console script when it is
installed next to the interpreter, so a change in the CLI's missing-config
message is caught by the product's real behavior, not by a stub.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE = REPO_ROOT / ".qa" / "chaos-probes" / "errorpath.sh"


def _stub(tmp_path: Path, body: str) -> Path:
    """Write an executable bash stub and return its path."""
    stub = tmp_path / "stub-gitreins"
    stub.write_text("#!/usr/bin/env bash\n" + body + "\n", encoding="utf-8")
    stub.chmod(0o755)
    return stub


def _run(tmp_path: Path, env: dict | None = None, timeout: int = 90) -> subprocess.CompletedProcess:
    """Run the probe with an isolated HOME/PATH and the caller's overrides."""
    base = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path / "operator-home"),
        "TMPDIR": str(tmp_path),
    }
    base.update({k: str(v) for k, v in (env or {}).items()})
    return subprocess.run(
        [str(PROBE)],
        capture_output=True,
        text=True,
        env=base,
        timeout=timeout,
    )


# ── the file itself ─────────────────────────────────────────────────────────────


def test_probe_is_shipped_at_the_hook_path_and_executable() -> None:
    """bunker-qa.sh only consults the probe when it is executable (`[ -x ... ]`)."""
    assert PROBE.is_file(), f"missing {PROBE}"
    assert os.access(PROBE, os.X_OK), f"{PROBE} is not executable — the hook would be skipped"
    shebang = PROBE.read_text(encoding="utf-8").splitlines()[0]
    assert shebang.startswith("#!") and "bash" in shebang


def test_default_command_needs_config_and_is_not_a_daemon() -> None:
    """`serve` must never be the probed command: it is long-running by design.

    The whole defect this probe replaces was a daemon being probed for an error
    path: a command that is SUPPOSED to stay up can only ever reach the bounce
    bound, so its timeout says nothing about missing-config behavior.
    """
    text = PROBE.read_text(encoding="utf-8")
    assert "QA_EP_ARGS:-guard" in text, (
        "the default probed subcommand must be `guard` (config-requiring)"
    )
    assert "QA_EP_ARGS:-serve" not in text
    assert "QA_EP_ARGS:-start" not in text
    assert "QA_EP_ARGS:-daemon" not in text


# ── the four documented product shapes ─────────────────────────────────────────


def test_clean_error_naming_the_missing_config_is_ok(tmp_path: Path) -> None:
    stub = _stub(
        tmp_path,
        "echo 'no .gitreins/config.yaml — run `gitreins init` first'\nexit 1",
    )
    proc = _run(tmp_path, {"QA_EP_BIN": stub})
    assert proc.returncode == 0, proc.stdout + proc.stderr
    first = proc.stdout.splitlines()[0]
    assert "clean error on missing config" in first
    assert "rc=1" in first
    assert str(stub) in first


def test_usage_banner_with_a_nonzero_exit_is_ok(tmp_path: Path) -> None:
    """A usage banner is the other documented shape ("print usage and exit")."""
    stub = _stub(tmp_path, 'echo "Usage: gitreins [-h] {task,guard}"\nexit 2')
    proc = _run(tmp_path, {"QA_EP_BIN": stub})
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "usage banner on missing config" in proc.stdout
    assert "rc=2" in proc.stdout


def test_silent_success_is_a_failure(tmp_path: Path) -> None:
    """rc=0 on a missing config means no error path was exercised at all."""
    stub = _stub(tmp_path, "exit 0")
    proc = _run(tmp_path, {"QA_EP_BIN": stub})
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "SILENT SUCCESS" in proc.stdout


def test_unhelpful_error_is_a_failure(tmp_path: Path) -> None:
    """A non-zero exit that names nothing gives the operator nothing to act on."""
    stub = _stub(tmp_path, 'echo "boom"\nexit 1')
    proc = _run(tmp_path, {"QA_EP_BIN": stub})
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "UNHELPFUL ERROR" in proc.stdout


def test_crash_is_a_failure(tmp_path: Path) -> None:
    stub = _stub(
        tmp_path,
        'echo "Traceback (most recent call last):"\necho "KeyError: config"\nexit 1',
    )
    proc = _run(tmp_path, {"QA_EP_BIN": stub})
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "CRASH" in proc.stdout


def test_signal_death_is_a_failure(tmp_path: Path) -> None:
    stub = _stub(tmp_path, "kill -SEGV $$")
    proc = _run(tmp_path, {"QA_EP_BIN": stub})
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "CRASH" in proc.stdout
    assert "signal" in proc.stdout


# ── the hang arm: classified here, never propagated as the harness's 124 ───────


def test_hang_is_classified_by_the_probe_not_left_as_124(tmp_path: Path) -> None:
    """A hang must leave as exit 1 with a reason — 124 is the harness's wrap.

    bunker-qa.sh reads a probe exit of 124 as "the probe itself HANGS", and the
    ambiguous predecessor of this file was killed at a bound and graded INFO
    ("unverified"). The probe therefore bounds each command itself and reports
    the hang as its own terminal FAIL.
    """
    stub = _stub(tmp_path, "sleep 30")
    started = time.monotonic()
    proc = _run(tmp_path, {"QA_EP_BIN": stub, "QA_EP_TIMEOUT": "1"})
    elapsed = time.monotonic() - started
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert proc.returncode != 124
    assert "HANGS" in proc.stdout
    assert "1s" in proc.stdout
    assert elapsed < 25, f"the probe did not bound the hang itself ({elapsed:.1f}s)"


# ── environment gaps stay INFO, never a product verdict ────────────────────────


def test_missing_cli_is_an_environment_gap(tmp_path: Path) -> None:
    proc = _run(tmp_path, {"QA_EP_BIN": "/nonexistent/gitreins"})
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "environment gap" in proc.stdout


def test_probe_isolates_home_from_the_operator(tmp_path: Path) -> None:
    """The probe must not read the caller's ~/.gitreins or ~/.gitreins/config.yaml."""
    seen_file = tmp_path / "seen-home.txt"
    stub = _stub(
        tmp_path,
        f'echo "$HOME" > {seen_file}\necho "Usage: gitreins"\nexit 2',
    )
    proc = _run(tmp_path, {"QA_EP_BIN": stub})
    assert proc.returncode == 0, proc.stdout + proc.stderr
    seen = seen_file.read_text(encoding="utf-8").strip()
    assert seen, "the stub never ran, so HOME isolation was not observed"
    assert seen != str(tmp_path / "operator-home"), "the probe reused the caller's HOME"
    assert seen != str(Path.home())
    assert "gitreins-errorpath-" in seen, f"expected the probe's own scratch HOME, saw {seen}"


# ── the product's own behavior (skipped only when the console script is absent) ─


def test_real_cli_reports_the_documented_missing_config(tmp_path: Path) -> None:
    """Run the repo's own console script: the documented clean error must hold."""
    console = Path(sys.executable).parent / "gitreins"
    if not console.is_file():
        pytest.skip(f"no console script next to the interpreter ({console})")
    proc = _run(tmp_path, {"QA_EP_BIN": console, "QA_EP_TIMEOUT": "60"})
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "clean error on missing config" in proc.stdout
    assert ".gitreins/config.yaml" in proc.stdout
