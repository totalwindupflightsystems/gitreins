"""Identity grading for the bunker full-CI lane (REVIEW-GITREINS-011).

The lane's whole value is that a run cannot be misread: scripts/bunker-full-ci.sh
collects identity facts on the agent, scripts/bunker_identity.py grades them, and
this module pins both halves:

* the counts parser, against the summary strings the suite ACTUALLY emits
  (including the 2026-09-23 A/B capture, whose identical "17 failed / 25 errors"
  on both revisions was an artifact of a git-less export — the finding this lane
  exists to make impossible);
* the graded fixture table (module.SELF_TEST_CASES) that `bunker-full-ci.sh
  self-test` runs, so the CLI path and the unit path can never drift apart;
* the FACT-line assembly path, which is the only place the agent's output
  becomes facts;
* the recipe's local preflight, run from a fresh repo with no toolchain.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "bunker_identity.py"
HARNESS_PATH = REPO_ROOT / "scripts" / "bunker-full-ci.sh"
DOC_PATH = REPO_ROOT / "docs" / "bunker-qa.md"


def _load_module():
    """Load scripts/bunker_identity.py by path (it is a script, not a package)."""
    spec = importlib.util.spec_from_file_location("bunker_identity_under_test", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


identity = _load_module()


# ── the counts parser ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("summary", "expected"),
    [
        # The 2026-09-23 capture, both sides of the A/B (git-less export: the
        # identical counts were an environment artifact, not a version finding).
        (
            "17 failed, 2273 passed, 24 skipped, 25 errors in 71.20s",
            {"passed": 2273, "failed": 17, "errors": 25, "skipped": 24},
        ),
        (
            "17 failed, 2203 passed, 24 skipped, 25 errors in 98.41s",
            {"passed": 2203, "failed": 17, "errors": 25, "skipped": 24},
        ),
        ("2931 passed in 120.51s", {"passed": 2931, "failed": 0, "errors": 0}),
        ("2931 passed, 16 skipped in 71.20s", {"passed": 2931, "skipped": 16}),
        ("1 error in 0.51s", {"errors": 1, "passed": 0}),
        ("no tests ran in 0.01s", {"passed": 0, "failed": 0, "errors": 0}),
        ("=== 2 failed, 10 passed in 3.10s ===", {"failed": 2, "passed": 10}),
        (
            "5 failed, 3 xfailed, 2 xpassed, 1 deselected, 4 skipped in 9.00s",
            {"failed": 5, "xfailed": 3, "xpassed": 2, "deselected": 1, "skipped": 4},
        ),
        ("", {"passed": 0, "failed": 0, "errors": 0, "skipped": 0}),
    ],
)
def test_parse_pytest_counts(summary: str, expected: dict[str, int]) -> None:
    counts = identity.parse_pytest_counts(summary)
    for word, value in expected.items():
        assert counts[word] == value, f"{word} in {summary!r}"


def test_parse_pytest_counts_never_sums_a_failure_count_into_passes() -> None:
    """A red suite must never be summarisable as green."""
    counts = identity.parse_pytest_counts("17 failed, 2273 passed, 25 errors in 71.20s")
    assert counts["passed"] == 2273
    assert counts["failed"] == 17
    assert identity.suite_scored({"passed": 2273, "failed": 17, "errors": 25}) == 2315


# ── the graded fixture table (shared with `bunker-full-ci.sh self-test`) ────


def test_self_test_table_is_not_empty() -> None:
    assert len(identity.SELF_TEST_CASES) >= 8


def test_native_enospc_is_infrastructure_and_not_a_suite_result() -> None:
    """Disk exhaustion must not be reported as a product suite failure."""
    facts = identity._facts(
        suite={
            "rc": 120,
            "summary": "OSError: [Errno 28] No space left on device",
            "stderr": "native.log write failed: ENOSPC",
        }
    )
    exit_code, mismatches, warnings = identity.grade(facts)
    text = "\n".join(identity.render_lines(facts, exit_code, mismatches, warnings))
    assert exit_code == identity.EXIT_INFRA
    assert "infrastructure failure" in text
    assert "ENOSPC" in text
    assert "UNVERIFIED" in text
    assert "IDENTITY-OK / SUITE-FAIL" not in text


@pytest.mark.parametrize(
    "name,facts,expected_exit,must,must_not",
    identity.SELF_TEST_CASES,
    ids=[case[0] for case in identity.SELF_TEST_CASES],
)
def test_graded_cases(name, facts, expected_exit, must, must_not) -> None:
    exit_code, mismatches, warnings = identity.grade(facts)
    text = "\n".join(identity.render_lines(facts, exit_code, mismatches, warnings))
    assert exit_code == expected_exit, f"{name}: exit {exit_code} != {expected_exit}\n{text}"
    for marker in must:
        assert marker in text, f"{name}: missing marker {marker!r}\n{text}"
    for marker in must_not:
        assert marker not in text, f"{name}: unexpected marker {marker!r}\n{text}"


def test_mismatch_lines_are_loud_and_name_the_fact() -> None:
    facts = identity._facts(deployed={"sha": "0" * 40})
    exit_code, mismatches, warnings = identity.grade(facts)
    assert exit_code == identity.EXIT_NOT_EVIDENCE
    assert mismatches, "a wrong-tree deploy must produce a mismatch"
    text = "\n".join(identity.render_lines(facts, exit_code, mismatches, warnings))
    assert identity.MISMATCH_MARKER in text
    assert "NOT evidence" in text


def test_exit_codes_are_distinct_by_class() -> None:
    """A red suite (1) and a mismatched run (3) must not collapse together."""
    assert identity.EXIT_OK == 0
    assert identity.EXIT_SUITE_FAIL == 1
    assert identity.EXIT_INFRA == 2
    assert identity.EXIT_NOT_EVIDENCE == 3
    assert (
        len(
            {
                identity.EXIT_OK,
                identity.EXIT_SUITE_FAIL,
                identity.EXIT_INFRA,
                identity.EXIT_NOT_EVIDENCE,
            }
        )
        == 4
    )


def test_identity_line_carries_counts_not_just_pass() -> None:
    """The ledger note must carry the numbers, never a bare verdict word."""
    facts = identity._facts()
    _, mismatches, _ = identity.grade(facts)
    assert mismatches == []
    line = identity.identity_line(facts, identity.EXIT_OK)
    assert "sha=762935469111" in line
    assert "cli=0.16.0" in line
    assert "surface=ALIGNED" in line
    assert "toolchain=FRESH" in line
    assert "2931p" in line and "16s" in line


# ── FACT-line assembly (the agent's output → facts) ─────────────────────────

_PHASE_FACTS = "\n".join(
    [
        "FACT requested_sha=7629354691110303214709e15aa64a09a273d0d2",
        "FACT requested_ref=HEAD",
        "FACT deployed_expected_files=386",
        "FACT repo_path=/home/ci-gitreins",
        "FACT agent_id=abc123def",
        "FACT agent_server=bunker-las-02",
        "FACT pre_existing_venv=",
        "FACT pre_existing_cli=/usr/local/bin/gitreins",
        "FACT toolchain_mode=fresh",
        "FACT toolchain_note=venv created by this run",
        "FACT toolchain_path=/home/ci-tools/venv",
        "FACT bundle_bytes=5312820",
        "FACT bundle_expected_bytes=5312820",
        "PHASE1-OK deployed=762935469111 files=386 cli=0.16.0 from_tree=1 surface_rc=0",
        "FACT deployed_sha=7629354691110303214709e15aa64a09a273d0d2",
        "FACT deployed_files=386",
        "FACT deployed_dir=/home/ci-gitreins",
        "FACT cli_path=/home/ci-tools/venv/bin/gitreins",
        "FACT cli_version=0.16.0",
        "FACT cli_dist_root=/home/ci-gitreins",
        "FACT cli_from_tree=1",
        "FACT surface_rc=0",
        'FACT surface_json={"repo_version": "0.16.0", "installed_version": "0.16.0", '
        '"missing_in_deployed": [], "only_in_deployed": [], "aligned": true, "exit_code": 0, '
        '"repo_subcommands": ["guard", "judge"], "installed_subcommands": ["guard", "judge"]}',
        "FACT suite_cmd=/home/ci-tools/venv/bin/python -m pytest -q -p no:cacheprovider",
        "FACT collect_cmd=/home/ci-tools/venv/bin/python -m pytest --collect-only -q --override-ini=addopts=",
        "FACT collect_rc=0",
        "FACT suite_collected=2947",
        "FACT suite_ran=1",
        "FACT suite_rc=0",
        "FACT suite_duration_s=71",
        "FACT suite_summary=2931 passed, 16 skipped in 71.20s",
        "PHASE2-OK rc=0 duration=71s",
    ]
)

_REQUESTED = {
    "sha": "7629354691110303214709e15aa64a09a273d0d2",
    "ref": "HEAD",
    "expected_files": 386,
}


def test_parse_fact_lines_ignores_everything_that_is_not_a_fact() -> None:
    parsed = identity.parse_fact_lines(_PHASE_FACTS)
    assert parsed["deployed_files"] == "386"
    assert parsed["cli_version"] == "0.16.0"
    # the human progress lines are not facts
    assert (
        "PHASE1-OK deployed=762935469111 files=386 cli=0.16.0 from_tree=1 surface_rc=0"
        not in parsed
    )


def test_assemble_grades_a_clean_deploy_as_evidence() -> None:
    facts = identity.assemble(_REQUESTED, _PHASE_FACTS)
    exit_code, mismatches, warnings = identity.grade(facts)
    assert exit_code == identity.EXIT_OK, mismatches
    assert mismatches == []
    assert facts["deployed"]["sha"] == _REQUESTED["sha"]
    assert facts["deployed"]["files"] == 386
    assert facts["cli"]["version"] == "0.16.0"
    assert facts["cli"]["from_tree"] is True
    assert facts["surface"]["aligned"] is True
    assert facts["toolchain"]["mode"] == "fresh"
    assert facts["suite"]["passed"] == 2931
    assert facts["suite"]["skipped"] == 16
    assert facts["suite"]["collected"] == 2947
    assert any("pre-existing" in w or "already existed" in w for w in warnings), warnings


def test_assemble_reports_a_deploy_that_never_happened() -> None:
    """Phase 1 dying before the clone leaves no deployed sha — and no pass."""
    facts = identity.assemble(
        _REQUESTED,
        "\n".join(
            sorted(
                _line
                for _line in _PHASE_FACTS.splitlines()
                if not _line.startswith("FACT deployed_")
            )
        ),
    )
    exit_code, mismatches, _ = identity.grade(facts)
    assert exit_code == identity.EXIT_NOT_EVIDENCE
    assert any("no HEAD" in problem for problem in mismatches), mismatches


def test_assemble_survives_unparseable_surface_output() -> None:
    facts = identity.assemble(_REQUESTED, _PHASE_FACTS + "\nFACT surface_json=not json at all")
    # the last write wins: an unparseable probe is drift, never a silent pass
    assert facts["surface"]["aligned"] is False
    exit_code, mismatches, _ = identity.grade(facts)
    assert exit_code == identity.EXIT_NOT_EVIDENCE
    assert any("surface" in problem for problem in mismatches), mismatches


def test_vacuous_pass_is_graded_not_evidence() -> None:
    """QA-AUGER-1: rc=0 with a zero collection is FAIL, never PASS."""
    vacuous = _PHASE_FACTS.replace("FACT suite_collected=2947", "FACT suite_collected=0").replace(
        "FACT suite_summary=2931 passed, 16 skipped in 71.20s",
        "FACT suite_summary=no tests ran in 0.01s",
    )
    facts = identity.assemble(_REQUESTED, vacuous)
    exit_code, mismatches, _ = identity.grade(facts)
    assert exit_code == identity.EXIT_NOT_EVIDENCE
    assert any("vacuous" in problem for problem in mismatches), mismatches


# ── the recipe itself ──────────────────────────────────────────────────────


def test_module_and_harness_are_present() -> None:
    assert SCRIPT_PATH.is_file()
    assert HARNESS_PATH.is_file()
    assert HARNESS_PATH.stat().st_mode & 0o111, "the recipe must be executable"


def test_module_self_test_exits_zero() -> None:
    """`bunker-full-ci.sh self-test` is the offline proof the gate works."""
    proc = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "self-test"], capture_output=True, text=True
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "self-test PASS" in proc.stdout
    assert "clean-pass-identity" in proc.stdout
    assert "stale-cli-mismatched-deploy" in proc.stdout


def test_harness_self_test_mode_runs_the_table() -> None:
    proc = subprocess.run(["bash", str(HARNESS_PATH), "self-test"], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "self-test PASS" in proc.stdout


def _init_temp_repo(path: Path) -> str:
    """A fresh, minimal repo — the 'clean clone' the criterion names."""
    path.mkdir(parents=True, exist_ok=True)
    env = {
        "GIT_AUTHOR_NAME": "Bunker Test",
        "GIT_AUTHOR_EMAIL": "bunker-test@example.invalid",
        "GIT_COMMITTER_NAME": "Bunker Test",
        "GIT_COMMITTER_EMAIL": "bunker-test@example.invalid",
        "PATH": "/usr/bin:/bin",
    }
    subprocess.run(["git", "init", "-q", "--initial-branch=main", str(path)], check=True, env=env)
    (path / "hello.txt").write_text("hello\n")
    subprocess.run(["git", "-C", str(path), "add", "hello.txt"], check=True, env=env)
    subprocess.run(["git", "-C", str(path), "commit", "-q", "-m", "init"], check=True, env=env)
    return subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()


def test_pytest_usage_error_is_named_in_the_warnings() -> None:
    """rc=4 means nothing ran — the lane must say why, not only 'red'."""
    usage = (
        _PHASE_FACTS.replace("FACT suite_rc=0", "FACT suite_rc=4")
        .replace(
            "FACT suite_summary=2931 passed, 16 skipped in 71.20s",
            "FACT suite_summary=no tests ran in 0.00s",
        )
        .replace("FACT suite_collected=2947", "FACT suite_collected=0")
    )
    facts = identity.assemble(_REQUESTED, usage)
    exit_code, mismatches, warnings = identity.grade(facts)
    assert exit_code == identity.EXIT_SUITE_FAIL, (exit_code, mismatches)
    assert any("usage error" in warning for warning in warnings), warnings


def test_rendered_block_names_the_toolchain_note() -> None:
    facts = identity.assemble(_REQUESTED, _PHASE_FACTS)
    text = "\n".join(identity.render_lines(facts, identity.EXIT_OK, [], []))
    assert "venv created by this run" in text


def _harness_function(name: str) -> str:
    """Return the full `name() { ... }` definition from the in-repo harness.

    Extracted rather than re-implemented so the test drives the SAME shell code
    the lane runs; a copy would pass after the real function regressed.
    """
    text = HARNESS_PATH.read_text()
    start = re.search(rf"(?m)^\s*{re.escape(name)}\(\) \{{\n", text)
    assert start, f"{name}() must be defined in {HARNESS_PATH.name}"
    tail = text[start.end() :]
    end = re.search(r"(?m)^\s*\}\n", tail)
    assert end, f"{name}() must close at the definition's own indentation"
    definition = f"{name}() {{\n" + tail[: end.end()]
    assert definition.count("{") == definition.count("}"), "unbalanced braces"
    return definition


def _run_cleanup(
    tmp_path: Path, agent_uid: str | None, agent: str = "abc123", probe: str = "4242"
) -> list[str]:
    """Run the harness teardown with a stub `bunker`, returning the recorded calls."""
    calls = tmp_path / "calls.log"
    stub = "\n".join(
        [
            "set -uo pipefail",
            "log() { :; }",
            # `bunker run ... id -u` is the uid probe; everything else is recorded.
            "bunker() {",
            '    case "$*" in',
            f'        *"id -u"*) printf "%s\\n" "{probe}" ;;',
            '        *) printf "%s\\n" "$*" >> "$CALLS" ;;',
            "    esac",
            "}",
            f"AGENT={agent}",
            "SERVER=testbox",
            "spawned=1",
            "KEEP=0",
        ]
    )
    if agent_uid is not None:
        stub += f"\nAGENT_UID={agent_uid}"
    stub += (
        "\n"
        + _harness_function("stop_agent_daemons")
        + _harness_function("cleanup")
        + "\ncleanup\n"
    )
    script = tmp_path / "teardown.sh"
    script.write_text(stub, encoding="utf-8")
    result = subprocess.run(
        ["bash", str(script)],
        env={**os.environ, "CALLS": str(calls)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return calls.read_text(encoding="utf-8").splitlines() if calls.exists() else []


def test_teardown_trap_reads_only_defaulted_variables() -> None:
    """The EXIT trap fires after the function's locals are gone — under `set -u`
    an unguarded read masks the run's own exit code with a teardown error.

    The same trap also has to stop the battery's own daemons (dbus-daemon,
    sshd) under the disposable agent uid BEFORE `bunker destroy`: destroy
    refuses while they are alive, which is how every graded run became a
    manual recovery (DF-GITREINS-POC-101).  Both contracts are pinned against
    the IN-REPO harness, so the fix is reproducible from a clone.
    """
    text = HARNESS_PATH.read_text()
    assert "${spawned:-0}" in text

    helper = _harness_function("stop_agent_daemons")
    cleanup = _harness_function("cleanup")
    for marker in ("pkill", "dbus-daemon", "sshd", "-u "):
        assert marker in helper, f"stop_agent_daemons() does not name {marker!r}"
    # ordering matters: stopping after the destroy is the bug, not the fix
    assert "stop_agent_daemons" in cleanup, "cleanup() never calls the daemon stop"
    assert cleanup.index("stop_agent_daemons") < cleanup.index("bunker destroy"), (
        "the daemons must be stopped BEFORE bunker destroy, which refuses live agents"
    )


def test_teardown_stops_agent_daemons_before_destroy(tmp_path: Path) -> None:
    """Behaviour of the extracted trap: TERM the uid's daemons, then destroy."""
    observed = _run_cleanup(tmp_path, agent_uid="4242")
    kills = [line for line in observed if "pkill" in line]
    assert kills, f"teardown never killed the agent daemons: {observed}"
    assert any("pkill -TERM -u 4242 -x dbus-daemon" in line for line in kills)
    assert any("pkill -TERM -u 4242 -x sshd" in line for line in kills)
    destroy = [index for index, line in enumerate(observed) if line.startswith("destroy ")]
    assert destroy, f"teardown never destroyed the agent: {observed}"
    assert observed.index(kills[-1]) < destroy[0], "daemons must die before destroy"


def test_teardown_resolves_the_uid_when_it_was_not_captured(tmp_path: Path) -> None:
    """The trap cannot rely on a variable the run may never have set."""
    observed = _run_cleanup(tmp_path, agent_uid=None)
    assert any("pkill -TERM -u 4242 -x dbus-daemon" in line for line in observed)
    assert any(line.startswith("destroy ") for line in observed)


@pytest.mark.parametrize("uid", ["not-a-uid", "0"])
def test_teardown_refuses_an_unusable_uid(tmp_path: Path, uid: str) -> None:
    """A non-numeric or root uid must never reach pkill (see AGENTS.md)."""
    observed = _run_cleanup(tmp_path, agent_uid=uid)
    assert not [line for line in observed if "pkill" in line], observed
    assert any(line.startswith("destroy ") for line in observed), observed


def test_teardown_skips_the_kill_when_the_uid_probe_is_unusable(tmp_path: Path) -> None:
    """An unreachable agent yields no uid — the teardown must still destroy it."""
    observed = _run_cleanup(tmp_path, agent_uid=None, probe="")
    assert not [line for line in observed if "pkill" in line], observed
    assert any(line.startswith("destroy ") for line in observed), observed


def test_dry_run_works_from_a_clean_repo_without_a_bunker(tmp_path: Path) -> None:
    """The documented local preflight: no agent, no bunker CLI, no toolchain."""
    repo = tmp_path / "clean-clone"
    sha = _init_temp_repo(repo)
    # Prove the preflight does not depend on the bunker CLI: run it with every
    # PATH entry that carries a `bunker` binary removed (there is more than one
    # on a fleet host — ~/.local/bin and ~/go/bin both held one).
    env = dict(os.environ)
    env["PATH"] = os.pathsep.join(
        entry
        for entry in env.get("PATH", "").split(os.pathsep)
        if entry and not (Path(entry) / "bunker").exists()
    )
    assert shutil.which("bunker", path=env["PATH"]) is None
    proc = subprocess.run(
        ["bash", str(HARNESS_PATH), "dry-run", "--repo", str(repo)],
        capture_output=True,
        text=True,
        env=env,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    out = proc.stdout
    assert "BUNKER FULL-CI — DRY RUN" in out
    assert sha[:12] in out, "the preflight must name the commit it would test"
    assert "tracked files   : 1" in out
    assert "NOT EVALUATED" in out
    # the two scripts that will run on the agent are shipped verbatim from here,
    # so the preflight has to show them
    assert "PHASE 1 — deploy identity" in out
    assert "PHASE 2 — the suite" in out


def test_harness_documents_the_grades_in_its_own_header() -> None:
    """The contract an operator reads lives in the script, not only in docs."""
    text = HARNESS_PATH.read_text()
    for marker in (
        "IDENTITY-MISMATCH",
        "print the identity of the build it measured",
        "fresh-vs-reused",
    ):
        assert marker.lower() in text.lower(), f"{marker} missing from the harness header"
    assert "3  IDENTITY-MISMATCH" in text


def test_docs_document_the_command_and_its_output(tmp_path: Path) -> None:
    assert DOC_PATH.is_file(), "docs/bunker-qa.md must exist"
    text = DOC_PATH.read_text()
    for marker in (
        "bunker-full-ci.sh run",
        "bunker-full-ci.sh dry-run",
        "bunker-full-ci.sh self-test",
        "IDENTITY-OK",
        "IDENTITY-MISMATCH",
        "fresh",
        "reused",
        "check_deployed_surface.py",
    ):
        assert marker in text, f"docs/bunker-qa.md does not mention {marker!r}"


def test_json_output_is_machine_readable(tmp_path: Path) -> None:
    facts = identity._facts()
    facts_file = tmp_path / "facts.json"
    facts_file.write_text(json.dumps(facts))
    proc = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "render", "--facts", str(facts_file), "--json"],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == identity.EXIT_OK, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["exit_code"] == identity.EXIT_OK
    assert payload["identity_line"].startswith("bunker full-ci: sha=")
    assert payload["mismatches"] == []
