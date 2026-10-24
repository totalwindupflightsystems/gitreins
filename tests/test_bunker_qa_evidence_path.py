"""DF-GITREINS-POC-99: bunker-qa.sh launch --evidence writes to the caller's path.

The bug: launch() only parsed --keep, ignoring --evidence. When a caller passed
--evidence /custom/path.jsonl, launch wrote to the global default $EVIDENCE path
(/tmp/bunker-qa-evidence.jsonl), but collect() looked for /custom/path.jsonl.meta
which didn't exist. The caller's evidence file stayed empty (only launch rows),
while a SECOND evidence file at the harness default carried the actual cell rows.

Fix: launch() now parses --evidence and overrides the global $EVIDENCE before
writing anything.

INT-CI-19 — the harness is a HOST artifact, so these tests must not require it.
``bunker-qa.sh`` ships with the Hermes host (``~/.hermes/scripts/``); it has never
been part of this repository. The tests used to assume that path unconditionally,
so a clean checkout on a GitHub runner (whose ``$HOME`` is ``/home/runner``) died
with "bunker-qa.sh not found" — a test-environment assumption, not a product
defect (CI runs 40057464458 / 40052595299 / 40052244667). The contract now:

* the script is resolved from ``$GITREINS_BUNKER_QA_SCRIPT`` first, then the host
  default, so the same tests can drive any checkout of the harness (including a
  scratch copy on a dev box) without editing the file;
* when no script resolves, the three behavioral tests SKIP — with a reason that
  names the variable and the absolute path that was probed, never a bare skip;
* the resolver and the skip marker are pinned by tests that run everywhere, so
  the skip can never silently degrade into "this suite always skips" and cannot
  be satisfied by a wrong path;
* the live-host arm is preserved verbatim in intent: where the script exists the
  tests still exercise the REAL script, not a stub fixture.

The two launch arms also scope the harness' "global" evidence path to the test's
own tmp dir (``BUNKER_QA_EVIDENCE`` + ``BUNKER_QA_ALLOW_SHARED_EVIDENCE=1``). The
negative arm ("the global path must stay untouched") used to read the fixed path
``/tmp/bunker-qa-evidence.jsonl`` — which the harness no longer writes by default
(it defaults to a per-run unique name) and which the suite also unlinked, a
mutating, xdist-unsafe fixed path. Pointed at a tmp path, a regression that drops
the ``--evidence`` parse writes exactly that file, so the arm is mutation-sensitive
again instead of vacuous.

The live-system coverage these tests stand in for (a real battery on a real
bunker) belongs to the bunker-qa battery, not to a unit-test suite.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

#: Environment override for the harness path — the repository-hermetic seam.
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
    """Return why the harness is unavailable, or None when it is usable.

    The reason is the whole point of the skip: it has to say WHICH path was
    probed and HOW to supply one, or the next reader is left with a mystery
    skip on a green dashboard.
    """
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


def _run(
    script: Path, *args: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess:
    """Invoke the harness with the caller's arguments and an env overlay."""
    full_env = {**os.environ}
    full_env.update({k: str(v) for k, v in (env or {}).items()})
    return subprocess.run(
        ["bash", str(script), *args],
        capture_output=True,
        text=True,
        timeout=60,
        env=full_env,
    )


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
    """The reason must be WIRED to the tests, not merely defined next to them.

    Without this, deleting one decorator silently turns that test into a
    ``FileNotFoundError`` on a clean runner — exactly the INT-CI-19 red. The
    marks must be the ONE tested decorator (``requires_bunker_qa``), so there is
    a single reason to keep honest, and when that decorator is actually skipping
    (a runner with no harness) the reason must name the path and the override.
    """
    behavioural = (
        test_launch_parses_evidence_flag,
        test_launch_parses_evidence_equals_form,
        test_collect_reads_custom_evidence_meta,
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


# ── launch: the fix itself (needs the real harness) ───────────────────────────


@requires_bunker_qa
def test_launch_parses_evidence_flag(tmp_path: Path) -> None:
    """launch() must honor --evidence <path> and write to the caller's file."""
    script = bunker_qa_script()
    custom_evidence = tmp_path / "my-custom-evidence.jsonl"
    # The harness' own default, scoped to this test: if launch() ignores
    # --evidence it writes HERE, and the negative arm below catches it.
    shared_evidence = tmp_path / "harness-default-evidence.jsonl"

    # launch() will fail at preflights (no repo, no server), but it must write
    # the early meta record to the CUSTOM path, not the harness default.
    result = _run(
        script,
        "launch",
        "/nonexistent/repo",
        "--evidence",
        str(custom_evidence),
        env={
            "BUNKER_QA_EVIDENCE": str(shared_evidence),
            "BUNKER_QA_ALLOW_SHARED_EVIDENCE": "1",
        },
    )

    custom_touched = custom_evidence.exists() or Path(f"{custom_evidence}.meta").exists()
    default_touched = shared_evidence.exists() or Path(f"{shared_evidence}.meta").exists()

    assert custom_touched, (
        f"launch --evidence {custom_evidence} did not create the custom path or its .meta. "
        f"stderr: {result.stderr[:500]}"
    )
    assert not default_touched, (
        f"launch --evidence {custom_evidence} also wrote to the harness default "
        f"{shared_evidence} — the fix did not override the global. stderr: {result.stderr[:500]}"
    )


@requires_bunker_qa
def test_launch_parses_evidence_equals_form(tmp_path: Path) -> None:
    """launch() must honor --evidence=<path> (equals form)."""
    script = bunker_qa_script()
    custom_evidence = tmp_path / "equals-form.jsonl"
    shared_evidence = tmp_path / "harness-default-evidence.jsonl"

    result = _run(
        script,
        "launch",
        "/nonexistent/repo",
        f"--evidence={custom_evidence}",
        env={
            "BUNKER_QA_EVIDENCE": str(shared_evidence),
            "BUNKER_QA_ALLOW_SHARED_EVIDENCE": "1",
        },
    )

    custom_touched = custom_evidence.exists() or Path(f"{custom_evidence}.meta").exists()
    default_touched = shared_evidence.exists() or Path(f"{shared_evidence}.meta").exists()

    assert custom_touched, (
        f"launch --evidence={custom_evidence} did not create the custom path. "
        f"stderr: {result.stderr[:500]}"
    )
    assert not default_touched, (
        f"launch --evidence={custom_evidence} also wrote to the harness default "
        f"{shared_evidence}. stderr: {result.stderr[:500]}"
    )


# ── collect: reads the meta at the caller's path (needs the real harness) ─────


@requires_bunker_qa
def test_collect_reads_custom_evidence_meta(tmp_path: Path) -> None:
    """collect() must find the .meta at the caller's --evidence path.

    Two arms, both offline and deterministic, so neither can pass vacuously:

    * ``--evidence`` names the path that CARRIES the meta → collect reads it and
      reports the meta's own verdict (``no-agent``, because the recorded launch
      never reached the spawn step);
    * ``--evidence`` names a path with no meta → ``no-launch-record``.

    The differential is the contract: if collect ignored the flag, the first arm
    would report ``no-launch-record`` and fail; a missing/broken script can no
    longer "pass" by printing nothing (the INT-CI-19 shape, where this one test
    went green on the runner while the two launch tests failed).
    """
    script = bunker_qa_script()
    custom_evidence = tmp_path / "collect-test.jsonl"
    Path(f"{custom_evidence}.meta").write_text(
        "agent=\n"
        "server=test-server\n"
        f"evidence={custom_evidence}\n"
        "launched=2026-10-23T00:00:00Z\n"
        "keep=0\n"
        "project=test\n",
        encoding="utf-8",
    )

    with_meta = _run(script, "collect", "--evidence", str(custom_evidence))
    without_meta = _run(script, "collect", "--evidence", str(tmp_path / "no-such-evidence.jsonl"))

    # Arm 1 — the meta at the caller's path was found and parsed. (An empty
    # agent= line makes the verdict deterministic and needs no network: the
    # harness refuses to probe an id it cannot know.)
    assert "no-launch-record" not in with_meta.stdout, (
        f"collect --evidence {custom_evidence} said 'no launch record' even though "
        f"{custom_evidence}.meta exists. stdout: {with_meta.stdout}"
    )
    assert "COLLECT-FAIL no-agent" in with_meta.stdout, (
        f"collect did not read the meta at the caller's path (expected the harness' "
        f"'no-agent' verdict). stdout: {with_meta.stdout!r} stderr: {with_meta.stderr[:500]}"
    )
    # Arm 2 — the same probe against a path with no meta proves the flag selects
    # the path rather than arm 1 passing for some unrelated reason.
    assert "COLLECT-FAIL no-launch-record" in without_meta.stdout, (
        f"collect --evidence <path-without-meta> should report no-launch-record. "
        f"stdout: {without_meta.stdout!r} stderr: {without_meta.stderr[:500]}"
    )
