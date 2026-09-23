"""HTTP-layer tests for the live judgment viewer server."""

import http.client
import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

from gitreins import serve

REPO_ROOT = Path(__file__).resolve().parents[1]


EVIDENCE_BRIEF_TEXT = "Deliver the viewer evidence section.\nSecond line of the brief.\n"
EVIDENCE_PATCH_TEXT = "diff --git a/gitreins/serve.py b/gitreins/serve.py\n+evidence\n"

# Judge telemetry fixture: two usage lines before the first verdict and one
# before the second, all inside the window each verdict owns (JVIEW-006).
USAGE_ROWS = [
    {
        "ts": 1788263940.0,
        "tokens_in": 41250,
        "tokens_out": 1863,
        "cache_read": 0,
        "cache_write": 0,
        "step": "ai_eval",
    },
    {
        "ts": 1788263945.0,
        "tokens_in": 50000,
        "tokens_out": 2000,
        "cache_read": 128,
        "cache_write": 0,
        "step": "tier1",
    },
    {
        "ts": 1788350340.0,
        "tokens_in": 9542,
        "tokens_out": 794,
        "cache_read": 0,
        "cache_write": 0,
        "step": "ai_eval",
    },
]
PRICE_CONFIG = "defaults:\n  model: deepseek-v4-flash\nusage:\n  price_per_1m_input: 0.28\n  price_per_1m_output: 0.42\n"


@pytest.fixture()
def repo_fixture(tmp_path: Path) -> dict:
    """Create the smallest repository-shaped judgment viewer fixture."""
    history = tmp_path / ".gitreins" / "history"
    board = tmp_path / ".coding-hermes" / "board"
    history.mkdir(parents=True)
    board.mkdir(parents=True)
    subprocess.run(["git", "-C", str(tmp_path), "init", "-q"], check=True)

    pass_verdict = {
        "task_id": "JVIEW-PASS",
        "task_title": "Passing viewer fixture",
        "passed": True,
        "items": [],
        "stages": {
            "tier1": {"id": "tier1", "passed": True, "summary": "all static gates passed"},
            "tier2": {
                "id": "tier2",
                "passed": True,
                "summary": "fixture judgment passed",
                "items": [
                    {
                        "criterion": "The viewer serves the fixture",
                        "status": "PASS",
                        "detail": "The HTTP layer returned the full record.",
                    }
                ],
            },
        },
        "evaluated_at": "2026-09-01T12:00:00Z",
        "worktree": "/tmp/task-worktree",
        "branch": "gitreins/task/JVIEW-PASS",
        "evidence": {
            "collected_at": "2026-09-01T12:00:01Z",
            "task_id": "JVIEW-PASS",
            "items": [
                {
                    "name": "brief",
                    "label": "Worker brief",
                    "file": "worker-brief.md",
                    "bytes": len(EVIDENCE_BRIEF_TEXT.encode("utf-8")),
                    "truncated": False,
                    "source": "GITREINS_WORKER_BRIEF: /tmp/brief.md",
                },
                {
                    "name": "patch",
                    "label": "Graded patch",
                    "file": "commit.patch",
                    "bytes": len(EVIDENCE_PATCH_TEXT.encode("utf-8")),
                    "truncated": False,
                    "source": "git diff HEAD (working tree)",
                },
            ],
        },
    }
    fail_verdict = {
        "task_id": "JVIEW-FAIL",
        "task_title": "Failing viewer fixture",
        "passed": False,
        "items": [],
        "stages": {
            "tier1": {"id": "tier1", "passed": False, "summary": "lint failed"},
            "tier2": {
                "id": "tier2",
                "passed": False,
                "summary": "fixture judgment failed",
                "items": [
                    {
                        "criterion": "The fixture must pass",
                        "status": "FAIL",
                        "detail": "This verdict intentionally fails.",
                    }
                ],
            },
        },
        "evaluated_at": "2026-09-02T12:00:00Z",
    }
    verdicts = [
        ("2026-09-01", "a1b2c3d4", pass_verdict),
        ("2026-09-02", "e5f6a7b8", fail_verdict),
    ]
    for date, verdict_hash, verdict in verdicts:
        verdict_dir = history / date / verdict_hash
        verdict_dir.mkdir(parents=True)
        (verdict_dir / "verdict.json").write_text(json.dumps(verdict), encoding="utf-8")

    # Worker evidence artifacts, as `task complete` writes them (JVIEW-005).
    pass_dir = history / "2026-09-01" / "a1b2c3d4"
    (pass_dir / "worker-brief.md").write_text(EVIDENCE_BRIEF_TEXT, encoding="utf-8")
    (pass_dir / "commit.patch").write_text(EVIDENCE_PATCH_TEXT, encoding="utf-8")

    # Judge telemetry, as the evaluator writes it (JVIEW-006). No config.yaml
    # here on purpose: the default state of a checkout is UNPRICED, and the
    # tests that want a costed verdict write the usage block themselves.
    (tmp_path / ".gitreins" / "usage.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in USAGE_ROWS), encoding="utf-8"
    )

    events = [
        {"timestamp": "2026-09-02T12:01:00Z", "event_type": "verdict", "task_id": "JVIEW-FAIL"},
        {"timestamp": "2026-09-01T12:01:00Z", "event_type": "verdict", "task_id": "JVIEW-PASS"},
    ]
    tasks = [
        {"id": "JVIEW-PASS", "status": "done"},
        {"id": "JVIEW-FAIL", "status": "failed"},
    ]
    (board / "events.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in events), encoding="utf-8"
    )
    (board / "tasks.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in tasks), encoding="utf-8"
    )
    (tmp_path / "etc").mkdir()
    (tmp_path / "etc" / "passwd").write_text("fixture traversal sentinel", encoding="utf-8")

    return {
        "root": tmp_path,
        "verdicts": verdicts,
        "events": events,
        "tasks": tasks,
    }


@contextmanager
def running_server(workdir: str, project: str = ""):
    """Run the production request handler against one workdir only."""

    class FixtureHandler(serve.Handler):
        pass

    FixtureHandler.workdir = workdir
    FixtureHandler.project = project
    server = serve.ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield ("127.0.0.1", server.server_port)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive(), "judgment viewer server thread did not stop"


@pytest.fixture()
def live_server(repo_fixture: dict):
    """Run the production request handler against only the fixture repository."""
    with running_server(str(repo_fixture["root"])) as address:
        yield address


def get(live_server: tuple[str, int], path: str) -> tuple[int, bytes]:
    """Make one standard-library HTTP request and return status and body."""
    connection = http.client.HTTPConnection(*live_server, timeout=5)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


def json_body(body: bytes) -> dict:
    return json.loads(body.decode("utf-8"))


def test_root_serves_judgment_browser_html(live_server):
    status, body = get(live_server, "/")

    assert status == 200
    assert "Judgment Browser" in body.decode("utf-8")


def test_stats_counts_fixture_verdicts(live_server):
    status, body = get(live_server, "/api/stats")

    assert status == 200
    payload = json_body(body)
    assert {key: payload[key] for key in ("total", "passed", "failed", "pass_rate")} == {
        "total": 2,
        "passed": 1,
        "failed": 1,
        "pass_rate": 50,
    }


def test_verdicts_lists_fixture_metadata(live_server, repo_fixture):
    status, body = get(live_server, "/api/verdicts")

    assert status == 200
    rows = json_body(body)["verdicts"]
    assert len(rows) == 2
    by_hash = {row["hash"]: row for row in rows}
    assert by_hash == {
        "a1b2c3d4": {
            "date": "2026-09-01",
            "hash": "a1b2c3d4",
            "task_id": "JVIEW-PASS",
            "title": "Passing viewer fixture",
            "passed": True,
            "n_criteria": 1,
            "tier1_passed": True,
            "worktree": "/tmp/task-worktree",
            "branch": "gitreins/task/JVIEW-PASS",
        },
        "e5f6a7b8": {
            "date": "2026-09-02",
            "hash": "e5f6a7b8",
            "task_id": "JVIEW-FAIL",
            "title": "Failing viewer fixture",
            "passed": False,
            "n_criteria": 1,
            "tier1_passed": False,
        },
    }
    assert len(repo_fixture["verdicts"]) == 2


def test_verdict_detail_returns_full_record(live_server, repo_fixture):
    date, verdict_hash, expected = repo_fixture["verdicts"][0]

    status, body = get(live_server, f"/api/verdicts/{date}/{verdict_hash}")

    assert status == 200
    # The stored record is served verbatim; the only added block is the
    # joined judge telemetry (asserted on its own below).
    served = json_body(body)
    assert {key: value for key, value in served.items() if key != "usage"} == expected
    assert served["stages"]["tier1"]["passed"] is True
    assert served["stages"]["tier2"]["items"][0]["status"] == "PASS"


def test_verdict_detail_returns_worktree_metadata_and_viewer_renders_it(live_server, repo_fixture):
    """The detail API preserves origin metadata and the SPA displays it."""
    date, verdict_hash, _expected = repo_fixture["verdicts"][0]
    status, body = get(live_server, f"/api/verdicts/{date}/{verdict_hash}")
    assert status == 200
    payload = json_body(body)
    assert payload["worktree"] == "/tmp/task-worktree"
    assert payload["branch"] == "gitreins/task/JVIEW-PASS"

    html_status, html_body = get(live_server, "/")
    html = html_body.decode("utf-8")
    assert html_status == 200
    assert "v.worktree" in html
    assert "v.branch" in html
    assert "worktree: " in html
    assert "branch: " in html


def test_legacy_verdict_detail_without_metadata_still_renders(live_server, repo_fixture):
    """Old verdict JSON without origin fields remains a valid detail record."""
    date, verdict_hash, expected = repo_fixture["verdicts"][1]
    status, body = get(live_server, f"/api/verdicts/{date}/{verdict_hash}")
    assert status == 200
    served = json_body(body)
    assert {key: value for key, value in served.items() if key != "usage"} == expected
    assert "worktree" not in served
    assert "branch" not in served


def test_verdict_detail_rejects_unknown_hash_and_malformed_date(live_server):
    unknown_status, unknown_body = get(live_server, "/api/verdicts/2026-09-01/deadbeef")
    malformed_status, malformed_body = get(live_server, "/api/verdicts/not-a-date/a1b2c3d4")

    assert unknown_status == 404
    assert json_body(unknown_body) == {"error": "not found"}
    assert malformed_status in (400, 404)
    assert json_body(malformed_body)["error"]


@pytest.mark.parametrize(
    "path",
    [
        "/api/verdicts/../../etc/passwd",
        "/api/verdicts/%2e%2e/%2e%2e/etc/passwd",
    ],
)
def test_verdict_detail_rejects_direct_and_encoded_traversal(live_server, path):
    status, body = get(live_server, path)

    assert status in (400, 404)
    assert "fixture traversal sentinel" not in body.decode("utf-8")
    assert json_body(body)["error"]


def test_board_routes_return_fixture_rows(live_server, repo_fixture):
    events_status, events_body = get(live_server, "/api/events")
    tasks_status, tasks_body = get(live_server, "/api/tasks")

    assert events_status == 200
    assert tasks_status == 200
    assert json_body(events_body) == {"events": repo_fixture["events"]}
    assert json_body(tasks_body) == {"tasks": repo_fixture["tasks"]}


# ── --repo: one install browses any checkout (JVIEW-004) ─────────────────────


def test_resolve_workdir_defaults_to_the_invoked_repository():
    assert serve.resolve_workdir(None, "/tmp/invoked") == "/tmp/invoked"
    assert serve.resolve_workdir("", "/tmp/invoked") == "/tmp/invoked"


def test_resolve_workdir_accepts_an_explicit_checkout(repo_fixture):
    resolved = serve.resolve_workdir(str(repo_fixture["root"]))

    assert Path(resolved) == Path(repo_fixture["root"])


def test_resolve_workdir_normalizes_a_subdirectory_to_the_repo_root(repo_fixture):
    """A path inside the checkout must browse the checkout, not the subdirectory."""
    nested = Path(repo_fixture["root"]) / ".gitreins" / "history"

    resolved = serve.resolve_workdir(str(nested))

    assert Path(resolved) == Path(repo_fixture["root"])


def test_resolve_workdir_accepts_a_directory_outside_git(tmp_path):
    """History-only directories (no Git) stay browsable as given."""
    plain = tmp_path / "history-only"
    verdict_dir = plain / ".gitreins" / "history" / "2026-09-03" / "beefcafe"
    verdict_dir.mkdir(parents=True)
    (verdict_dir / "verdict.json").write_text(
        json.dumps({"task_id": "PLAIN", "task_title": "no git here", "passed": True, "stages": {}}),
        encoding="utf-8",
    )

    resolved = serve.resolve_workdir(str(plain))

    assert Path(resolved) == plain
    assert [row["task_id"] for row in serve.list_verdicts(resolved)] == ["PLAIN"]


def test_resolve_workdir_rejects_paths_that_are_not_directories(tmp_path):
    a_file = tmp_path / "not-a-directory.txt"
    a_file.write_text("plain file", encoding="utf-8")

    with pytest.raises(serve.ServeArgumentError, match="not a directory"):
        serve.resolve_workdir(str(tmp_path / "absent"))
    with pytest.raises(serve.ServeArgumentError, match="not a directory"):
        serve.resolve_workdir(str(a_file))


def test_board_routes_are_empty_when_the_browsed_checkout_has_no_board(tmp_path):
    """A checkout without .coding-hermes/board answers with empty lists, not a 500."""
    verdict_dir = tmp_path / ".gitreins" / "history" / "2026-09-04" / "c0ffee12"
    verdict_dir.mkdir(parents=True)
    (verdict_dir / "verdict.json").write_text(
        json.dumps({"task_id": "NOBOARD", "passed": True, "stages": {}}), encoding="utf-8"
    )
    subprocess.run(["git", "-C", str(tmp_path), "init", "-q"], check=True)

    with running_server(str(tmp_path)) as address:
        for route, key in (("/api/tasks", "tasks"), ("/api/events", "events")):
            status, body = get(address, route)
            assert status == 200
            assert json_body(body) == {key: []}
        stats_status, stats_body = get(address, "/api/stats")
        assert stats_status == 200
        assert json_body(stats_body)["total"] == 1


def test_ticks_route_reads_the_selected_project_ledger(repo_fixture, tmp_path, monkeypatch):
    """The tick panel is opt-in per project and reads that project's rows only."""
    db_path = tmp_path / "scheduler.db"
    connection = sqlite3.connect(db_path)
    connection.execute(
        "CREATE TABLE ticks (id INTEGER PRIMARY KEY, project_name TEXT, spawned_at TEXT,"
        " status TEXT, outcome TEXT, commits INTEGER, files_changed INTEGER,"
        " cost_usd REAL, error TEXT)"
    )
    connection.execute(
        "INSERT INTO ticks (id, project_name, spawned_at, status, outcome, commits,"
        " files_changed, cost_usd, error) VALUES"
        " (1, 'gitreins-poc', '2026-09-16T18:00:00Z', 'completed', 'committed', 2, 5, 0.0, NULL),"
        " (2, 'someone-else', '2026-09-16T19:00:00Z', 'completed', 'committed', 9, 9, 0.0, NULL)"
    )
    connection.commit()
    connection.close()
    monkeypatch.setattr(serve, "TICKS_DB", str(db_path))

    with running_server(str(repo_fixture["root"])) as address:
        unset_status, unset_body = get(address, "/api/ticks")
    with running_server(str(repo_fixture["root"]), project="gitreins-poc") as address:
        ticks_status, ticks_body = get(address, "/api/ticks")

    assert unset_status == 200
    assert json_body(unset_body)["ticks"] == []
    assert ticks_status == 200
    payload = json_body(ticks_body)
    assert payload["project"] == "gitreins-poc"
    assert [row["id"] for row in payload["ticks"]] == [1]


# ── /api/qa: the QA run ledger in the viewer (JVIEW-007) ─────────────────────


def _pin_qa_ledger(monkeypatch, path) -> None:
    """Hermetic ledger location: clear any host value, then pin the fixture's."""
    monkeypatch.delenv("GITREINS_QA_LEDGER", raising=False)
    monkeypatch.setenv("GITREINS_QA_LEDGER", str(path))


def test_qa_route_is_empty_when_the_ledger_is_absent(repo_fixture, tmp_path, monkeypatch):
    """A missing ledger answers 200 with an empty run list, never a 500."""
    ledger = tmp_path / "absent-qa-ledger.jsonl"
    _pin_qa_ledger(monkeypatch, ledger)

    with running_server(str(repo_fixture["root"])) as address:
        status, body = get(address, "/api/qa")

    assert status == 200
    payload = json_body(body)
    assert payload["runs"] == []
    assert payload["ledger"] == str(ledger)


def test_qa_route_serves_ledger_rows_in_file_order(repo_fixture, tmp_path, monkeypatch):
    """Ledger rows round-trip verbatim, oldest first, with the resolved path."""
    ledger = tmp_path / "qa-ledger.jsonl"
    rows = [
        {
            "ts": "2026-09-16T10:00:00+00:00",
            "project": "gitreins-poc",
            "kind": "fresh",
            "verdict": "PASS",
            "status": "ok",
            "cells": {"tier1": "passed", "tests": "passed", "lint": "passed"},
            "exit_code": 0,
            "commit": "abc1234def5678",
            "run_id": "qa-fresh-001",
        },
        {
            "ts": "2026-09-16T11:00:00+00:00",
            "project": "gitreins-poc",
            "kind": "repro",
            "verdict": "FAIL",
            "status": "failed",
            "cells": {"run-1": "failed", "run-2": "passed"},
            "exit_code": 1,
            "run_id": "qa-repro-002",
        },
    ]
    ledger.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    _pin_qa_ledger(monkeypatch, ledger)

    with running_server(str(repo_fixture["root"])) as address:
        status, body = get(address, "/api/qa")

    assert status == 200
    payload = json_body(body)
    assert payload["ledger"] == str(ledger)
    assert payload["runs"] == rows
    assert payload["runs"][0]["verdict"] == "PASS"
    assert payload["runs"][0]["commit"] == "abc1234def5678"
    assert payload["runs"][0]["exit_code"] == 0
    assert payload["runs"][1]["verdict"] == "FAIL"


def test_qa_route_survives_an_unreadable_ledger(repo_fixture, tmp_path, monkeypatch):
    """A directory where the ledger file should be is an empty run list, still 200."""
    ledger_dir = tmp_path / "qa-ledger-dir"
    ledger_dir.mkdir()
    # qa_ledger_path resolves a directory override to <dir>/qa-ledger.jsonl;
    # making THAT a directory is what makes open() raise IsADirectoryError.
    (ledger_dir / "qa-ledger.jsonl").mkdir()
    _pin_qa_ledger(monkeypatch, ledger_dir)

    with running_server(str(repo_fixture["root"])) as address:
        status, body = get(address, "/api/qa")

    assert status == 200
    payload = json_body(body)
    assert payload["runs"] == []
    assert payload["ledger"] == str(ledger_dir / "qa-ledger.jsonl")


def test_viewer_page_advertises_the_qa_panel(repo_fixture, tmp_path, monkeypatch):
    """The SPA carries the QA Runs panel shell and fetches the ledger route."""
    _pin_qa_ledger(monkeypatch, tmp_path / "unused-qa-ledger.jsonl")

    with running_server(str(repo_fixture["root"])) as address:
        status, body = get(address, "/")

    assert status == 200
    html = body.decode("utf-8")
    assert "qalist" in html
    assert "/api/qa" in html


# ── worker evidence: brief + driver log + graded patch (JVIEW-005) ───────────


def test_evidence_route_serves_a_manifest_declared_artifact(live_server, repo_fixture):
    """The brief the fixture's manifest declares round-trips as text/plain."""
    date, verdict_hash, _ = repo_fixture["verdicts"][0]

    status, body = get(live_server, f"/api/verdicts/{date}/{verdict_hash}/evidence/brief")

    assert status == 200
    assert body.decode("utf-8") == EVIDENCE_BRIEF_TEXT


def test_evidence_route_serves_the_graded_patch(live_server, repo_fixture):
    date, verdict_hash, _ = repo_fixture["verdicts"][0]

    status, body = get(live_server, f"/api/verdicts/{date}/{verdict_hash}/evidence/patch")

    assert status == 200
    assert body.decode("utf-8") == EVIDENCE_PATCH_TEXT


def test_evidence_route_404s_for_undeclared_and_traversing_names(live_server, repo_fixture):
    """Only artifacts the verdict's own manifest declares are reachable."""
    date, verdict_hash, _ = repo_fixture["verdicts"][0]

    undeclared_status, undeclared_body = get(
        live_server, f"/api/verdicts/{date}/{verdict_hash}/evidence/log"
    )
    traversal_status, traversal_body = get(
        live_server, f"/api/verdicts/{date}/{verdict_hash}/evidence/..%2f..%2fetc%2fpasswd"
    )
    bare_status, bare_body = get(live_server, f"/api/verdicts/{date}/{verdict_hash}/evidence")

    assert undeclared_status == 404
    assert json_body(undeclared_body) == {"error": "not found"}
    assert traversal_status == 404
    assert "sentinel" not in traversal_body.decode("utf-8", errors="replace")
    assert bare_status == 400
    assert json_body(bare_body)["error"]


def test_evidence_route_404s_when_the_artifact_file_is_gone(live_server, repo_fixture):
    """A deleted artifact is a 404, never a 500."""
    date, verdict_hash, _ = repo_fixture["verdicts"][0]
    patch = repo_fixture["root"] / ".gitreins" / "history" / date / verdict_hash / "commit.patch"
    stored = patch.read_text(encoding="utf-8")
    patch.unlink()
    try:
        status, body = get(live_server, f"/api/verdicts/{date}/{verdict_hash}/evidence/patch")
    finally:
        patch.write_text(stored, encoding="utf-8")

    assert status == 404
    assert json_body(body) == {"error": "not found"}


def test_legacy_verdict_without_evidence_has_no_items_and_no_artifacts(live_server, repo_fixture):
    """A verdict recorded before evidence embedding stays readable and 404s."""
    date, verdict_hash, _ = repo_fixture["verdicts"][1]

    detail_status, detail_body = get(live_server, f"/api/verdicts/{date}/{verdict_hash}")
    artifact_status, _artifact_body = get(
        live_server, f"/api/verdicts/{date}/{verdict_hash}/evidence/brief"
    )

    assert detail_status == 200
    assert "evidence" not in json_body(detail_body)
    assert artifact_status == 404


def test_verdict_detail_exposes_the_evidence_manifest(live_server, repo_fixture):
    date, verdict_hash, _ = repo_fixture["verdicts"][0]

    status, body = get(live_server, f"/api/verdicts/{date}/{verdict_hash}")

    assert status == 200
    items = json_body(body)["evidence"]["items"]
    assert [item["name"] for item in items] == ["brief", "patch"]
    assert items[0]["source"].startswith("GITREINS_WORKER_BRIEF")


def test_viewer_page_renders_the_evidence_section(live_server):
    """The SPA carries the Evidence section and fetches the artifact route."""
    status, body = get(live_server, "/")

    assert status == 200
    html = body.decode("utf-8")
    assert "evidenceSection" in html
    assert "no worker evidence recorded for this verdict" in html
    assert "/evidence/" in html


# ── judge telemetry: tokens/cost per judgment (JVIEW-006) ────────────────────


def test_verdict_detail_includes_judge_telemetry_when_traceable(live_server, repo_fixture):
    """Usage lines before a verdict's evaluated_at are charged to that verdict."""
    date, verdict_hash, _ = repo_fixture["verdicts"][0]

    status, body = get(live_server, f"/api/verdicts/{date}/{verdict_hash}")

    assert status == 200
    usage = json_body(body)["usage"]
    assert usage["tokens_in"] == 41250 + 50000
    assert usage["tokens_out"] == 1863 + 2000
    assert usage["cache_read"] == 128
    assert usage["rows"] == 2
    assert usage["steps"] == ["ai_eval", "tier1"]
    # No rates configured in this fixture => tokens are reported, cost is not.
    assert usage["priced"] is False
    assert usage["cost_usd"] is None


def test_verdict_detail_prices_the_judgment_when_rates_are_configured(live_server, repo_fixture):
    date, verdict_hash, _ = repo_fixture["verdicts"][0]
    (repo_fixture["root"] / ".gitreins" / "config.yaml").write_text(PRICE_CONFIG, encoding="utf-8")

    status, body = get(live_server, f"/api/verdicts/{date}/{verdict_hash}")

    assert status == 200
    usage = json_body(body)["usage"]
    expected = round(((41250 + 50000) * 0.28 + (1863 + 2000) * 0.42) / 1_000_000, 6)
    assert usage["priced"] is True
    assert usage["cost_usd"] == expected
    assert usage["model"] == "deepseek-v4-flash"


def test_verdict_without_traceable_rows_has_no_usage_block(live_server, repo_fixture):
    """A verdict that predates every usage line reports no telemetry, not zeroes."""
    late = repo_fixture["root"] / ".gitreins" / "history" / "2026-09-03" / "ffff0000"
    late.mkdir(parents=True)
    (late / "verdict.json").write_text(
        json.dumps(
            {
                "task_id": "JVIEW-LATE",
                "task_title": "late",
                "passed": True,
                "evaluated_at": "2026-09-04T12:00:00Z",
            }
        ),
        encoding="utf-8",
    )

    status, body = get(live_server, "/api/verdicts/2026-09-03/ffff0000")

    assert status == 200
    assert "usage" not in json_body(body)


def test_stats_include_the_aggregate_judge_spend(live_server, repo_fixture):
    status, body = get(live_server, "/api/stats")

    assert status == 200
    usage = json_body(body)["usage"]
    assert usage["judgements"] == 2
    assert usage["verdicts"] == 2
    assert usage["unattributed"] == 0
    assert usage["tokens_in"] == 41250 + 50000 + 9542
    assert usage["tokens_out"] == 1863 + 2000 + 794
    assert usage["unpriced"] == 2
    assert usage["priced"] == 0
    assert usage["cost_usd"] == 0.0
    assert usage["prices_configured"] is False
    assert usage["model"] == ""


def test_stats_report_a_priced_subtotal_next_to_the_unpriced_count(live_server, repo_fixture):
    (repo_fixture["root"] / ".gitreins" / "config.yaml").write_text(PRICE_CONFIG, encoding="utf-8")

    status, body = get(live_server, "/api/stats")

    assert status == 200
    usage = json_body(body)["usage"]
    expected = round(
        ((41250 + 50000) * 0.28 + (1863 + 2000) * 0.42) / 1_000_000,
        6,
    ) + round((9542 * 0.28 + 794 * 0.42) / 1_000_000, 6)
    assert usage["priced"] == 2
    assert usage["unpriced"] == 0
    assert usage["cost_usd"] == round(expected, 6)
    assert usage["prices_configured"] is True


def test_viewer_page_renders_the_cost_badge_and_the_spend_card(live_server):
    """The SPA carries the detail cost badge and the aggregate spend card."""
    status, body = get(live_server, "/")

    assert status == 200
    html = body.decode("utf-8")
    assert "costBadge" in html
    assert "spendCard" in html
    assert "Judge spend" in html
    assert "Judge telemetry" in html


def _cli_env() -> dict:
    """Child environment that imports the working tree (as CI does)."""
    env = dict(os.environ)
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = os.pathsep.join([str(REPO_ROOT)] + ([existing] if existing else []))
    return env


def _wait_for_banner(
    log_path: Path, process: subprocess.Popen, timeout: float = 30.0
) -> tuple[int, str]:
    """Wait for `serve` to announce its URL and return (port, whole log)."""
    deadline = time.time() + timeout
    log = ""
    while time.time() < deadline:
        log = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
        match = re.search(r"http://127\.0\.0\.1:(\d+)/", log)
        if match:
            return int(match.group(1)), log
        if process.poll() is not None:
            break
        time.sleep(0.2)
    raise AssertionError(f"serve never announced its URL (exit={process.poll()}): {log!r}")


def test_cli_serve_repo_flag_browses_another_checkout(repo_fixture, tmp_path):
    """`gitreins serve --repo <path>` serves that checkout from an unrelated cwd."""
    unrelated = tmp_path / "unrelated-cwd"
    unrelated.mkdir()
    log_path = tmp_path / "serve.log"
    with log_path.open("w", encoding="utf-8") as log:
        process = subprocess.Popen(
            [
                sys.executable,
                "-u",
                "-m",
                "gitreins",
                "serve",
                "--repo",
                str(repo_fixture["root"]),
                "--port",
                "0",
            ],
            cwd=str(unrelated),
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
            env=_cli_env(),
        )
    try:
        port, log = _wait_for_banner(log_path, process)
        assert "Ctrl-C to stop" in log

        status, body = get(("127.0.0.1", port), "/api/stats")
        assert status == 200
        payload = json_body(body)
        assert payload["total"] == 2
        assert payload["passed"] == 1
        assert payload["failed"] == 1
        assert Path(payload["path"]).samefile(repo_fixture["root"])
        assert payload["repo"] == Path(repo_fixture["root"]).name

        tasks_status, tasks_body = get(("127.0.0.1", port), "/api/tasks")
        assert tasks_status == 200
        assert json_body(tasks_body) == {"tasks": repo_fixture["tasks"]}
    finally:
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def test_cli_serve_rejects_a_repo_path_that_is_not_a_directory(tmp_path):
    result = subprocess.run(
        [sys.executable, "-m", "gitreins", "serve", "--repo", str(tmp_path / "absent")],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        timeout=60,
        env=_cli_env(),
    )

    assert result.returncode == 2
    assert "--repo is not a directory" in result.stderr
    assert "Judgment browser" not in result.stdout


# ── Resolution-gate records in the viewer API (DF-GITREINS-POC-36) ──


def _write_resolution_record(root: Path, date: str = "2026-09-03", h: str = "aa11bb22") -> dict:
    """The record ``engine.persist.persist_resolution`` files, shape-faithful."""
    record = {
        "kind": "resolution",
        "source": "cli",
        "band": "RESOLVED",
        "probability": 0.91,
        "missing_kind": None,
        "question": "Does the gate persist its verdicts?",
        "task_title": "Does the gate persist its verdicts?",
        "task_id": "resolution",
        "evaluated_at": f"{date}T12:00:00Z",
        "verdict": {
            "question": "Does the gate persist its verdicts?",
            "verdict": "RESOLVED",
            "probability": 0.91,
            "manifest": [{"file": "engine/persist.py", "provenance": "ast_exact", "bytes": 120}],
        },
    }
    entry = root / ".gitreins" / "history" / date / h
    entry.mkdir(parents=True)
    (entry / "verdict.json").write_text(json.dumps(record), encoding="utf-8")
    return record


@pytest.fixture()
def resolution_repo(tmp_path: Path) -> Path:
    """A checkout holding one passing judge verdict AND one resolution record."""
    judge = {
        "task_id": "JVIEW-PASS",
        "task_title": "Passing viewer fixture",
        "passed": True,
        "items": [],
        "evaluated_at": "2026-09-01T12:00:00Z",
    }
    entry = tmp_path / ".gitreins" / "history" / "2026-09-01" / "a1b2c3d4"
    entry.mkdir(parents=True)
    (entry / "verdict.json").write_text(json.dumps(judge), encoding="utf-8")
    _write_resolution_record(tmp_path)
    return tmp_path


class TestResolutionRecordsInTheViewerAPI:
    """`gitreins serve` lists the gate's decisions without lying about them."""

    def test_the_row_list_hands_back_the_kind_marker(self, resolution_repo: Path):
        rows = serve.list_verdicts(str(resolution_repo))
        by_task = {row["task_id"]: row for row in rows}

        assert set(by_task) == {"JVIEW-PASS", "resolution"}
        assert "kind" not in by_task["JVIEW-PASS"], "a judge row carries no kind key"
        assert by_task["resolution"]["kind"] == "resolution"
        assert by_task["resolution"]["band"] == "RESOLVED"
        assert by_task["resolution"]["title"] == "Does the gate persist its verdicts?"

    def test_stats_count_judgments_only(self, resolution_repo: Path):
        stats = serve.stats(serve.list_verdicts(str(resolution_repo)))

        assert stats == {"total": 1, "passed": 1, "failed": 0, "pass_rate": 100}

    def test_http_api_lists_the_record_and_keeps_the_pass_rate_honest(self, resolution_repo: Path):
        with running_server(str(resolution_repo)) as (host, port):
            status, body = get((host, port), "/api/verdicts")
            assert status == 200
            rows = json_body(body)["verdicts"]

            stats_status, stats_body = get((host, port), "/api/stats")
            assert stats_status == 200
            stats_payload = json_body(stats_body)

            record_status, record_body = get((host, port), "/api/verdicts/2026-09-03/aa11bb22")

        resolution = next(row for row in rows if row["task_id"] == "resolution")
        assert resolution["kind"] == "resolution"
        assert resolution["band"] == "RESOLVED"
        # A resolution record must never be counted as a failed judgment.
        assert stats_payload["total"] == 1
        assert stats_payload["failed"] == 0
        assert stats_payload["pass_rate"] == 100
        # …and its full record (band + nested verdict) is fetchable at its route.
        assert record_status == 200
        full = json_body(record_body)
        assert full["kind"] == "resolution"
        assert full["verdict"]["manifest"][0]["file"] == "engine/persist.py"
