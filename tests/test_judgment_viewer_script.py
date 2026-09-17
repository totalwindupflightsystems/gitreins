"""Tests for the static judgment viewer generator (scripts/judgment_viewer.py).

The script is not a package module, so it is loaded by path. What matters here
is the QA section (JVIEW-007): the static page reads the same QA ledger the live
viewer does, and a missing or unreadable ledger degrades to "no QA runs" instead
of crashing the generator.
"""

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "judgment_viewer.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("judgment_viewer_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


viewer = _load_module()


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """Smallest repository-shaped fixture the generator can read."""
    (tmp_path / ".gitreins" / "history").mkdir(parents=True)
    (tmp_path / ".coding-hermes" / "board").mkdir(parents=True)
    subprocess.run(["git", "-C", str(tmp_path), "init", "-q"], check=True)
    (tmp_path / ".coding-hermes" / "board" / "events.jsonl").write_text("", encoding="utf-8")
    (tmp_path / ".coding-hermes" / "board" / "tasks.jsonl").write_text("", encoding="utf-8")
    return tmp_path


QA_ROW = {
    "ts": "2026-09-17T00:32:16+00:00",
    "project": "gitreins-poc",
    "kind": "dogfood",
    "verdict": "PASS",
    "status": "pass",
    "cells": {"init": "passed", "task": "passed", "guard": "failed"},
    "exit_code": 0,
    "commit": "67eca8fc8ef5d19163f45485bb066f04ec84f98c",
}


def test_load_qa_reports_no_runs_when_the_ledger_is_absent(repo: Path, monkeypatch, tmp_path):
    monkeypatch.setenv("GITREINS_QA_LEDGER", str(tmp_path / "absent.jsonl"))

    ledger, rows = viewer.load_qa(str(repo))

    assert rows == []
    assert ledger == str(tmp_path / "absent.jsonl")


def test_load_qa_returns_ledger_rows_and_their_path(repo: Path, monkeypatch):
    ledger_path = repo / ".gitreins" / "qa-ledger.jsonl"
    ledger_path.write_text(json.dumps(QA_ROW) + "\n", encoding="utf-8")
    monkeypatch.delenv("GITREINS_QA_LEDGER", raising=False)

    ledger, rows = viewer.load_qa(str(repo))

    assert ledger == str(ledger_path)
    assert [row["kind"] for row in rows] == ["dogfood"]
    assert rows[0]["commit"] == QA_ROW["commit"]


def test_load_qa_survives_an_unreadable_ledger(repo: Path, monkeypatch, tmp_path):
    broken = tmp_path / "qa-dir"
    broken.mkdir()
    (broken / "qa-ledger.jsonl").mkdir()  # a directory where a file is expected
    monkeypatch.setenv("GITREINS_QA_LEDGER", str(broken))

    ledger, rows = viewer.load_qa(str(repo))

    assert rows == []
    assert ledger.endswith("qa-ledger.jsonl")


def test_generated_page_carries_the_qa_section(repo: Path, monkeypatch, tmp_path):
    ledger_path = repo / ".gitreins" / "qa-ledger.jsonl"
    ledger_path.write_text(json.dumps(QA_ROW) + "\n", encoding="utf-8")
    monkeypatch.delenv("GITREINS_QA_LEDGER", raising=False)
    monkeypatch.setattr(viewer, "TICKS_DB", str(tmp_path / "no-scheduler.db"))
    out = tmp_path / "page.html"

    monkeypatch.setattr("sys.argv", ["judgment_viewer.py", "--repo", str(repo), "--out", str(out)])
    viewer.main()

    page = out.read_text(encoding="utf-8")
    assert "__QA__" not in page and "__QA_LEDGER__" not in page and "__N_QA__" not in page
    assert "QA Runs" in page
    assert "dogfood" in page
    assert QA_ROW["commit"][:7] in page
    assert str(ledger_path) in page
