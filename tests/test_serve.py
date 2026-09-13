"""HTTP-layer tests for the live judgment viewer server."""

import http.client
import json
import threading
from pathlib import Path

import pytest

from gitreins import serve


@pytest.fixture()
def repo_fixture(tmp_path: Path) -> dict:
    """Create the smallest repository-shaped judgment viewer fixture."""
    history = tmp_path / ".gitreins" / "history"
    board = tmp_path / ".coding-hermes" / "board"
    history.mkdir(parents=True)
    board.mkdir(parents=True)

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


@pytest.fixture()
def live_server(repo_fixture: dict):
    """Run the production request handler against only the fixture repository."""

    class FixtureHandler(serve.Handler):
        pass

    FixtureHandler.workdir = str(repo_fixture["root"])
    FixtureHandler.project = ""
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
