"""Judge-lane coherence validation (CH-MAINT-015).

The judge lane resolves from GITREINS_LLM_* env only (INT-CI-021), so an
incoherent triple used to surface only as HTTP 400s on every tier-2
iteration and a FAIL [DEGRADED] INCOMPLETE verdict. ``LLMClient.validate_lane``
probes ``GET {base_url}/models`` once per base_url (process-lifetime cache)
and raises :class:`JudgeLaneIncoherent` naming base_url, model id, and the
reason before any tier-1 work starts.

All HTTP is mocked here — no live calls.
"""

import json
import os
from unittest.mock import MagicMock, patch

import pytest
import requests

from engine.llm import JudgeLaneIncoherent, LLMClient, _served_models_cache

ZAI_BASE = "https://api.z.ai/api/coding/paas/v4"


@pytest.fixture(autouse=True)
def _clean_env_and_cache(monkeypatch):
    """No ambient mock/skip flags and an empty /models cache per test."""
    monkeypatch.delenv("GITREINS_MOCK_LLM_RESPONSE", raising=False)
    monkeypatch.delenv("GITREINS_LLM_SKIP_MODEL_CHECK", raising=False)
    monkeypatch.delenv("GITREINS_LLM_BASE_URL", raising=False)
    monkeypatch.delenv("GITREINS_LLM_MODEL", raising=False)
    monkeypatch.delenv("GITREINS_LLM_API_KEY", raising=False)
    _served_models_cache.clear()
    yield
    _served_models_cache.clear()


def _client(**kw) -> LLMClient:
    kw.setdefault("base_url", ZAI_BASE)
    kw.setdefault("api_key", "test-key-not-a-secret")
    kw.setdefault("model", "glm-5.3-flash")
    return LLMClient(**kw)


def _models_response(ids: list[str], status: int = 200) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = {"object": "list", "data": [{"id": i} for i in ids]}
    return resp


class TestValidateLane:
    """validate_lane: mismatch, match, auth, skip flag, caching."""

    def test_mismatch_refuses_naming_the_pair(self) -> None:
        """A model the endpoint does not serve → named refusal, one probe."""
        with patch(
            "engine.llm.requests.get",
            return_value=_models_response(["glm-5.3-flash", "glm-5"]),
        ) as get:
            with pytest.raises(JudgeLaneIncoherent) as ei:
                _client(model="moonshotai/kimi-k3").validate_lane()
        msg = str(ei.value)
        assert "judge lane incoherent" in msg
        assert ZAI_BASE in msg
        assert "moonshotai/kimi-k3" in msg
        assert "does not serve" in msg
        assert "GITREINS_LLM_MODEL" in msg
        assert "GITREINS_LLM_BASE_URL" in msg
        # Never the key.
        assert "test-key-not-a-secret" not in msg
        get.assert_called_once()

    def test_matching_model_proceeds_and_returns_served_set(self) -> None:
        with patch(
            "engine.llm.requests.get",
            return_value=_models_response(["glm-5.3-flash", "glm-5"]),
        ):
            served = _client(model="glm-5.3-flash").validate_lane()
        assert served == frozenset({"glm-5.3-flash", "glm-5"})

    def test_models_401_refusal_names_auth(self) -> None:
        with patch("engine.llm.requests.get", return_value=_models_response([], 401)) as get:
            with pytest.raises(JudgeLaneIncoherent) as ei:
                _client().validate_lane()
        msg = str(ei.value)
        assert ZAI_BASE in msg
        assert "glm-5.3-flash" in msg
        assert "(HTTP 401 from /models)" in msg
        assert "credential" in msg
        assert "GITREINS_LLM_API_KEY" in msg
        get.assert_called_once()

    def test_models_400_refusal_uses_measured_shape(self) -> None:
        with patch("engine.llm.requests.get", return_value=_models_response([], 400)):
            with pytest.raises(JudgeLaneIncoherent) as ei:
                _client(model="moonshotai/kimi-k3").validate_lane()
        assert "(HTTP 400 from /models)" in str(ei.value)

    def test_unreachable_endpoint_refusal(self) -> None:
        with patch("engine.llm.requests.get", side_effect=requests.ConnectionError("conn refused")):
            with pytest.raises(JudgeLaneIncoherent) as ei:
                _client().validate_lane()
        msg = str(ei.value)
        assert "unreachable" in msg
        assert ZAI_BASE in msg
        assert "glm-5.3-flash" in msg

    def test_skip_flag_bypasses_probe(self) -> None:
        monkey_skip = {"GITREINS_LLM_SKIP_MODEL_CHECK": "1"}
        with patch("engine.llm.requests.get") as get, patch.dict(os.environ, monkey_skip):
            assert _client(model="never-served-anywhere").validate_lane() == frozenset()
        get.assert_not_called()

    def test_cache_one_probe_per_base_url(self) -> None:
        """Two clients against the same base_url → a single HTTP probe."""
        with patch(
            "engine.llm.requests.get",
            return_value=_models_response(["glm-5.3-flash"]),
        ) as get:
            first = _client()
            second = LLMClient(base_url=ZAI_BASE, api_key="other", model="glm-5.3-flash")
            assert first.validate_lane() == frozenset({"glm-5.3-flash"})
            assert second.validate_lane() == frozenset({"glm-5.3-flash"})
        assert get.call_count == 1

    def test_probe_failure_is_not_cached(self) -> None:
        """A transient 5xx must not poison the process-lifetime cache."""
        with patch(
            "engine.llm.requests.get",
            side_effect=[_models_response([], 500), _models_response(["glm-5.3-flash"])],
        ) as get:
            with pytest.raises(JudgeLaneIncoherent):
                _client().validate_lane()
            assert _client().validate_lane() == frozenset({"glm-5.3-flash"})
        assert get.call_count == 2

    def test_refusal_names_skip_hatch(self) -> None:
        with patch("engine.llm.requests.get", return_value=_models_response(["glm-5"])):
            with pytest.raises(JudgeLaneIncoherent) as ei:
                _client(model="glm-5.3-flash").validate_lane()
        assert "GITREINS_LLM_SKIP_MODEL_CHECK" in str(ei.value)


class TestValidateLaneSkipsMockedLane:
    """A mocked lane (GITREINS_MOCK_LLM_RESPONSE) makes no real HTTP call."""

    def test_mocked_lane_skips_probe(self) -> None:
        with (
            patch("engine.llm.requests.get") as get,
            patch.dict(os.environ, {"GITREINS_MOCK_LLM_RESPONSE": '{"content": "ok"}'}),
        ):
            assert _client(model="moonshotai/kimi-k3").validate_lane() == frozenset()
        get.assert_not_called()


class TestCliJudgeLaneRefusal:
    """End-to-end: `gitreins judge` refuses an incoherent lane, exit 1, fast."""

    def test_judge_refuses_incoherent_lane_fast(self, tmp_workdir: str) -> None:
        """Unreachable base_url → named refusal, exit 1, no tier-1 guard work."""
        from tests.test_cli import run_cli

        run_cli("task", "create", "PROBE-CLI", "t", "criterion one", cwd=tmp_workdir)
        result = run_cli(
            "judge",
            "PROBE-CLI",
            cwd=tmp_workdir,
            extra_env={
                "GITREINS_LLM_BASE_URL": "http://127.0.0.1:9/v1",
                "GITREINS_LLM_MODEL": "moonshotai/kimi-k3",
                "GITREINS_LLM_API_KEY": "placeholder-key",
            },
            # Unset-wins: the mock/skip escape hatches must be ABSENT so the
            # coherence gate actually runs (run_cli sets the mock credential
            # only for GITREINS_MOCK_LLM_RESPONSE extra_env, which is absent).
            unset_env=("GITREINS_MOCK_LLM_RESPONSE", "GITREINS_LLM_SKIP_MODEL_CHECK"),
        )
        assert result.returncode == 1, result.stdout + result.stderr
        output = result.stdout + result.stderr
        assert "judge lane incoherent" in output
        assert "http://127.0.0.1:9/v1" in output
        assert "moonshotai/kimi-k3" in output
        assert "exit code 1" in output
        # The refusal precedes the guard: no tier-1 narration anywhere.
        assert "Running static guards" not in output

    def test_judge_with_skip_flag_and_mock_proceeds_to_grading(self, tmp_workdir: str) -> None:
        """The escape hatch unblocks the same lane for a mocked run."""
        from tests.test_cli import run_cli

        verdict_json = '{"verdict":"COMPLETE","items":[{"criterion":"c1","status":"PASS","detail":"ok"}],"summary":"all good"}'
        run_cli("task", "create", "PROBE-SKIP", "t", "criterion one", cwd=tmp_workdir)
        result = run_cli(
            "judge",
            "PROBE-SKIP",
            cwd=tmp_workdir,
            extra_env={
                "GITREINS_LLM_BASE_URL": "http://127.0.0.1:9/v1",
                "GITREINS_LLM_MODEL": "moonshotai/kimi-k3",
                "GITREINS_LLM_API_KEY": "placeholder-key",
                "GITREINS_LLM_SKIP_MODEL_CHECK": "1",
                "GITREINS_MOCK_LLM_RESPONSE": json.dumps({"content": verdict_json}),
            },
        )
        output = result.stdout + result.stderr
        assert "judge lane incoherent" not in output
        assert "Judge Result" in output
