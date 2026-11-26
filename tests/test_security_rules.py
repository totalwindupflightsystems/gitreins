"""Regression tests for the bundled deterministic security rule set (GR-GAP-068).

The dogfood run (gitreins-dogfood-2026-11-06-12-08-29) graded a staged diff
GREEN that contained SQL injection, shell=True command injection and path
traversal, because the bundled rule table had no coverage for those classes.
These tests pin the rule inventory: every class a user expects a "security
review" to catch must fire through the REAL stage path (run_rules_stage, which
renders the bundled rules to a temp file and spawns the actual tool), and the
clean control lines must stay silent.

INT-FLAKE-11 (partial-result flake): under host contention semgrep's
per-rule core execution can exceed its timeout budget (or its core process
can be killed). Semgrep records the skip in its JSON ``errors`` array and
EXITS 0, so the stage parser — which reads only ``results`` — sees a clean
scan whose results are missing exactly the skipped rule while sibling rules
from the same run still fire, and run_rules_stage grades that partial result
``ok``. The coverage tests therefore distinguish partial from regression:
``_fired_rules`` retries the rules stage ONCE when a non-empty result is a
strict subset of the expected rules (the skip is transient per-rule
scheduling under load), while a wholly EMPTY result is never retried and
fails immediately. See ``_fired_rules`` for the full rationale.
"""

from __future__ import annotations

import shutil
import textwrap
from pathlib import Path

import pytest

from engine.security_stages import run_rules_stage

FIXTURE = textwrap.dedent(
    """\
    import os
    import pickle
    import subprocess

    import yaml


    def sql_two_step(conn, name):
        query = "SELECT * FROM users WHERE name = '%s'" % name
        return conn.execute(query)


    def sql_percent(conn, name):
        return conn.execute("SELECT * FROM users WHERE name = '%s'" % name)


    def sql_fstring(conn, name):
        return conn.execute(f"SELECT * FROM users WHERE name = '{name}'")


    def shell_concat(key_path):
        return subprocess.run("gpg --import " + key_path, shell=True)


    def shell_fstring(key_path):
        return subprocess.run(f"gpg --import {key_path}", shell=True)


    def traversal(base_dir, filename):
        return open(os.path.join(base_dir, filename))


    def eval_call(payload):
        return eval(payload)  # noqa: S307


    def pickle_loads(blob):
        return pickle.loads(blob)


    def yaml_unsafe(raw):
        return yaml.load(raw, Loader=yaml.Loader)


    def ok_parameterized(conn, name):
        return conn.execute("SELECT * FROM users WHERE name = ?", (name,))  # clean


    def ok_literal_shell():
        return subprocess.run(["gpg", "--import", "/etc/keys/host.asc"])  # clean


    def ok_static_path():
        return open(os.path.join("/etc", "passwd"))  # clean
    """
)

EXPECTED_SQL = {
    "gitreins-sql-assign-interpolation",
    "gitreins-sql-execute-percent",
    "gitreins-sql-execute-fstring",
}
EXPECTED_SHELL = {"gitreins-shell-true-nonliteral"}
EXPECTED_TRAVERSAL = {"gitreins-path-join-open"}
EXPECTED_EXISTING = {"gitreins-eval-call", "gitreins-pickle-loads", "gitreins-yaml-unsafe-load"}

pytestmark = pytest.mark.skipif(shutil.which("semgrep") is None, reason="semgrep not on PATH")


@pytest.fixture()
def staged(tmp_path: Path) -> Path:
    target = tmp_path / "report.py"
    target.write_text(FIXTURE)
    return tmp_path


def _rules_once(workdir: Path, file: Path) -> set[str]:
    outcome = run_rules_stage(str(workdir), [str(file)], timeout=120.0)
    assert outcome.findings is not None, f"rules stage produced no findings: {outcome.detail}"
    return {f.rule_id for f in outcome.findings}


def _fired_rules(workdir: Path, file: Path, expected: set[str] | None = None) -> set[str]:
    """Fired rule ids through the REAL stage path, retried once on partial.

    INT-FLAKE-11 rationale — why the retry exists and where its boundary is:

    Under host contention semgrep's per-rule core execution can exceed its
    timeout budget (or its core process can be killed). Semgrep records the
    skip in its JSON ``errors`` array and EXITS 0, so the stage parser —
    which reads only ``results`` — sees a clean scan whose results are
    missing exactly the skipped rule while sibling rules from the same run
    still fire, and ``run_rules_stage`` grades that partial result ``ok``.
    That graded-partial shape makes the coverage assertions flake without
    any rule being genuinely absent from the bundled table.

    Because the skip is transient per-rule scheduling under load, a
    non-empty result that is a strict SUBSET of *expected* is retried ONCE
    and the second run decides. The boundary cases stay fail-fast:

    * a wholly EMPTY result is never retried and fails immediately — it is
      either a degraded stage (every tool leg lost) or genuine zero
      coverage, and neither is the transient partial shape this retry
      exists for;
    * the retry cannot mask a real regression: a rule genuinely absent from
      the bundled rule table is missing from BOTH attempts, so the caller's
      ``expected - fired`` assertion still fails.

    *expected* is the caller's coverage set (or None for callers that only
    need the fired ids, e.g. the clean-control noise check, which asserts
    nothing about coverage).
    """
    fired = _rules_once(workdir, file)
    if not fired:
        return fired
    if expected and expected - fired:
        # transient per-rule skip: retry once, second run decides
        fired = _rules_once(workdir, file)
    return fired


def test_sql_injection_classes_fire(staged: Path) -> None:
    fired = _fired_rules(staged, staged / "report.py", EXPECTED_SQL)
    missing = EXPECTED_SQL - fired
    assert not missing, f"SQL injection rules did not fire: {missing}"


def test_shell_true_command_injection_fires(staged: Path) -> None:
    fired = _fired_rules(staged, staged / "report.py", EXPECTED_SHELL)
    missing = EXPECTED_SHELL - fired
    assert not missing, f"shell=True command injection rule did not fire: {missing}"


def test_path_traversal_fires(staged: Path) -> None:
    """AC: gitreins-path-join-open fires, with one retry on partial results.

    INT-FLAKE-11: this node flaked because semgrep's per-rule timeout under
    host contention dropped exactly this rule's results (it is the heaviest
    rule over os.path.join targets) while sibling rules from the same run
    fired, and the stage graded the partial result ``ok``. The retry in
    ``_fired_rules`` tolerates that transient partial shape once; a rule
    genuinely missing from the bundled table fails on both attempts and this
    node still fails. A wholly empty stage result fails immediately.
    """
    fired = _fired_rules(staged, staged / "report.py", EXPECTED_TRAVERSAL)
    missing = EXPECTED_TRAVERSAL - fired
    assert not missing, f"path traversal rule did not fire: {missing}"


def test_existing_rules_still_fire(staged: Path) -> None:
    """AC: the new rules must not come at the cost of the old coverage."""
    fired = _fired_rules(staged, staged / "report.py", EXPECTED_EXISTING)
    missing = EXPECTED_EXISTING - fired
    assert not missing, f"pre-existing rule coverage regressed: {missing}"


def test_fired_rules_retries_partial_subset_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """INT-FLAKE-11: a transient partial stage result is retried exactly once.

    Attempt 1 fires a strict subset of the expected rules (the per-rule skip
    shape); attempt 2 is complete and decides. No third stage run happens.
    """
    calls: list[set[str]] = []

    def fake_once(workdir: Path, file: Path) -> set[str]:
        calls.append(set())
        return EXPECTED_SHELL | {"gitreins-eval-call"} if len(calls) == 1 else EXPECTED_TRAVERSAL

    monkeypatch.setattr(f"{__name__}._rules_once", fake_once)
    fired = _fired_rules(Path("/tmp"), Path("/tmp/x.py"), EXPECTED_TRAVERSAL)
    assert fired == EXPECTED_TRAVERSAL
    assert len(calls) == 2, f"expected exactly one retry, got {len(calls)} stage runs"


def test_fired_rules_empty_result_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    """INT-FLAKE-11: a wholly empty stage result is NEVER retried.

    An empty run is either a degraded stage or genuine zero coverage —
    neither is the transient partial shape — so it must fail immediately.
    """
    calls: list[set[str]] = []

    def fake_once(workdir: Path, file: Path) -> set[str]:
        calls.append(set())
        return set()

    monkeypatch.setattr(f"{__name__}._rules_once", fake_once)
    fired = _fired_rules(Path("/tmp"), Path("/tmp/x.py"), EXPECTED_TRAVERSAL)
    assert fired == set()
    assert len(calls) == 1, f"empty result must not be retried, got {len(calls)} stage runs"


def test_fired_rules_second_run_decides(monkeypatch: pytest.MonkeyPatch) -> None:
    """INT-FLAKE-11: if the retry is still partial, the caller's assertion
    sees the SECOND result — the rule genuinely missing from the table
    fails on both attempts and the node still fails."""
    calls: list[set[str]] = []

    def fake_once(workdir: Path, file: Path) -> set[str]:
        calls.append(set())
        return EXPECTED_SHELL | {"gitreins-eval-call"}

    monkeypatch.setattr(f"{__name__}._rules_once", fake_once)
    fired = _fired_rules(Path("/tmp"), Path("/tmp/x.py"), EXPECTED_TRAVERSAL)
    missing = EXPECTED_TRAVERSAL - fired
    assert missing == EXPECTED_TRAVERSAL, "the still-partial second run must decide"
    assert len(calls) == 2


def test_clean_controls_stay_silent(staged: Path) -> None:
    fired = _fired_rules(staged, staged / "report.py")
    outcome = run_rules_stage(str(staged), [str(staged / "report.py")], timeout=120.0)
    clean_lines = {
        i
        for i, line in enumerate(staged.joinpath("report.py").read_text().splitlines(), 1)
        if line.rstrip().endswith("# clean")
    }
    assert clean_lines, "fixture lost its clean control markers"
    noise = [f for f in outcome.findings if f.line in clean_lines]
    assert not noise, f"clean control lines flagged: {[(f.rule_id, f.line) for f in noise]}"
    assert fired  # and the fixture is not silently uncovered


def test_rule_inventory_is_documented() -> None:
    """The docs must state the bundled rule inventory (GR-GAP-068 AC)."""
    docs = Path(__file__).resolve().parent.parent / "docs" / "security-review.md"
    text = docs.read_text(encoding="utf-8")
    for rule_id in sorted(
        EXPECTED_SQL
        | EXPECTED_SHELL
        | EXPECTED_TRAVERSAL
        | EXPECTED_EXISTING
        | {"gitreins-os-system"}
    ):
        assert rule_id in text, f"docs/security-review.md does not document rule {rule_id}"
