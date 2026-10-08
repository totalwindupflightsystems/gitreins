# DF-GITREINS-POC-78: a background job dispatched with a credential that
# LOOKS usable (a non-empty key string) but is rejected by the provider used
# to hang in `running` forever. The probe proves the failure mode the fix
# relies on: a bad credential surfaces as an exception, deterministically,
# before any evaluation work is done.
from unittest.mock import patch

import requests

from engine.eval_cap import EvalCap


class TestVerifyProbe:
    """verify_probe(): one real round trip that proves the credential works."""

    def test_verify_probe_ok(self, llm_client):
        """A 200-with-choices round trip proves the credential usable."""
        import json as _json

        ok = requests.Response()
        ok.status_code = 200
        ok._content = _json.dumps(
            {"choices": [{"message": {"content": "pong"}}], "usage": {}}
        ).encode()
        ok.headers["Content-Type"] = "application/json"
        with patch("requests.post", return_value=ok):
            assert llm_client.verify_probe() is True

    def test_verify_probe_401_is_false(self, llm_client):
        """A rejected credential (401) reports False — no retry, one call."""
        import json as _json

        def _real_response(status, body=b"{}"):
            resp = requests.Response()
            resp.status_code = status
            resp._content = body
            resp.headers["Content-Type"] = "application/json"
            return resp

        resp = _real_response(401, _json.dumps({"error": {"message": "invalid api key"}}).encode())
        with patch("requests.post", return_value=resp) as post:
            ok = llm_client.verify_probe()
        assert ok is False
        assert post.call_count == 1, "probe must not retry a permanent rejection"

    def test_verify_probe_transport_error_is_false(self, llm_client):
        """A dead endpoint reports False (failure, not an 'usable' credential)."""
        with patch("requests.post", side_effect=requests.ConnectionError("connection refused")):
            assert llm_client.verify_probe() is False

    def test_verify_probe_ignores_mock_response_env(self, monkeypatch, llm_client):
        """GITREINS_MOCK_LLM_RESPONSE must not fake a passing probe — the probe
        exists precisely to prove the REAL wire accepts the credential."""
        monkeypatch.setenv("GITREINS_MOCK_LLM_RESPONSE", '{"content": "hi"}')
        with patch("requests.post", side_effect=requests.ConnectionError("no wire")):
            assert llm_client.verify_probe() is False


class TestJobTimeCapMidRun:
    """A dispatched background job must always carry a wall-clock ceiling.

    The stuck-`running` job (DF-GITREINS-POC-78) had NO time cap: a hung
    connect inside the guard hung the worker thread with nothing to cut it
    off. With a cap, `_check_hard_caps` refuses the next LLM call once the
    budget is spent — the evaluation ends and the job record goes terminal.
    """

    def test_mid_run_exhausted_cap_blocks_next_llm_call(self):
        cap = EvalCap(max_iterations=50, max_seconds=120.0)
        cap.start()  # timer starts at dispatch
        # Jump past the deadline
        cap.start_time -= 200.0

        exhausted = cap.check()
        assert exhausted is not None
        assert "Time cap" in exhausted

    def test_mid_run_cap_within_budget_allows_work(self):
        cap = EvalCap(max_iterations=50, max_seconds=120.0)
        cap.start()
        assert cap.check() is None

    def test_start_time_is_set_when_started(self):
        cap = EvalCap(max_iterations=50, max_seconds=120.0)
        assert cap.start_time == 0
        cap.start()
        assert cap.start_time > 0
