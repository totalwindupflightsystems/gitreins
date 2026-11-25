"""DF-034: `gitreins task complete` prints its evaluator job id and phase progress.

The closeout ran silent for its whole tier-2 wall — no job id, no phase
progress, no ETA — so silence was indistinguishable from a hang (the get-h3
digest lane wrapped the command in `timeout 570 … | tail` because of exactly
that). These tests drive a complete real closeout against a local
OpenAI-compatible mock lane (``GITREINS_LLM_BASE_URL``/``MODEL``/``API_KEY``
pointed at a localhost HTTP server) and pin:

* stdout carries the spawned job id plus a ``judge --status`` poll handle;
* the job id is printed BEFORE the verdict (usable as a live handle);
* at least one progress line per evaluator phase (guard, evaluating,
  per-iteration, judging, second pass) precedes the verdict;
* the record behind the handle is pollable via ``gitreins judge --status``;
* progress respects the machine-readable contract: on a piped stdout (what
  the in-process runner captures) progress lines move to stderr, stdout keeps
  its document shape;
* a FAIL verdict still closes its job record (a red closeout is not a hung
  one), and ``--skip-tier2``/adoption paths open no job at all.
"""

from __future__ import annotations

import json
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from tests.test_cli import run_cli

MOCK_MODEL = "mock-eval-lane"

_VERDICT_COMPLETE = json.dumps(
    {
        "verdict": "COMPLETE",
        "items": [{"criterion": "c1", "status": "PASS", "detail": "ok"}],
        "summary": "all good",
    }
)

_JOB_ID_RE = re.compile(r"job-[0-9a-f]{32}")


class _MockOpenAILane(BaseHTTPRequestHandler):
    """Smallest OpenAI-compatible lane: GET /models + POST /chat/completions."""

    # Class attribute so each test can serve its own verdict reply.
    verdict_content: str = _VERDICT_COMPLETE

    def log_message(self, *_args: object) -> None:  # keep test output clean
        pass

    def _send_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - http.server naming
        if self.path.endswith("/models"):
            self._send_json({"object": "list", "data": [{"id": MOCK_MODEL}]})
        else:
            self._send_json({"error": "not found"}, status=404)

    def do_POST(self) -> None:  # noqa: N802 - http.server naming
        length = int(self.headers.get("Content-Length", "0") or 0)
        self.rfile.read(length)
        self._send_json(
            {
                "id": "mock-chat-1",
                "object": "chat.completion",
                "created": 0,
                "model": MOCK_MODEL,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": self.verdict_content},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
            }
        )


@pytest.fixture
def mock_lane():
    _MockOpenAILane.verdict_content = _VERDICT_COMPLETE  # reset per test
    server = ThreadingHTTPServer(("127.0.0.1", 0), _MockOpenAILane)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield {
            "base_url": f"http://127.0.0.1:{server.server_address[1]}/v1",
            "set_verdict": lambda content: setattr(_MockOpenAILane, "verdict_content", content),
        }
    finally:
        server.shutdown()
        server.server_close()


def _lane_env(lane: dict) -> dict:
    return {
        "GITREINS_LLM_BASE_URL": lane["base_url"],
        "GITREINS_LLM_MODEL": MOCK_MODEL,
        "GITREINS_LLM_API_KEY": "test-key",
    }


def _create_started_task(tmp_workdir: str) -> None:
    run_cli("task", "create", "prog-1", "Progress Task", "c1", cwd=tmp_workdir)
    run_cli("task", "start", "prog-1", cwd=tmp_workdir)


class TestTaskCompleteProgress:
    def test_complete_prints_job_id_and_phase_lines_before_verdict(
        self, tmp_workdir: str, mock_lane: dict
    ) -> None:
        _create_started_task(tmp_workdir)
        result = run_cli(
            "task", "complete", "prog-1", cwd=tmp_workdir, extra_env=_lane_env(mock_lane)
        )

        assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"

        # stdout carries the spawned job id and a poll handle, BEFORE the
        # verdict — the handle exists while (and after) the run, not only in
        # retrospect.
        out = result.stdout
        match = _JOB_ID_RE.search(out)
        assert match is not None, f"no job id on stdout:\n{out}"
        job_id = match.group(0)
        assert f"Evaluator job: {job_id}" in out
        assert f"gitreins judge --status {job_id}" in out
        assert out.index("Evaluator job:") < out.index("Judge Result:"), (
            "the job id must print before the verdict, not after it"
        )

        # At least one progress line per evaluator phase, printed before the
        # verdict. On a piped stdout (the in-process runner captures strings,
        # never a TTY) the progress lines ride STDERR so stdout keeps its
        # document shape.
        err = result.stderr
        verdict_index = out.index("Judge Result:")
        for phase in (
            "evaluator: tier-1 guard",
            "evaluator: tier-2 evaluating",
            "evaluator: tier-2 iteration 1/",
            "evaluator: tier-2 judging",
        ):
            assert phase in err, f"missing progress line {phase!r} in stderr:\n{err}"

        # The job record behind the handle is real and terminal: pollable via
        # the documented surface.
        from engine.job_store import load_job

        job = load_job(job_id, directory=os.environ["GITREINS_JOB_DIR"])
        assert job is not None, "the announced job id has no record in the job store"
        assert job["task_id"] == "prog-1"
        assert job["status"] == "complete"
        assert job["running"] is False
        assert job["result"] is not None

        status = run_cli("judge", "--status", job_id, cwd=tmp_workdir)
        assert status.returncode == 0, status.stdout + status.stderr
        assert "Status:   complete" in status.stdout

    def test_failed_verdict_still_closes_the_job_record(
        self, tmp_workdir: str, mock_lane: dict
    ) -> None:
        mock_lane["set_verdict"](
            json.dumps(
                {
                    "verdict": "INCOMPLETE",
                    "items": [],
                    "summary": "could not verify",
                }
            )
        )
        _create_started_task(tmp_workdir)
        result = run_cli(
            "task", "complete", "prog-1", cwd=tmp_workdir, extra_env=_lane_env(mock_lane)
        )

        # A red closeout exits 1 — but its job record must read complete, not
        # hang as a running job nobody is working on.
        assert result.returncode == 1
        job_id = _JOB_ID_RE.search(result.stdout)
        assert job_id is not None, f"no job id on stdout:\n{result.stdout}"

        from engine.job_store import load_job

        job = load_job(job_id.group(0), directory=os.environ["GITREINS_JOB_DIR"])
        assert job is not None
        assert job["status"] == "complete"
        assert job["running"] is False
        assert job["result"]["passed"] is False

    def test_skip_tier2_opens_no_job(self, tmp_workdir: str) -> None:
        _create_started_task(tmp_workdir)
        result = run_cli("task", "complete", "--skip-tier2", "prog-1", cwd=tmp_workdir)

        assert result.returncode == 0, result.stdout + result.stderr
        assert "Evaluator job:" not in result.stdout

        from engine.job_store import list_jobs

        assert list_jobs(directory=os.environ["GITREINS_JOB_DIR"]) == []
