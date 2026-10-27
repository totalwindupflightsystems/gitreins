#!/usr/bin/env python3
"""Render and grade the IDENTITY of a bunker full-CI run.

REVIEW-GITREINS-011. The fleet runs the full CI suite on JIT bunker agents
(`scripts/bunker-full-ci.sh`). A battery that reports "suite PASS" without
saying *which build* it measured is not evidence: the prototype capture of
2026-09-23 shipped two trees exported with `git archive` (no `.git`) and both
sides reported the identical "17 failed / 25 errors", which reads as a version
finding and was an artifact of the export. The fix is not more prose — it is
an identity that a machine can grade and an operator cannot misread:

* the tested tree's commit, and the commit the deploy was asked for;
* the deployed CLI's version and its surface probe result
  (`scripts/check_deployed_surface.py` — REVIEW-001);
* whether the toolchain was created fresh or reused from a previous run.

This module is the single source of truth for that grading. It is stdlib-only
so it runs on the host, in the clone, and inside the test suite:

    scripts/bunker-full-ci.sh run ...        # the harness: collects facts, calls this
    python3 scripts/bunker_identity.py render --facts facts.json
    python3 scripts/bunker_identity.py render --facts - --json     # machine form
    python3 scripts/bunker_identity.py render --facts facts.json --line   # ledger note
    python3 scripts/bunker_identity.py self-test   # the graded fixture table

Exit codes (grading, not narration):

    0  IDENTITY-OK            — every identity fact agrees and the suite passed
    1  SUITE-FAIL             — identity is exact, the suite is red
    3  IDENTITY-MISMATCH /
       NOT-EVIDENCE           — the run does NOT describe the requested build:
                                wrong tree deployed, partial deploy, a CLI not
                                built from the tested tree, a CLI surface that
                                differs from the tree, a vacuous zero-test pass
    2  INFRA                  — the facts are unusable (missing/unparseable)

Exit 3 is deliberately distinct from 1: a red suite is a result about the
build, a mismatch is the absence of one. Neither is ever reported as PASS.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from typing import Any

EXIT_OK = 0
EXIT_SUITE_FAIL = 1
EXIT_INFRA = 2
EXIT_NOT_EVIDENCE = 3

#: LOUD markers. A mismatch must be greppable and must name the fact that
#: disagrees — "various problems were detected" is the failure mode this
#: module exists to remove.
MISMATCH_MARKER = "!! MISMATCH:"
WARN_MARKER = "!! WARN:"

_COUNT_WORDS = (
    "passed",
    "failed",
    "errors",
    "skipped",
    "xfailed",
    "xpassed",
    "deselected",
    "warnings",
)


def parse_pytest_counts(summary: str) -> dict[str, int]:
    """Counts from a pytest summary line.

    Handles the shapes this suite actually emits, measured on the 2026-09-23
    A/B capture:

        "17 failed, 2273 passed, 24 skipped, 25 errors in 71.20s"
        "2931 passed in 120.51s"
        "no tests ran in 0.01s"
        "=== 2 failed, 10 passed in 3.10s ==="
    """
    counts = {word: 0 for word in _COUNT_WORDS}
    text = summary.strip().strip("=").strip()
    for value, word in re.findall(r"(\d+)\s+([a-z]+)", text.lower()):
        word = "errors" if word == "error" else word
        if word in counts:
            counts[word] += int(value)
    return counts


def suite_ran(suite: dict[str, Any]) -> bool:
    """True when the runner executed (as opposed to never being reached)."""
    return bool(suite.get("ran", True))


def suite_scored(suite: dict[str, Any]) -> int:
    """Tests that produced a verdict (passed + failed + collection errors)."""
    return (
        int(suite.get("passed") or 0)
        + int(suite.get("failed") or 0)
        + int(suite.get("errors") or 0)
    )


def suite_total(suite: dict[str, Any]) -> int:
    """Everything pytest reported on, skips included."""
    return (
        suite_scored(suite)
        + int(suite.get("skipped") or 0)
        + int(suite.get("xfailed") or 0)
        + int(suite.get("xpassed") or 0)
    )


def is_storage_failure(suite: dict[str, Any]) -> bool:
    """A host-disk failure is infrastructure evidence, not a suite verdict."""
    text = "\n".join(
        str(suite.get(key) or "") for key in ("summary", "error", "stderr", "log_tail", "detail")
    ).lower()
    return "enospc" in text or "no space left on device" in text


def is_vacuous(suite: dict[str, Any]) -> bool:
    """A zero-test rc=0 run (QA-AUGER-1).

    The standing harness grades this FAIL rather than PASS: a suite that
    collected nothing is not a green build, it is the absence of a measurement
    that reads like one. The collection count — `pytest --collect-only -q
    --override-ini=addopts=`, the same mechanism scripts/check_docs_drift.py
    uses — is authoritative when present; the summary counts are the fallback.
    """
    if not suite_ran(suite) or int(suite.get("rc") or 0) != 0:
        return False
    collected = suite.get("collected")
    if collected is not None:
        return int(collected) == 0
    return suite_total(suite) == 0


def grade(facts: dict[str, Any]) -> tuple[int, list[str], list[str]]:
    """Grade one run's facts.

    Returns ``(exit_code, mismatches, warnings)``. Mismatches are identity
    problems (the run stops describing the requested build); warnings are facts
    the operator must see but which do not invalidate the identity.
    """
    mismatches: list[str] = []
    warnings: list[str] = []

    repo = facts.get("repo") or {}
    deployed = facts.get("deployed") or {}
    cli = facts.get("cli") or {}
    surface = facts.get("surface") or {}
    suite = facts.get("suite") or {}

    want_sha = str(repo.get("sha") or "")
    got_sha = str(deployed.get("sha") or "")

    if not want_sha:
        mismatches.append(
            f"no requested revision in the facts (ref={repo.get('ref')!r}) — nothing to identify"
        )
    elif not got_sha:
        mismatches.append("the deployed tree reported no HEAD — the clone/checkout never happened")
    elif want_sha != got_sha:
        mismatches.append(
            f"the tested tree is {got_sha[:12]} but {repo.get('ref')!r} resolves to "
            f"{want_sha[:12]} — the suite measured a different build than the one requested"
        )

    expected = deployed.get("expected_files")
    landed = deployed.get("files")
    if expected is not None and landed is not None and int(landed) != int(expected):
        mismatches.append(
            f"partial deploy: {landed} of {expected} tracked files landed on the agent "
            "(GAP-144 — assert the landed count before believing a timing)"
        )

    expected_bytes = deployed.get("bundle_expected_bytes")
    landed_bytes = deployed.get("bundle_bytes")
    if (
        expected_bytes is not None
        and landed_bytes is not None
        and int(landed_bytes) != int(expected_bytes)
    ):
        mismatches.append(
            f"the shipped bundle landed short: {landed_bytes} of {expected_bytes} bytes "
            "on the agent (GAP-144 — a partial scp leaves a tree that looks deployed)"
        )

    if cli.get("from_tree") is False:
        mismatches.append(
            f"the deployed CLI ({cli.get('path')}, version {cli.get('version')}) was NOT installed "
            "from the tested tree — the run would exercise different code than the clone"
        )

    repo_version = surface.get("repo_version")
    deployed_version = surface.get("deployed_version") or cli.get("version")
    if repo_version and deployed_version and str(repo_version) != str(deployed_version):
        mismatches.append(f"CLI version drift: tree {repo_version} vs deployed {deployed_version}")

    if surface:
        if surface.get("aligned") is False or int(surface.get("exit_code") or 0) != 0:
            detail = []
            missing = surface.get("missing_in_deployed") or []
            extra = surface.get("only_in_deployed") or []
            if missing:
                detail.append(f"missing from the deployed CLI: {', '.join(map(str, missing))}")
            if extra:
                detail.append(f"only in the deployed CLI: {', '.join(map(str, extra))}")
            if surface.get("error"):
                detail.append(str(surface["error"]))
            detail_text = f" ({'; '.join(detail)})" if detail else ""
            mismatches.append(
                "the deployed CLI surface does not match the tested tree "
                f"(scripts/check_deployed_surface.py rc={surface.get('exit_code')}){detail_text}"
            )

    if is_storage_failure(suite):
        mismatches.append(
            "infrastructure failure: native suite hit disk-capacity exhaustion "
            "(ENOSPC / No space left on device); preserve the complete artifact and "
            "leave dependent chaos cells UNVERIFIED"
        )
    elif is_vacuous(suite):
        mismatches.append(
            "vacuous pass: pytest exited 0 with ZERO tests collected — a zero-test suite is not "
            "evidence about the build (QA-AUGER-1)"
        )
    elif suite_ran(suite) and int(suite.get("rc") or 0) == 0 and suite_total(suite) == 0:
        mismatches.append(
            "the suite exited 0 but its summary line carried no counts "
            f"(summary={suite.get('summary')!r}) — the counts this run exists to "
            "record are unreadable, so a pass here would be a claim without a measurement"
        )

    toolchain = facts.get("toolchain") or {}
    if str(toolchain.get("mode") or "").lower() == "reused":
        where = toolchain.get("path") or "the agent's existing toolchain"
        warnings.append(
            f"toolchain REUSED ({where}) — the venv predates this run, so a stale CLI or "
            "stale dependencies can survive inside it; identity is only as good as the "
            "CLI/surface facts below"
        )
    pre_existing = cli.get("pre_existing")
    if pre_existing:
        warnings.append(
            f"a gitreins CLI already existed on the agent before this run ({pre_existing}) — the "
            "identity below names which one the probe resolved, and PATH order decides it"
        )

    if suite_ran(suite) and int(suite.get("rc") or 0) == 4 and suite_total(suite) == 0:
        warnings.append(
            "pytest exited 4 (usage error) and ran nothing — the requested test path is "
            "not in the tested tree; uncommitted paths are never in a clone, so commit "
            "before pointing the lane at a file"
        )

    if repo.get("dirty"):
        warnings.append(
            "the SOURCE tree has uncommitted changes: the clone contains the committed tree of "
            f"{want_sha[:12]}, so working-tree edits are NOT part of what was tested"
        )

    if is_storage_failure(suite):
        return EXIT_INFRA, mismatches, warnings
    if mismatches:
        return EXIT_NOT_EVIDENCE, mismatches, warnings
    if not suite_ran(suite) or int(suite.get("rc") or 0) != 0:
        return EXIT_SUITE_FAIL, mismatches, warnings
    return EXIT_OK, mismatches, warnings


def _fmt_sha(sha: Any) -> str:
    text = str(sha or "")
    return text[:12] + "…" if len(text) > 12 else (text or "?")


def _surface_line(surface: dict[str, Any]) -> str:
    if not surface:
        return "not probed"
    state = "ALIGNED" if surface.get("aligned") else "DRIFT"
    parts = [f"{state} (rc={surface.get('exit_code')})"]
    repo_n = surface.get("repo_subcommands")
    dep_n = surface.get("deployed_subcommands") or surface.get("installed_subcommands")
    if repo_n and dep_n:
        parts.append(f"tree {len(repo_n)} == deployed {len(dep_n)} subcommands")
    if not surface.get("aligned"):
        missing = surface.get("missing_in_deployed") or []
        extra = surface.get("only_in_deployed") or []
        if missing:
            parts.append(f"missing from the deployed CLI: {', '.join(map(str, missing))}")
        if extra:
            parts.append(f"only in the deployed CLI: {', '.join(map(str, extra))}")
    return " — ".join(parts) + " [scripts/check_deployed_surface.py]"


def render_lines(
    facts: dict[str, Any], exit_code: int, mismatches: list[str], warnings: list[str]
) -> list[str]:
    """The human block: what was tested, and whether it can be believed."""
    repo = facts.get("repo") or {}
    agent = facts.get("agent") or {}
    deployed = facts.get("deployed") or {}
    cli = facts.get("cli") or {}
    surface = facts.get("surface") or {}
    toolchain = facts.get("toolchain") or {}
    suite = facts.get("suite") or {}

    lines = ["==================== BUNKER FULL-CI IDENTITY ===================="]
    lines.append(f"mode            : {facts.get('mode', 'run')}")
    if agent.get("id"):
        lines.append(f"agent           : {agent.get('id')} @ {agent.get('server', '?')}")
    lines.append(
        f"requested       : {repo.get('path', '?')} ref={repo.get('ref', 'HEAD')} "
        f"sha={_fmt_sha(repo.get('sha'))}"
    )
    lines.append(
        "source tree     : "
        + ("DIRTY (uncommitted changes are not in the clone)" if repo.get("dirty") else "clean")
    )
    if deployed.get("sha") or deployed.get("dir") or deployed.get("files") is not None:
        expected = deployed.get("expected_files")
        files = (
            "files not measured"
            if deployed.get("files") is None
            else f"files {deployed.get('files')}"
        )
        if expected is not None:
            files += f"/{expected}"
        lines.append(
            f"tested tree     : {deployed.get('dir') or '?'} "
            f"sha={_fmt_sha(deployed.get('sha'))}  {files} landed"
        )
    else:
        lines.append("tested tree     : NOT DEPLOYED — no clone was measured")
    if cli.get("path"):
        lines.append(
            f"deployed CLI    : {cli.get('path')} version={cli.get('version') or '?'} "
            f"(from tested tree: {'yes' if cli.get('from_tree') else 'NO'})"
        )
    else:
        lines.append("deployed CLI    : NOT INSTALLED — no CLI was measured")
    lines.append(f"surface probe   : {_surface_line(surface)}")

    mode = str(toolchain.get("mode") or "?").upper()
    detail = toolchain.get("path") or toolchain.get("pre_existing") or "?"
    note = f" — {toolchain['note']}" if toolchain.get("note") else ""
    lines.append(f"toolchain       : {mode} ({detail}){note}")

    if suite_ran(suite):
        collected = suite.get("collected")
        lines.append(
            f"suite           : rc={suite.get('rc')} · {suite.get('passed', 0)} passed · "
            f"{suite.get('failed', 0)} failed · {suite.get('errors', 0)} errors · "
            f"{suite.get('skipped', 0)} skipped"
            + (f" (collected {collected})" if collected is not None else "")
            + (f" · {suite.get('duration_s')}s" if suite.get("duration_s") is not None else "")
        )
        if suite.get("summary"):
            lines.append(f"                  summary: {str(suite['summary']).strip()[:160]}")
        if suite.get("command"):
            lines.append(f"                  cmd: {suite['command']}")
        if suite.get("log"):
            lines.append(f"                  log: {suite['log']}")
    else:
        lines.append("suite           : NEVER RAN — no result, not a pass")

    for note in facts.get("notes") or []:
        lines.append(f"note            : {note}")

    for problem in mismatches:
        lines.append(f"{MISMATCH_MARKER} {problem}")
    for warning in warnings:
        lines.append(f"{WARN_MARKER} {warning}")

    if exit_code == EXIT_NOT_EVIDENCE:
        verdict = "IDENTITY-MISMATCH — this run is NOT evidence about the requested build"
    elif exit_code == EXIT_SUITE_FAIL:
        verdict = "IDENTITY-OK / SUITE-FAIL — the tested build is exact, its suite is red"
    else:
        verdict = "IDENTITY-OK / SUITE-PASS"
    lines.append(f"VERDICT         : {verdict} (exit {exit_code})")
    lines.append("=" * 65)
    return lines


def identity_line(facts: dict[str, Any], exit_code: int) -> str:
    """One line for `gitreins qa record --note` (counts, never just 'PASS')."""
    repo = facts.get("repo") or {}
    cli = facts.get("cli") or {}
    surface = facts.get("surface") or {}
    toolchain = facts.get("toolchain") or {}
    suite = facts.get("suite") or {}
    sha = str(repo.get("sha") or "")[:12]
    state = {
        EXIT_OK: "PASS",
        EXIT_SUITE_FAIL: "SUITE-FAIL",
        EXIT_NOT_EVIDENCE: "MISMATCH",
        EXIT_INFRA: "UNUSABLE",
    }.get(exit_code, "?")
    suite_rc = suite.get("rc", "ran" if suite_ran(suite) else "never-ran")
    return (
        f"bunker full-ci: sha={sha} cli={cli.get('version', '?')} "
        f"surface={'ALIGNED' if surface.get('aligned') else 'DRIFT'} "
        f"toolchain={str(toolchain.get('mode') or '?').upper()} rc={suite_rc} "
        f"tests={suite.get('passed', 0)}p/{suite.get('failed', 0)}f/"
        f"{suite.get('errors', 0)}e/{suite.get('skipped', 0)}s "
        f"verdict={state}"
    )


# ── the graded fixture table ────────────────────────────────────────────────
# One source of truth for `self-test` (the shell harness) and
# tests/test_bunker_identity.py. Every entry is a shape measured in the wild:
# the clean pass, the wrong-tree deploy, the partial deploy GAP-144 measured
# (49 of 290 files), the stale CLI REVIEW-001 describes, the vacuous pass
# QA-AUGER-1 grades FAIL, and the honest red suite from the 2026-09-23 A/B.

_SHA_A = "7629354691110303214709e15aa64a09a273d0d2"
_SHA_B = "1e65720ae48a6b76522e07e76ac1206df036cd8e"


def _facts(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "mode": "run",
        "agent": {"id": "abc123def", "server": "bunker-las-02"},
        "repo": {"path": "/home/kara/gitreins", "ref": "HEAD", "sha": _SHA_A, "dirty": False},
        "deployed": {
            "dir": "~/ci-gitreins",
            "sha": _SHA_A,
            "files": 1234,
            "expected_files": 1234,
            "bundle_bytes": 5312820,
            "bundle_expected_bytes": 5312820,
        },
        "cli": {"path": "~/ci-tools/venv/bin/gitreins", "version": "0.16.0", "from_tree": True},
        "surface": {
            "aligned": True,
            "exit_code": 0,
            "repo_version": "0.16.0",
            "deployed_version": "0.16.0",
            "repo_subcommands": ["guard", "judge", "qa"],
            "deployed_subcommands": ["guard", "judge", "qa"],
            "missing_in_deployed": [],
            "only_in_deployed": [],
        },
        "toolchain": {"mode": "fresh", "path": "~/ci-tools/venv (created by this run)"},
        "suite": {
            "ran": True,
            "rc": 0,
            "passed": 2931,
            "failed": 0,
            "errors": 0,
            "skipped": 16,
            "collected": 2947,
            "duration_s": 71,
            "summary": "2931 passed, 16 skipped in 71.20s",
            "command": ".venv/bin/python -m pytest -q -p no:cacheprovider",
            "log": "~/ci-full.log",
        },
    }
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            merged = dict(base[key])
            merged.update(value)
            base[key] = merged
        else:
            base[key] = value
    return base


#: (name, facts, expected exit, [markers that MUST appear], [markers that must NOT])
SELF_TEST_CASES: list[tuple[str, dict[str, Any], int, list[str], list[str]]] = [
    (
        "clean-pass-identity",
        _facts(),
        EXIT_OK,
        ["VERDICT         : IDENTITY-OK / SUITE-PASS", "toolchain       : FRESH"],
        [MISMATCH_MARKER],
    ),
    (
        "wrong-tree-deployed",
        _facts(deployed={"sha": _SHA_B}),
        EXIT_NOT_EVIDENCE,
        [MISMATCH_MARKER, "the tested tree is 1e65720ae48a but 'HEAD' resolves to 762935469111"],
        [],
    ),
    (
        "partial-deploy-49-of-290",
        _facts(deployed={"files": 49, "expected_files": 290}),
        EXIT_NOT_EVIDENCE,
        [MISMATCH_MARKER, "partial deploy: 49 of 290"],
        [],
    ),
    (
        "partial-bundle-ship",
        _facts(deployed={"bundle_bytes": 524288, "bundle_expected_bytes": 5312820}),
        EXIT_NOT_EVIDENCE,
        [MISMATCH_MARKER, "the shipped bundle landed short: 524288 of 5312820 bytes"],
        [],
    ),
    (
        "collected-but-summary-unreadable",
        _facts(
            suite={
                "rc": 0,
                "passed": 0,
                "failed": 0,
                "errors": 0,
                "skipped": 0,
                "collected": 2947,
                "summary": "",
            }
        ),
        EXIT_NOT_EVIDENCE,
        [MISMATCH_MARKER, "carried no counts"],
        [],
    ),
    (
        "stale-cli-mismatched-deploy",
        _facts(
            cli={"from_tree": False, "path": "/usr/local/bin/gitreins", "version": "0.14.0"},
            surface={
                "aligned": False,
                "exit_code": 1,
                "deployed_version": "0.14.0",
                "missing_in_deployed": ["qa", "preflight"],
                "only_in_deployed": [],
            },
        ),
        EXIT_NOT_EVIDENCE,
        [
            MISMATCH_MARKER,
            "was NOT installed from the tested tree",
            "CLI version drift: tree 0.16.0 vs deployed 0.14.0",
        ],
        [],
    ),
    (
        "native-enospc-is-infrastructure",
        _facts(
            suite={
                "rc": 120,
                "passed": 0,
                "failed": 0,
                "errors": 0,
                "summary": "OSError: [Errno 28] No space left on device",
                "stderr": "native suite output was truncated while writing native.log",
                "log_tail": "ENOSPC",
            }
        ),
        EXIT_INFRA,
        [MISMATCH_MARKER, "infrastructure failure", "ENOSPC", "UNVERIFIED"],
        ["IDENTITY-OK / SUITE-FAIL"],
    ),
    (
        "vacuous-zero-test-pass",
        _facts(
            suite={
                "passed": 0,
                "failed": 0,
                "errors": 0,
                "skipped": 0,
                "collected": 0,
                "rc": 0,
                "summary": "no tests ran in 0.01s",
            }
        ),
        EXIT_NOT_EVIDENCE,
        [MISMATCH_MARKER, "vacuous pass"],
        [],
    ),
    (
        "red-suite-identity-exact",
        _facts(
            suite={
                "rc": 1,
                "passed": 2273,
                "failed": 17,
                "errors": 25,
                "skipped": 24,
                "duration_s": 71,
                "summary": "17 failed, 2273 passed, 24 skipped, 25 errors in 71.20s",
            }
        ),
        EXIT_SUITE_FAIL,
        ["IDENTITY-OK / SUITE-FAIL", "2273 passed", "17 failed", "25 errors"],
        [MISMATCH_MARKER],
    ),
    (
        "reused-toolchain-warns-not-fails",
        _facts(
            toolchain={"mode": "reused", "path": "~/ci-tools/venv (pre-existing)"},
            cli={"pre_existing": "/usr/local/bin/gitreins 0.16.0"},
        ),
        EXIT_OK,
        [WARN_MARKER, "toolchain REUSED", "IDENTITY-OK / SUITE-PASS"],
        [MISMATCH_MARKER],
    ),
    (
        "dirty-source-tree-is-named",
        _facts(repo={"dirty": True}),
        EXIT_OK,
        [
            WARN_MARKER,
            "source tree     : DIRTY",
            "working-tree edits are NOT part of what was tested",
        ],
        [MISMATCH_MARKER],
    ),
    (
        "suite-never-ran",
        _facts(suite={"ran": False, "rc": None}),
        EXIT_SUITE_FAIL,
        ["NEVER RAN", "IDENTITY-OK / SUITE-FAIL"],
        [MISMATCH_MARKER],
    ),
]


# ── facts assembly ──────────────────────────────────────────────────────────
# The remote runner (`scripts/bunker-full-ci.sh` phase 1 + phase 2 script)
# prints one `FACT key=value` line per measured fact. Parsing and assembly live
# here, in the tested module, so the shell never has to build JSON by hand and
# a missing fact is visible (absent key → the field is unknown, never guessed).


def parse_fact_lines(text: str) -> dict[str, str]:
    """`FACT key=value` lines → a dict (last value wins, blanks mean 'unknown')."""
    facts: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("FACT "):
            continue
        key, _, value = stripped[5:].partition("=")
        key = key.strip()
        if key:
            facts[key] = value.strip()
    return facts


def _as_int(value: Any) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _as_bool(value: Any) -> bool | None:
    text = str(value).strip().lower()
    if text in ("1", "true", "yes"):
        return True
    if text in ("0", "false", "no"):
        return False
    return None


def assemble(requested: dict[str, Any], fact_lines: str, *, mode: str = "run") -> dict[str, Any]:
    """Build the facts dict from the local request + the runner's FACT lines."""
    facts = parse_fact_lines(fact_lines)

    surface: dict[str, Any] = {}
    raw_surface = facts.get("surface_json")
    if raw_surface:
        try:
            surface = json.loads(raw_surface)
        except json.JSONDecodeError:
            surface = {
                "error": f"unparseable surface probe output: {raw_surface[:200]}",
                "exit_code": 1,
                "aligned": False,
            }
    if facts.get("surface_rc"):
        surface.setdefault("exit_code", _as_int(facts["surface_rc"]))
    if surface:
        surface.setdefault("aligned", int(surface.get("exit_code") or 0) == 0)

    suite: dict[str, Any] = {"ran": False}
    if facts.get("suite_ran"):
        summary = facts.get("suite_summary", "")
        counts = parse_pytest_counts(summary)
        suite = {
            "ran": _as_bool(facts.get("suite_ran")),
            "rc": _as_int(facts.get("suite_rc")),
            "duration_s": _as_int(facts.get("suite_duration_s")),
            "summary": summary or None,
            "command": facts.get("suite_cmd") or None,
            "log": facts.get("suite_log") or None,
            "passed": _as_int(facts.get("suite_passed"))
            if facts.get("suite_passed")
            else counts["passed"],
            "failed": _as_int(facts.get("suite_failed"))
            if facts.get("suite_failed")
            else counts["failed"],
            "errors": _as_int(facts.get("suite_errors"))
            if facts.get("suite_errors")
            else counts["errors"],
            "skipped": _as_int(facts.get("suite_skipped"))
            if facts.get("suite_skipped")
            else counts["skipped"],
            "collected": _as_int(facts.get("suite_collected")),
            "collect_command": facts.get("collect_cmd") or None,
        }

    toolchain = {
        "mode": facts.get("toolchain_mode") or None,
        "path": facts.get("toolchain_path") or None,
        "note": facts.get("toolchain_note") or None,
        "pre_existing": facts.get("pre_existing_venv") or None,
    }

    deployed: dict[str, Any] = {
        "dir": facts.get("deployed_dir") or None,
        "sha": facts.get("deployed_sha") or None,
        "files": _as_int(facts.get("deployed_files")),
        "expected_files": _as_int(facts.get("deployed_expected_files")),
        "bundle_bytes": _as_int(facts.get("bundle_bytes")),
        "bundle_expected_bytes": _as_int(facts.get("bundle_expected_bytes")),
    }
    if deployed["expected_files"] is None:
        deployed["expected_files"] = _as_int(requested.get("expected_files"))

    cli = {
        "path": facts.get("cli_path") or None,
        "version": facts.get("cli_version") or None,
        "dist_root": facts.get("cli_dist_root") or None,
        "from_tree": _as_bool(facts.get("cli_from_tree")) if facts.get("cli_from_tree") else None,
        "pre_existing": facts.get("pre_existing_cli") or None,
    }

    return {
        "mode": mode,
        "agent": {"id": facts.get("agent_id") or None, "server": facts.get("agent_server") or None},
        "repo": {
            "path": requested.get("repo_path") or facts.get("repo_path") or None,
            "ref": requested.get("ref") or facts.get("requested_ref") or "HEAD",
            "sha": requested.get("sha") or facts.get("requested_sha") or None,
            "dirty": requested.get("dirty"),
        },
        "deployed": deployed,
        "cli": cli,
        "surface": surface,
        "toolchain": toolchain,
        "suite": suite,
        "notes": [note for note in [facts.get("notes")] if note],
    }


def run_self_test() -> int:
    failures = 0
    print(f"bunker_identity self-test — {len(SELF_TEST_CASES)} cases")
    for name, facts, expected_exit, must, must_not in SELF_TEST_CASES:
        exit_code, mismatches, warnings = grade(facts)
        text = "\n".join(render_lines(facts, exit_code, mismatches, warnings))
        problems = []
        if exit_code != expected_exit:
            problems.append(f"exit {exit_code} != expected {expected_exit}")
        for marker in must:
            if marker not in text:
                problems.append(f"missing marker: {marker!r}")
        for marker in must_not:
            if marker in text:
                problems.append(f"unexpected marker: {marker!r}")
        status = "PASS" if not problems else "FAIL"
        print(f"  {status}  {name} (exit {exit_code})")
        for problem in problems:
            print(f"        {problem}")
            failures += 1
    graded = len(SELF_TEST_CASES) - min(failures, len(SELF_TEST_CASES))
    print(f"self-test {'PASS' if not failures else 'FAIL'}: {graded}/{len(SELF_TEST_CASES)} cases")
    return 0 if not failures else 1


def _load_facts(path: str) -> dict[str, Any]:
    if path == "-":
        return json.load(sys.stdin)
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command")

    render = sub.add_parser("render", help="render and grade a run's identity facts")
    render.add_argument("--facts", required=True, help="JSON facts file, or - for stdin")
    render.add_argument("--json", dest="as_json", action="store_true", help="emit the machine form")
    render.add_argument("--line", action="store_true", help="emit only the one-line ledger note")

    collect = sub.add_parser("collect", help="assemble facts from the runner's FACT lines")
    collect.add_argument(
        "--facts-lines", required=True, help="file holding the runner's FACT lines (- for stdin)"
    )
    collect.add_argument(
        "--requested-sha", default=None, help="the commit the deploy was asked for"
    )
    collect.add_argument("--ref", default="HEAD")
    collect.add_argument("--repo-path", default=None)
    collect.add_argument(
        "--expected-files", type=int, default=None, help="tracked files at the requested commit"
    )
    collect.add_argument(
        "--dirty", action="store_true", help="the source tree has uncommitted changes"
    )
    collect.add_argument("--mode", default="run")
    collect.add_argument(
        "--out", default=None, help="write the assembled facts here (default: stdout)"
    )
    collect.add_argument(
        "--grade", action="store_true", help="render the identity block and exit with its grade"
    )

    sub.add_parser("self-test", help="run the graded fixture table")

    args = parser.parse_args(argv)

    if args.command == "self-test":
        return run_self_test()

    if args.command == "collect":
        try:
            if args.facts_lines == "-":
                fact_lines = sys.stdin.read()
            else:
                with open(args.facts_lines, encoding="utf-8") as handle:
                    fact_lines = handle.read()
        except OSError as exc:
            print(f"bunker_identity: cannot read the runner's facts: {exc}", file=sys.stderr)
            return EXIT_INFRA
        facts = assemble(
            {
                "sha": args.requested_sha,
                "ref": args.ref,
                "repo_path": args.repo_path,
                "expected_files": args.expected_files,
                "dirty": bool(args.dirty),
            },
            fact_lines,
            mode=args.mode,
        )
        payload = json.dumps(facts, indent=2)
        if args.out:
            try:
                with open(args.out, "w", encoding="utf-8") as handle:
                    handle.write(payload + "\n")
            except OSError as exc:
                print(f"bunker_identity: cannot write {args.out}: {exc}", file=sys.stderr)
                return EXIT_INFRA
        elif not args.grade:
            print(payload)
        if not args.grade:
            return EXIT_OK
        exit_code, mismatches, warnings = grade(facts)
        print("\n".join(render_lines(facts, exit_code, mismatches, warnings)))
        return exit_code

    if args.command != "render":
        parser.print_help()
        return EXIT_INFRA

    try:
        facts = _load_facts(args.facts)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"bunker_identity: unusable facts: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_INFRA

    exit_code, mismatches, warnings = grade(facts)

    if args.line:
        print(identity_line(facts, exit_code))
        return exit_code

    if args.as_json:
        print(
            json.dumps(
                {
                    "exit_code": exit_code,
                    "verdict": {
                        EXIT_OK: "IDENTITY-OK/SUITE-PASS",
                        EXIT_SUITE_FAIL: "IDENTITY-OK/SUITE-FAIL",
                        EXIT_NOT_EVIDENCE: "IDENTITY-MISMATCH",
                        EXIT_INFRA: "INFRA",
                    }[exit_code],
                    "identity_line": identity_line(facts, exit_code),
                    "mismatches": mismatches,
                    "warnings": warnings,
                    "facts": facts,
                },
                indent=2,
            )
        )
        return exit_code

    print("\n".join(render_lines(facts, exit_code, mismatches, warnings)))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
