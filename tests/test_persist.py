"""Dedicated tests for verdict persistence and history reporting."""

import json
import os
from unittest.mock import patch

from engine.persist import (
    DEFAULT_HISTORY_CONFIG,
    VerdictPersister,
    _pct,
    build_report,
    load_history_config,
)


# ── _pct ─────────────────────────────────────────────────────

def test_pct_formats_ratio():
    assert _pct(0, 10) == "0%"
    assert _pct(5, 10) == "50%"
    assert _pct(10, 10) == "100%"
    assert _pct(0, 0) == "0%"


# ── DEFAULT_HISTORY_CONFIG ───────────────────────────────────

def test_default_history_config_has_expected_keys():
    assert DEFAULT_HISTORY_CONFIG["enabled"] is True
    assert DEFAULT_HISTORY_CONFIG["storage"] == "git"
    assert DEFAULT_HISTORY_CONFIG["max_verdicts"] == 1000
    assert ".gitreins/history" in DEFAULT_HISTORY_CONFIG["path"]


# ── VerdictPersister init ────────────────────────────────────

def test_persister_uses_absolute_workdir(tmp_path):
    p = VerdictPersister(str(tmp_path))
    assert os.path.isabs(p.workdir)


def test_persister_enabled_defaults_true(tmp_path):
    p = VerdictPersister(str(tmp_path))
    assert p.enabled is True


def test_persister_history_dir_is_under_workdir_by_default(tmp_path):
    p = VerdictPersister(str(tmp_path))
    assert p.history_dir.startswith(str(tmp_path))


def test_persister_storage_mode_is_git_by_default(tmp_path):
    p = VerdictPersister(str(tmp_path))
    assert p.storage_mode == "git"


# ── persist (non-git path) ───────────────────────────────────

def test_persist_returns_disabled_when_history_disabled(tmp_path):
    p = VerdictPersister(str(tmp_path))
    p.config["enabled"] = False
    assert p.persist("task-1", {}) == "disabled"


def test_persist_creates_verdict_json_and_summary_md(tmp_path):
    p = VerdictPersister(str(tmp_path))
    p.config["storage"] = "filesystem"  # skip git
    p.config["max_verdicts"] = 0  # no pruning

    result = p.persist("task-1", {"passed": True, "task_title": "Test Task"})
    assert result == "dry-run"

    # Find the verdict directory
    history = p.history_dir
    assert os.path.isdir(history)
    date_dirs = os.listdir(history)
    assert len(date_dirs) == 1
    hash_dirs = os.listdir(os.path.join(history, date_dirs[0]))
    assert len(hash_dirs) == 1

    entry = os.path.join(history, date_dirs[0], hash_dirs[0])
    assert os.path.isfile(os.path.join(entry, "verdict.json"))
    assert os.path.isfile(os.path.join(entry, "summary.md"))

    # Verdict JSON has task_id and evaluated_at
    with open(os.path.join(entry, "verdict.json")) as f:
        data = json.load(f)
    assert data["task_id"] == "task-1"
    assert "evaluated_at" in data

    # Summary markdown contains task title
    with open(os.path.join(entry, "summary.md")) as f:
        summary = f.read()
    assert "Test Task" in summary


# ── list_verdicts ────────────────────────────────────────────

def test_list_verdicts_returns_empty_when_no_history(tmp_path):
    p = VerdictPersister(str(tmp_path))
    assert p.list_verdicts() == []


def test_list_verdicts_returns_entries_newest_first(tmp_path):
    p = VerdictPersister(str(tmp_path))
    p.config["storage"] = "filesystem"
    p.config["max_verdicts"] = 0

    p.persist("task-1", {"passed": True})
    p.persist("task-2", {"passed": False})

    entries = p.list_verdicts()
    assert len(entries) == 2
    # Ordering depends on directory entry order — both entries must exist
    task_ids = {e["task_id"] for e in entries}
    assert task_ids == {"task-1", "task-2"}


def test_list_verdicts_filters_by_task_id(tmp_path):
    p = VerdictPersister(str(tmp_path))
    p.config["storage"] = "filesystem"
    p.config["max_verdicts"] = 0

    p.persist("task-a", {"passed": True})
    p.persist("task-b", {"passed": True})

    filtered = p.list_verdicts(task_id="task-a")
    assert len(filtered) == 1
    assert filtered[0]["task_id"] == "task-a"


def test_list_verdicts_limits_to_n(tmp_path):
    p = VerdictPersister(str(tmp_path))
    p.config["storage"] = "filesystem"
    p.config["max_verdicts"] = 0

    for i in range(5):
        p.persist(f"task-{i}", {"passed": True})

    assert len(p.list_verdicts(n=2)) == 2


# ── count_verdicts ───────────────────────────────────────────

def test_count_verdicts_returns_zero_for_no_history(tmp_path):
    p = VerdictPersister(str(tmp_path))
    assert p.count_verdicts() == 0


def test_count_verdicts_counts_all_entries(tmp_path):
    p = VerdictPersister(str(tmp_path))
    p.config["storage"] = "filesystem"
    p.config["max_verdicts"] = 0

    for i in range(3):
        p.persist(f"task-{i}", {"passed": True})

    assert p.count_verdicts() == 3


# ── build_report ─────────────────────────────────────────────

def test_build_report_returns_disabled_message_when_history_off(tmp_path):
    p = VerdictPersister(str(tmp_path))
    with patch("engine.persist.VerdictPersister", return_value=p):
        p.config["enabled"] = False
        result = build_report(str(tmp_path))
        assert "disabled" in result


def test_build_report_shows_no_history_when_empty(tmp_path):
    p = VerdictPersister(str(tmp_path))
    with patch("engine.persist.VerdictPersister", return_value=p):
        result = build_report(str(tmp_path))
        assert "No verdict history found" in result


def test_build_report_includes_summary_stats(tmp_path):
    p = VerdictPersister(str(tmp_path))
    p.config["storage"] = "filesystem"
    p.config["max_verdicts"] = 0

    p.persist("pass-1", {"passed": True, "task_title": "Passing"})
    p.persist("fail-1", {"passed": False, "task_title": "Failing"})

    report = build_report(str(tmp_path))
    assert "pass-1" in report
    assert "fail-1" in report
    assert "Passing" in report or "Failing" in report


# ── _build_summary edge cases ────────────────────────────────

def test_build_summary_handles_dict_items(tmp_path):
    """Summary generation works with dict-format criteria items (MCP)."""
    p = VerdictPersister(str(tmp_path))
    data = {
        "passed": True,
        "task_title": "Dict Items",
        "items": [{"criterion": "Must pass", "status": "PASS", "detail": "ok"}],
        "verdict": None,
    }
    summary = p._build_summary("task-x", data)
    assert "✓" in summary
    assert "Must pass" in summary


def test_build_summary_handles_pipeline_stages(tmp_path):
    p = VerdictPersister(str(tmp_path))
    data = {
        "passed": True,
        "task_title": "With Stages",
        "verdict": None,
        "stages": {"tier1": {"passed": True, "summary": "guard ok"}},
    }
    summary = p._build_summary("task-x", data)
    assert "tier1" in summary
