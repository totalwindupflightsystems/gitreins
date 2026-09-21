"""Hermetic tests for the QA run ledger (QA-GITREINS-POC-7).

QA runs used to leave no record beyond stdout and a gitignored registry, so the
harness record covered task verdicts only. These tests pin the ledger contract:
one row per QA run, fleet-compatible keys, a readable ledger, and a ledger
failure that never fails the run it records.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from engine.qa_ledger import (
    CELL_LIMIT,
    DEFAULT_QA_LEDGER_FILE,
    format_report_section,
    list_rows,
    record_external,
    record_run,
)

REPO_ROOT = Path(__file__).parents[1]
CLI_SCRIPT = REPO_ROOT / "gitreins" / "cli.py"
FLEET_KEYS = ("ts", "project", "status", "cells", "findings", "evidence", "note")


def _git(repo: Path, *args: str, check: bool = True):
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=check
    )


@pytest.fixture
def qa_repo(tmp_path: Path) -> Path:
    """A real git checkout with the board dir disposable runs require."""
    repo = tmp_path / "qa-lab"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "GitReins QA Ledger Tests")
    _git(repo, "config", "user.email", "gitreins-qa@example.invalid")
    (repo / "base.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "base.txt")
    _git(repo, "commit", "-qm", "initial")
    (repo / ".coding-hermes" / "board").mkdir(parents=True)
    (repo / ".gitreins").mkdir(parents=True, exist_ok=True)
    return repo


def _run_cli(repo: Path, *args: str, extra_env: dict[str, str] | None = None):
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}
    env.update(extra_env or {})
    return subprocess.run(
        [os.fspath(os.sys.executable), os.fspath(CLI_SCRIPT), *args],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )


def _read_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _ledger_rows(repo: Path, name: str = DEFAULT_QA_LEDGER_FILE) -> list[dict]:
    """Rows from the repo's default ledger location, or a named sibling."""
    return _read_rows(repo / ".gitreins" / name)


# ── QA runs record themselves ──────────────────────────────────


def test_fresh_run_records_pass_row_with_cells_commit_and_evidence(qa_repo):
    result = _run_cli(qa_repo, "worktree", "fresh", "--cmd", "printf hello")
    assert result.returncode == 0, result.stderr

    rows = _ledger_rows(qa_repo)
    assert len(rows) == 1
    row = rows[0]
    assert row["verdict"] == "PASS"
    assert row["status"] == "pass"
    assert row["kind"] == "fresh"
    assert row["project"] == "qa-lab"
    assert row["exit_code"] == 0
    assert row["cells"] == {"fresh": "passed"}
    assert row["commit"] == _git(qa_repo, "rev-parse", "HEAD").stdout.strip()
    assert row["detail"]["command"] == "printf hello"
    assert row["detail"]["output"] == "hello"
    assert row["harness_version"]


def test_failed_fresh_run_records_fail_verdict_with_the_exit_code(qa_repo):
    result = _run_cli(qa_repo, "worktree", "fresh", "--cmd", "exit 7")
    assert result.returncode == 7

    row = _ledger_rows(qa_repo)[0]
    assert row["verdict"] == "FAIL"
    assert row["status"] == "fail"
    assert row["exit_code"] == 7
    assert row["cells"] == {"fresh": "failed"}


def test_repro_records_a_cell_per_run_and_fails_when_one_run_fails(qa_repo):
    clean = _run_cli(qa_repo, "worktree", "repro", "--cmd", "true", "-k", "2")
    assert clean.returncode == 0, clean.stderr
    row = _ledger_rows(qa_repo)[-1]
    assert row["verdict"] == "PASS"
    assert row["exit_code"] == 0
    assert row["cells"] == {"run-1": "passed", "run-2": "passed"}
    assert row["detail"]["pass_rate"] == 1.0

    mixed = _run_cli(qa_repo, "worktree", "repro", "--cmd", "exit 3", "-k", "2")
    assert mixed.returncode == 1
    row = _ledger_rows(qa_repo)[-1]
    assert row["verdict"] == "FAIL"
    assert row["exit_code"] == 1
    assert row["cells"] == {"run-1": "failed", "run-2": "failed"}


def test_dogfood_records_a_cell_per_step_and_the_judge(qa_repo):
    result = _run_cli(qa_repo, "worktree", "dogfood", "--skip-judge", "--test-command", "true")
    assert result.returncode == 0, result.stderr

    row = _ledger_rows(qa_repo)[0]
    assert row["kind"] == "dogfood"
    assert row["verdict"] == "PASS"
    assert row["cells"]["init"] == "passed"
    assert row["cells"]["guard"] == "passed"
    assert row["cells"]["judge"] == "skipped"


def test_row_is_a_superset_of_the_fleet_ledger_keys(tmp_path):
    row = record_run(str(tmp_path), "fresh", {"exit_code": 0})
    assert row is not None
    for key in FLEET_KEYS:
        assert key in row, key
    assert row["findings"] == []
    assert row["evidence"] == ""


# ── Recording a run produced elsewhere ─────────────────────────


def test_qa_record_appends_a_fleet_row_for_an_external_run(qa_repo):
    result = _run_cli(
        qa_repo,
        "qa",
        "record",
        "--project",
        "gitreins-poc",
        "--kind",
        "bunker",
        "--exit-code",
        "0",
        "--cell",
        "launch=OK",
        "--cell",
        "collect=OK",
        "--finding",
        "QA-GITREINS-POC-42:ledger row missing",
        "--evidence",
        "/tmp/bunker-qa-evidence.jsonl",
        "--note",
        "recorded by the QA lane",
        "--agent",
        "abc123",
        "--server",
        "bunker-las-02",
        "--commit",
        "deadbeef",
        "--ts",
        "2026-09-15T00:20:00+00:00",
    )
    assert result.returncode == 0, result.stderr
    assert "recorded bunker gitreins-poc PASS" in result.stdout

    row = _ledger_rows(qa_repo)[0]
    assert row["project"] == "gitreins-poc"
    assert row["kind"] == "bunker"
    assert row["status"] == "pass"
    assert row["verdict"] == "PASS"
    assert row["cells"] == {"launch": "OK", "collect": "OK"}
    assert row["findings"] == [{"id": "QA-GITREINS-POC-42", "title": "ledger row missing"}]
    assert row["evidence"] == "/tmp/bunker-qa-evidence.jsonl"
    assert row["agent"] == "abc123"
    assert row["server"] == "bunker-las-02"
    assert row["commit"] == "deadbeef"
    assert row["ts"] == "2026-09-15T00:20:00+00:00"


def test_qa_record_derives_status_from_an_explicit_fail_verdict(qa_repo):
    result = _run_cli(
        qa_repo, "qa", "record", "--project", "x", "--verdict", "FAIL", "--cell", "ci=FAIL"
    )
    assert result.returncode == 0, result.stderr
    row = _ledger_rows(qa_repo)[0]
    assert (row["verdict"], row["status"]) == ("FAIL", "fail")


def test_qa_record_defaults_to_pass_when_no_verdict_or_exit_code_is_given(qa_repo):
    # The documented default (docs/cli-reference.md): with neither --verdict,
    # --exit-code nor --status, the row records a passing verdict — a green
    # battery and an undecided one must not be indistinguishable.
    result = _run_cli(qa_repo, "qa", "record", "--project", "x", "--note", "no flags")
    assert result.returncode == 0, result.stderr
    row = _ledger_rows(qa_repo)[0]
    assert (row["verdict"], row["status"]) == ("PASS", "pass")
    assert "recorded lane x PASS" in result.stdout


def test_qa_record_preserves_an_explicit_unknown_verdict(qa_repo):
    result = _run_cli(qa_repo, "qa", "record", "--project", "x", "--verdict", "UNKNOWN")
    assert result.returncode == 0, result.stderr
    row = _ledger_rows(qa_repo)[0]
    assert (row["verdict"], row["status"]) == ("UNKNOWN", "unknown")


def test_qa_record_stamps_evidence_missing_when_the_evidence_path_is_absent(qa_repo, tmp_path):
    missing = tmp_path / "definitely-missing.json"
    result = _run_cli(qa_repo, "qa", "record", "--project", "x", "--evidence", str(missing))
    assert result.returncode == 0, result.stderr
    row = _ledger_rows(qa_repo)[0]
    assert row["evidence"] == str(missing)
    assert row["evidence_missing"] is True
    assert f"qa record: evidence path not found: {missing}" in result.stderr


def test_qa_record_with_an_existing_evidence_file_carries_no_marker(qa_repo, tmp_path):
    evidence = tmp_path / "battery.jsonl"
    evidence.write_text('{"ok": true}\n', encoding="utf-8")
    result = _run_cli(qa_repo, "qa", "record", "--project", "x", "--evidence", str(evidence))
    assert result.returncode == 0, result.stderr
    row = _ledger_rows(qa_repo)[0]
    assert row["evidence"] == str(evidence)
    assert "evidence_missing" not in row
    assert result.stderr == ""


def test_qa_record_rejects_a_malformed_cell(qa_repo):
    result = _run_cli(qa_repo, "qa", "record", "--project", "x", "--cell", "launch")
    assert result.returncode == 2
    assert "--cell expects NAME=STATUS" in result.stderr
    assert _ledger_rows(qa_repo) == []


def test_record_external_reports_nothing_when_the_ledger_is_disabled(qa_repo):
    (qa_repo / ".gitreins").mkdir(exist_ok=True)
    (qa_repo / ".gitreins" / "config.yaml").write_text(
        "qa_ledger:\n  enabled: false\n", encoding="utf-8"
    )
    assert record_external(str(qa_repo), project="x", status="pass") is None
    assert _ledger_rows(qa_repo) == []


# ── Reading the ledger back ────────────────────────────────────


def test_qa_list_names_the_newest_runs_and_the_ledger(qa_repo):
    _run_cli(qa_repo, "worktree", "fresh", "--cmd", "true")
    _run_cli(qa_repo, "worktree", "fresh", "--cmd", "exit 5")
    result = _run_cli(qa_repo, "qa", "list")
    assert result.returncode == 0, result.stderr
    assert "2 QA run(s):" in result.stdout
    assert "✓ fresh" in result.stdout and "✗ fresh" in result.stdout
    assert "cells 1/1 passed" in result.stdout
    assert str(qa_repo / ".gitreins" / DEFAULT_QA_LEDGER_FILE) in result.stdout


def test_qa_list_json_emits_the_rows(qa_repo):
    _run_cli(qa_repo, "worktree", "fresh", "--cmd", "true")
    result = _run_cli(qa_repo, "qa", "list", "--json")
    assert result.returncode == 0, result.stderr
    rows = json.loads(result.stdout)
    assert len(rows) == 1 and rows[0]["kind"] == "fresh"


def test_qa_list_says_so_when_nothing_was_recorded(qa_repo):
    result = _run_cli(qa_repo, "qa", "list")
    assert result.returncode == 0, result.stderr
    assert "No QA runs recorded." in result.stdout


def test_qa_list_limits_to_the_newest_n(qa_repo):
    for _ in range(3):
        _run_cli(qa_repo, "worktree", "fresh", "--cmd", "true")
    result = _run_cli(qa_repo, "qa", "list", "-n", "2")
    assert "Showing newest 2 of 3 QA run(s):" in result.stdout


def test_report_includes_qa_runs_and_stays_quiet_without_rows(qa_repo):
    assert format_report_section(str(qa_repo)) == ""
    _run_cli(qa_repo, "worktree", "fresh", "--cmd", "true")
    result = _run_cli(qa_repo, "report", "-n", "3")
    assert result.returncode == 0, result.stderr
    assert "QA runs (newest 1 of 1):" in result.stdout
    assert "QA ledger:" in result.stdout


def test_malformed_ledger_lines_are_skipped_not_fatal(qa_repo):
    _run_cli(qa_repo, "worktree", "fresh", "--cmd", "true")
    ledger = qa_repo / ".gitreins" / DEFAULT_QA_LEDGER_FILE
    with ledger.open("a", encoding="utf-8") as stream:
        stream.write("not json\n")
    result = _run_cli(qa_repo, "qa", "list")
    assert result.returncode == 0, result.stderr
    assert "1 QA run(s):" in result.stdout
    assert len(list_rows(str(qa_repo))) == 1


# ── Ledger location and retention ──────────────────────────────


def test_env_override_writes_a_file_path(qa_repo, tmp_path):
    target = tmp_path / "fleet-ledger.jsonl"
    _run_cli(
        qa_repo,
        "worktree",
        "fresh",
        "--cmd",
        "true",
        extra_env={"GITREINS_QA_LEDGER": str(target)},
    )
    assert [row["kind"] for row in map(json.loads, target.read_text().splitlines())] == ["fresh"]
    assert _ledger_rows(qa_repo) == []


def test_env_override_writes_into_a_directory(qa_repo, tmp_path):
    target = tmp_path / "fleet"
    target.mkdir()
    _run_cli(
        qa_repo,
        "worktree",
        "fresh",
        "--cmd",
        "true",
        extra_env={"GITREINS_QA_LEDGER": f"{target}{os.sep}"},
    )
    assert (target / DEFAULT_QA_LEDGER_FILE).exists()


def test_config_path_and_max_entries_keep_the_newest_rows(qa_repo):
    (qa_repo / ".gitreins" / "config.yaml").write_text(
        "qa_ledger:\n  path: custom-ledger.jsonl\n  max_entries: 2\n", encoding="utf-8"
    )
    for index in range(3):
        result = _run_cli(qa_repo, "worktree", "fresh", "--cmd", f"printf run-{index}")
        assert result.returncode == 0, result.stderr

    rows = _read_rows(qa_repo / "custom-ledger.jsonl")
    assert [row["detail"]["output"] for row in rows] == ["run-1", "run-2"]


# ── Rotation announces eviction ────────────────────────────────


def test_rotation_past_max_entries_announces_the_eviction_on_stderr(qa_repo, capsys):
    # An append-only audit trail must never shrink silently: recording past
    # max_entries evicts the oldest rows, and that eviction is said on stderr
    # while stdout (which consumers parse) stays untouched.
    (qa_repo / ".gitreins" / "config.yaml").write_text(
        "qa_ledger:\n  max_entries: 3\n", encoding="utf-8"
    )
    for index in range(4):
        stored = record_external(str(qa_repo), project=f"p-{index}", status="pass", kind="lane")
        assert stored is not None

    rows = _ledger_rows(qa_repo)
    assert len(rows) == 3
    # Rotation kept the NEWEST rows, not an arbitrary window.
    assert [row["project"] for row in rows] == ["p-1", "p-2", "p-3"]

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "qa ledger: rotation evicted 1 row(s) (max_entries=3)" in captured.err


def test_recording_under_the_cap_prints_no_eviction_line(qa_repo, capsys):
    for index in range(3):
        stored = record_external(str(qa_repo), project=f"p-{index}", status="pass", kind="lane")
        assert stored is not None

    assert len(_ledger_rows(qa_repo)) == 3
    captured = capsys.readouterr()
    assert "evicted" not in captured.err
    assert "qa ledger" not in captured.err


def test_a_ledger_write_failure_does_not_fail_the_run(qa_repo):
    # A regular file where the ledger's parent directory should be makes the
    # append raise OSError; the QA run must still succeed and say so.
    blocker = qa_repo / "blocker"
    blocker.write_text("not a directory\n", encoding="utf-8")
    result = _run_cli(
        qa_repo,
        "worktree",
        "fresh",
        "--cmd",
        "true",
        extra_env={"GITREINS_QA_LEDGER": str(blocker / DEFAULT_QA_LEDGER_FILE)},
    )
    assert result.returncode == 0, result.stderr
    assert "qa ledger: fresh run not recorded" in result.stderr


def test_disabled_ledger_is_announced_on_stderr(qa_repo):
    (qa_repo / ".gitreins").mkdir(exist_ok=True)
    (qa_repo / ".gitreins" / "config.yaml").write_text(
        "qa_ledger:\n  enabled: false\n", encoding="utf-8"
    )
    result = _run_cli(qa_repo, "worktree", "fresh", "--cmd", "true")
    assert result.returncode == 0, result.stderr
    assert "qa_ledger.enabled is false" in result.stderr
    assert _ledger_rows(qa_repo) == []


# ── Row shape details ──────────────────────────────────────────


def test_cells_are_bounded_and_name_the_remainder(qa_repo):
    report = {
        "command": "true",
        "k": CELL_LIMIT + 6,
        "passes": CELL_LIMIT + 6,
        "failures": 0,
        "pass_rate": 1.0,
        "runs": [
            {"index": index, "exit_code": 0, "duration_s": 0.1}
            for index in range(1, CELL_LIMIT + 7)
        ],
    }
    row = record_run(str(qa_repo), "repro", report)
    assert row is not None
    assert row["cells"]["…"] == "7 more"
    assert len(row["cells"]) == CELL_LIMIT
    assert len(row["detail"]["runs"]) == CELL_LIMIT
    assert row["verdict"] == "PASS"


def test_project_defaults_to_the_repository_directory_name(qa_repo):
    row = record_run(str(qa_repo), "fresh", {"exit_code": 0})
    assert row is not None
    assert row["project"] == "qa-lab"


def test_a_timeout_is_recorded_as_a_failure(qa_repo):
    row = record_run(str(qa_repo), "fresh", {"exit_code": -1, "output": "command timed out"})
    assert row is not None
    assert row["verdict"] == "FAIL"
    assert row["exit_code"] == -1
