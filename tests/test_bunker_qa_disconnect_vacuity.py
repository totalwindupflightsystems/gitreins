"""DF-GITREINS-POC-100: the bunker-qa chaos-disconnect vacuity contract.

The bug: the battery graded ``chaos-disconnect`` from an EMPTY ``disc.log`` —
the network cut was never exercised (the suite produced no output at all), yet
the final fallback could still print "cell chaos-disconnect OK". An empty
subject log is the QA-BUNKER-29 / QA-WARPFS-17 vacuity class: rc=0 without any
suite output is not a disconnect result.

The fix lives in the fleet harness, which ships with the Hermes host
(``~/.hermes/scripts/bunker-qa.sh``) and has never been part of this repository
— the same external-surface situation as DF-GITREINS-POC-99, and the same test
contract applies:

* the script is resolved from ``$GITREINS_BUNKER_QA_SCRIPT`` first, then the
  host default, so the same tests can drive any checkout of the harness;
* when no script resolves, the behavioral tests SKIP — with a reason that
  names the variable and the absolute path probed, never a bare skip;
* the live-host arm is preserved in intent: where the script exists the tests
  exercise the REAL harness, not a stub fixture;
* the fence check (``bash bunker-qa.sh __fence-check``) is driven directly, so
  the generated battery body cannot drift out of sync with its substitutions
  without this suite going red.

What is pinned here, per the DF-GITREINS-POC-100 acceptance contract:

1. ``bash -n`` on the harness succeeds (the script stays parseable);
2. the harness contains a ``cell chaos-disconnect FAIL`` branch — the empty /
   vacuous subject can never silently land in OK;
3. the fence check exits 0 with ``violations=0``;
4. the vacuity arm exists verbatim ("disc.log is EMPTY") and grades UNVERIFIED,
   and the collect path PRESERVES the exact disconnect subject log: the full
   agent-side ``disc.log`` is pulled to ``<evidence>.chaos-disconnect.log``
   with the artifact path (or its emptiness) recorded in the evidence detail —
   so the graded verdict always names a re-readable subject log path.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

#: Environment override for the harness path — the repository-hermetic seam
#: (same contract as tests/test_bunker_qa_evidence_path.py, INT-CI-19).
SCRIPT_ENV = "GITREINS_BUNKER_QA_SCRIPT"

#: Where the fleet host installs the harness. Never assumed to exist.
HOST_SCRIPT = Path.home() / ".hermes" / "scripts" / "bunker-qa.sh"


def bunker_qa_script() -> Path:
    """Resolve the harness: explicit override first, then the host default."""
    override = os.environ.get(SCRIPT_ENV)
    if override:
        return Path(override).expanduser()
    return HOST_SCRIPT


def absence_reason(script: Path | None = None) -> str | None:
    """Return why the harness is unavailable, or None when it is usable."""
    path = Path(script) if script is not None else bunker_qa_script()
    if path.is_file():
        return None
    return (
        f"bunker-qa.sh (the fleet QA harness these contracts drive) is not present at {path}. "
        f"It ships with the Hermes host, not with this repository, so a clean checkout cannot "
        f"run it; set {SCRIPT_ENV}=/path/to/bunker-qa.sh to exercise these tests against a real "
        f"harness. Live-system coverage runs in the bunker-qa battery, not in this suite."
    )


SKIP_REASON = absence_reason()
requires_bunker_qa = pytest.mark.skipif(SKIP_REASON is not None, reason=SKIP_REASON or "")


def _run(*args: str, timeout: int = 60) -> subprocess.CompletedProcess:
    """Invoke the harness with an env overlay, returning the CompletedProcess."""
    return subprocess.run(
        ["bash", str(bunker_qa_script()), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _text() -> str:
    """Read the resolved harness source as text."""
    return bunker_qa_script().read_text(encoding="utf-8")


# ── resolver + skip contract (runs everywhere, script present or not) ──────────


def test_resolver_defaults_to_the_host_install(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no override the host install is the candidate — but only a candidate."""
    monkeypatch.delenv(SCRIPT_ENV, raising=False)

    assert bunker_qa_script() == HOST_SCRIPT


def test_resolver_honours_the_env_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The seam is real: an override is used verbatim, so any checkout is testable."""
    fake = tmp_path / "bunker-qa.sh"
    fake.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setenv(SCRIPT_ENV, str(fake))

    assert bunker_qa_script() == fake
    assert absence_reason() is None


def test_absence_reason_names_the_probed_path_and_the_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A skip must be actionable: path probed + variable to set, not a bare skip."""
    missing = tmp_path / "definitely-absent" / "bunker-qa.sh"
    monkeypatch.setenv(SCRIPT_ENV, str(missing))

    reason = absence_reason()

    assert reason is not None, "a missing harness must produce a reason, never None"
    assert str(missing) in reason, f"reason must name the probed path: {reason}"
    assert SCRIPT_ENV in reason, f"reason must name the override variable: {reason}"
    assert "bunker-qa" in reason, f"reason must name the artifact: {reason}"


def test_absence_reason_is_none_when_the_script_is_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Conditional, not removal: a present script clears the skip."""
    present = tmp_path / "bunker-qa.sh"
    present.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    monkeypatch.setenv(SCRIPT_ENV, str(present))

    assert absence_reason() is None


def test_every_behavioural_test_carries_the_resolver_skipif() -> None:
    """The skip must be WIRED to the behavioral tests, not merely defined next to them.

    Without this, deleting one decorator silently turns that test into a
    FileNotFoundError on a clean runner — exactly the INT-CI-19 red.
    """
    behavioural = (
        test_harness_is_valid_bash,
        test_disconnect_cell_has_a_fail_branch,
        test_fence_check_passes_with_zero_violations,
        test_vacuity_arm_exists_and_fails_closed,
        test_disconnect_log_valid_requires_marker_and_counts,
        test_collect_preserves_the_disconnect_subject_log,
    )
    for test in behavioural:
        marks = [m for m in getattr(test, "pytestmark", []) if m.name == "skipif"]
        assert marks == [requires_bunker_qa.mark], (
            f"{test.__name__} must carry requires_bunker_qa (the tested skip contract); "
            f"found {marks!r}"
        )
        skipping, reason = marks[0].args[0], str(marks[0].kwargs.get("reason", ""))
        if skipping:  # no harness in this environment: the skip has to explain itself
            assert reason.strip(), f"{test.__name__} has a bare skip (no reason)"
            assert str(bunker_qa_script()) in reason, f"reason must name the probed path: {reason}"
            assert SCRIPT_ENV in reason, f"reason must name the override variable: {reason}"


# ── DF-GITREINS-POC-100 acceptance contract (needs the real harness) ──────────


@requires_bunker_qa
def test_harness_is_valid_bash() -> None:
    """``bash -n`` must succeed: the harness stays parseable after every edit."""
    result = _run("-n")

    assert result.returncode == 0, (
        f"bash -n failed on {bunker_qa_script()} (rc={result.returncode}). "
        f"stderr: {result.stderr[:500]}"
    )


@requires_bunker_qa
def test_disconnect_cell_has_a_fail_branch() -> None:
    """The chaos-disconnect cell must have a FAIL branch (the rc=124 hang arm).

    The brief's premise: a vacuous subject could fall through to
    "cell chaos-disconnect OK". The FAIL branch must exist so an empty or
    otherwise undecidable subject can never silently land as an OK verdict.
    """
    assert "cell chaos-disconnect FAIL" in _text(), (
        "bunker-qa.sh has no 'cell chaos-disconnect FAIL' branch — a vacuous "
        "chaos-disconnect subject can degrade into a bare OK again"
    )


@requires_bunker_qa
def test_fence_check_passes_with_zero_violations() -> None:
    """``__fence-check`` must exit 0 and report violations=0.

    The battery body is a heredoc-fence: every literal ``$var`` inside the
    battery must be escaped exactly once (\\$) so the generated qa-run.sh
    binds at agent runtime. A new unescaped substitution shifts EVERY later
    one — the fence detector exists because that drift shipped silently once
    before. This runs the harness's own detector.
    """
    result = _run("__fence-check")

    assert result.returncode == 0, (
        f"fence check failed (rc={result.returncode}). stdout: {result.stdout[:500]} "
        f"stderr: {result.stderr[:300]}"
    )
    assert "violations=0" in result.stdout, (
        f"fence check reported drift instead of violations=0: {result.stdout[:500]}"
    )


@requires_bunker_qa
def test_vacuity_arm_exists_and_fails_closed() -> None:
    """The empty-log arm must exist verbatim and grade UNVERIFIED, never OK.

    The exact vacuity sentence is load-bearing fleet language: downstream
    verdict readers (QA ledger, foreman evidence reconciliation) match on it.
    An EMPTY disc.log means the network cut was never exercised, so the grade
    must be UNVERIFIED (not a FAIL blaming the code, and never OK).
    """
    vacuity_lines = [line for line in _text().splitlines() if "disc.log is EMPTY" in line]

    assert vacuity_lines, (
        "bunker-qa.sh lost the 'disc.log is EMPTY' vacuity arm — an empty "
        "disconnect subject log would fall through to the graded branches again"
    )
    for line in vacuity_lines:
        assert "cell chaos-disconnect UNVERIFIED" in line, (
            f"the vacuity arm must grade UNVERIFIED, found: {line.strip()[:200]}"
        )
        assert "cell chaos-disconnect OK" not in line, (
            f"the vacuity arm must never grade OK: {line.strip()[:200]}"
        )


@requires_bunker_qa
def test_disconnect_log_valid_requires_marker_and_counts() -> None:
    """disconnect_log_valid must gate rc=0/1 on the cut marker AND suite output.

    POC-100's core contract: rc=0 alone can never stand as disconnect evidence.
    The harness guard needs the NETWORK_CUT_APPLIED marker line (proving the
    probe actually ran under cut) AND a non-zero suite pass/fail count (proving
    tests actually executed under cut).
    """
    text = _text()
    assert "disconnect_log_valid()" in text, (
        "bunker-qa.sh lost the disconnect_log_valid guard — rc=0/1 could stand "
        "alone as disconnect evidence again"
    )
    guard_body = text.split("disconnect_log_valid()", 1)[1]
    for required in ("NETWORK_CUT_APPLIED", "disconnect_suite_counts"):
        assert required in guard_body.split("\n\n", 1)[0] or required in guard_body[:4000], (
            f"disconnect_log_valid must reference {required!r} — the vacuity "
            "gate no longer requires the marker or the suite counts"
        )


@requires_bunker_qa
def test_collect_preserves_the_disconnect_subject_log() -> None:
    """Collect must pull the exact disc.log to <evidence>.chaos-disconnect.log.

    The vacuity fix is only auditable after the agent is destroyed if collect
    preserved the subject log: the artifact must be named after the evidence
    file (so each run's log is uniquely addressable), pulled from the agent
    even when the cell graded UNVERIFIED, and the evidence detail must record
    EITHER the captured path + size OR the explicit unavailability — never a
    detail with no way to re-read the subject.
    """
    text = _text()

    assert '"${evidence}.chaos-disconnect.log"' in text, (
        "collect must write the disconnect subject log to "
        "<evidence>.chaos-disconnect.log — a unique, re-readable path per run"
    )
    assert "${evidence}.chaos-disconnect.log" in text, (
        "collect lost the disconnect-log pull mechanism entirely"
    )
    assert "chaos-disconnect log captured at $disconnect_artifact" in text, (
        "the collect detail must record the captured artifact path + size so "
        "the graded verdict names a re-readable subject log"
    )
    assert "chaos-disconnect log unavailable/empty at $disconnect_artifact" in text, (
        "when the pull fails the detail must say so explicitly (UNVERIFIED "
        "evidence), never silently pass"
    )
