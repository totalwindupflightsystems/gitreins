"""Regression tests for the bundled deterministic security rule set (GR-GAP-068).

The dogfood run (gitreins-dogfood-2026-11-06-12-08-29) graded a staged diff
GREEN that contained SQL injection, shell=True command injection and path
traversal, because the bundled rule table had no coverage for those classes.
These tests pin the rule inventory: every class a user expects a "security
review" to catch must fire through the REAL stage path (run_rules_stage, which
renders the bundled rules to a temp file and spawns the actual tool), and the
clean control lines must stay silent.
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


def _fired_rules(workdir: Path, file: Path) -> set[str]:
    outcome = run_rules_stage(str(workdir), [str(file)], timeout=120.0)
    assert outcome.findings is not None, f"rules stage produced no findings: {outcome.detail}"
    return {f.rule_id for f in outcome.findings}


def test_sql_injection_classes_fire(staged: Path) -> None:
    fired = _fired_rules(staged, staged / "report.py")
    missing = EXPECTED_SQL - fired
    assert not missing, f"SQL injection rules did not fire: {missing}"


def test_shell_true_command_injection_fires(staged: Path) -> None:
    fired = _fired_rules(staged, staged / "report.py")
    missing = EXPECTED_SHELL - fired
    assert not missing, f"shell=True command injection rule did not fire: {missing}"


def test_path_traversal_fires(staged: Path) -> None:
    fired = _fired_rules(staged, staged / "report.py")
    missing = EXPECTED_TRAVERSAL - fired
    assert not missing, f"path traversal rule did not fire: {missing}"


def test_existing_rules_still_fire(staged: Path) -> None:
    """AC: the new rules must not come at the cost of the old coverage."""
    fired = _fired_rules(staged, staged / "report.py")
    missing = EXPECTED_EXISTING - fired
    assert not missing, f"pre-existing rule coverage regressed: {missing}"


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
