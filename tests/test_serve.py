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
    assert json_body(body) == expected
    assert json_body(body)["stages"]["tier1"]["passed"] is True
    assert json_body(body)["stages"]["tier2"]["items"][0]["status"] == "PASS"


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
    assert json_body(body) == expected
    assert "worktree" not in json_body(body)
    assert "branch" not in json_body(body)


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
