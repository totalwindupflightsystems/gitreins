"""JEVRES-003 — hermetic tests for the pre-dispatch premise check.

The policy under test is ``engine/preflight.py``; the verdicts come from
``engine/resolution.py`` (JEVRES-001) driven through the SAME seams its own
tests use: a ``poster`` stub returns a payload, or raises — there is never a
network call, and no key is needed (keys are irrelevant when the poster is
stubbed; the autouse fixture still clears ambient credentials and moves HOME
so a regression to the real poster fails loudly as ``no-credentials``).

The dispatch hook is the contract under test: invoked for every dispatch
decision (including the fail-open ABSTAIN), never for ``skip-dispatch``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from engine import preflight as preflight_module
from engine import resolution
from engine.preflight import (
    DECISION_DISPATCH,
    DECISION_NOTE,
    DECISION_SKIP,
    decide,
    preflight,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


def _fake_key(tag: str) -> str:
    """An OpenRouter-shaped key assembled at runtime — no sk- literal here."""
    return "sk-or-" + "v1-" + tag + "-" + "0" * 16


def _jev_payload(noul: float, choice: str = "none") -> dict:
    """The live decisions-endpoint answer shape, exactly as measured."""
    return {
        "model": "typesafe/jev-1.13-20260917",
        "answers": {
            "resolves": {"type": "noul", "noul": noul},
            "missing_kind": {
                "type": "choice",
                "choice": choice,
                "probabilities": {"none": 0.93, "implementation": 0.05, "test": 0.02},
                "confidence": 0.9,
            },
            "evidence_quality": {
                "type": "score",
                "score": 2.6,
                "legend": {
                    "0": "mentions only",
                    "1": "adjacent code",
                    "2": "the exact code path",
                    "3": "path plus its test",
                },
                "probabilities": {"0": 0.01, "1": 0.05, "2": 0.84, "3": 0.10},
                "confidence": 0.77,
            },
        },
        "usage": {"input_tokens": 520, "output_tokens": 96, "cost": 2.184e-05},
        "id": "gen-dec-test-preflight-0001",
        "provider": "TypeSafe",
    }


class _StubResponse:
    status_code = 200

    def __init__(self, noul: float, choice: str = "none"):
        self._noul = noul
        self._choice = choice

    def json(self):
        return _jev_payload(self._noul, self._choice)


@pytest.fixture(autouse=True)
def _hermetic_credentials(monkeypatch, tmp_path):
    """No ambient credentials, no reachable .env, no real egress."""
    for var in resolution.CREDENTIAL_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def script_assembler(monkeypatch):
    """Replace hilo with a fixed bundle so the gate reaches the Jev call."""

    def _install() -> None:
        from engine.resolution import ManifestEntry, TraceSeed

        def fake_assemble_bundle(question, **kwargs):
            return resolution.AssembledBundle(
                text="## MAP\nengine/evidence_bounds.py →\n  - bound_evidence\n",
                manifest=[
                    ManifestEntry(
                        file="engine/evidence_bounds.py",
                        provenance="ast_exact",
                        score=1.0,
                        bytes=1024,
                        truncated=False,
                        source="understand",
                        lines=24,
                    )
                ],
                seeds=[TraceSeed(file="engine/evidence_bounds.py", score=0.9)],
                tokens_estimated=300,
            )

        monkeypatch.setattr(resolution, "assemble_bundle", fake_assemble_bundle)

    return _install


class _RecordingDispatch:
    """The caller's dispatch step, remembering how often it ran."""

    def __init__(self):
        self.calls = 0

    def __call__(self) -> None:
        self.calls += 1


class TestDecide:
    """``decide`` — pure policy over a verdict."""

    def test_resolved_maps_to_skip_dispatch(self, script_assembler, monkeypatch):
        script_assembler()
        monkeypatch.setenv("GITREINS_OPENROUTER_KEY", _fake_key("dec"))
        verdict = resolution.resolve("q?", poster=lambda *a, **k: _StubResponse(0.87))
        record = decide(verdict)
        assert record["decision"] == DECISION_SKIP
        assert record["band"] == "RESOLVED"
        assert record["probability"] == pytest.approx(0.87)

    def test_review_maps_to_dispatch_with_note(self, script_assembler, monkeypatch):
        script_assembler()
        monkeypatch.setenv("GITREINS_OPENROUTER_KEY", _fake_key("dec"))
        verdict = resolution.resolve("q?", poster=lambda *a, **k: _StubResponse(0.50))
        record = decide(verdict)
        assert record["decision"] == DECISION_NOTE
        assert record["band"] == "REVIEW"

    def test_unresolved_maps_to_dispatch(self, script_assembler, monkeypatch):
        script_assembler()
        monkeypatch.setenv("GITREINS_OPENROUTER_KEY", _fake_key("dec"))
        verdict = resolution.resolve("q?", poster=lambda *a, **k: _StubResponse(0.49))
        record = decide(verdict)
        assert record["decision"] == DECISION_DISPATCH
        assert record["band"] == "UNRESOLVED"

    def test_abstain_maps_to_dispatch_fail_open(self, script_assembler):
        script_assembler()
        # No key at all: the gate abstains (no-credentials); the policy must
        # still dispatch — this signal may skip work, never stop it.
        verdict = resolution.resolve("q?")
        assert verdict.verdict == "ABSTAIN"
        record = decide(verdict)
        assert record["decision"] == DECISION_DISPATCH
        assert record["abstain_reason"] == "no-credentials"

    def test_every_record_carries_probability_and_full_verdict_json(
        self, script_assembler, monkeypatch
    ):
        """No blind skip: even the skip record carries the probability + verdict."""
        import json as _json

        script_assembler()
        monkeypatch.setenv("GITREINS_OPENROUTER_KEY", _fake_key("dec"))
        verdict = resolution.resolve("q?", poster=lambda *a, **k: _StubResponse(0.93))
        record = decide(verdict)
        assert record["probability"] == pytest.approx(0.93)
        assert record["missing_kind"] == "none"
        assert record["reason"]
        # The full verdict JSON round-trips — the annotation shows what the gate saw.
        parsed = _json.loads(record["verdict_json"])
        assert parsed["verdict"] == "RESOLVED"
        assert parsed["probability"] == pytest.approx(0.93)


class TestPreflightDispatchHook:
    """``preflight`` — the dispatch hook fires exactly for dispatch decisions."""

    def test_skip_dispatch_never_invokes_the_hook(self, script_assembler, monkeypatch):
        script_assembler()
        monkeypatch.setenv("GITREINS_OPENROUTER_KEY", _fake_key("pf"))
        dispatch = _RecordingDispatch()
        record = preflight(
            "q?", workdir=".", dispatch=dispatch, poster=lambda *a, **k: _StubResponse(0.85)
        )
        assert record["decision"] == DECISION_SKIP
        assert dispatch.calls == 0, "a RESOLVED premise must not spawn a worker"

    def test_abstain_invokes_the_hook_and_records_the_reason(self, script_assembler):
        """All keys invalid / poster failure -> work still proceeds, fail open."""
        script_assembler()

        def dead_poster(*_a, **_k):
            response = _StubResponse(0.99)
            response.status_code = 401
            return response

        dispatch = _RecordingDispatch()
        record = preflight(
            "q?",
            workdir=".",
            dispatch=dispatch,
            keys=[_fake_key("dead")],
            poster=dead_poster,
        )
        assert record["decision"] == DECISION_DISPATCH
        assert record["abstain_reason"] == "all-credentials-rejected"
        assert dispatch.calls == 1, "an ABSTAIN must never be the reason work stops"

    def test_review_invokes_the_hook_and_carries_the_note_fields(
        self, script_assembler, monkeypatch
    ):
        script_assembler()
        monkeypatch.setenv("GITREINS_OPENROUTER_KEY", _fake_key("pf"))
        dispatch = _RecordingDispatch()
        record = preflight(
            "q?",
            workdir=".",
            dispatch=dispatch,
            poster=lambda *a, **k: _StubResponse(0.60, choice="test"),
        )
        assert record["decision"] == DECISION_NOTE
        assert dispatch.calls == 1
        assert record["missing_kind"] == "test"
        assert record["probability"] == pytest.approx(0.60)

    def test_unresolved_invokes_the_hook(self, script_assembler, monkeypatch):
        script_assembler()
        monkeypatch.setenv("GITREINS_OPENROUTER_KEY", _fake_key("pf"))
        dispatch = _RecordingDispatch()
        record = preflight(
            "q?",
            workdir=".",
            dispatch=dispatch,
            poster=lambda *a, **k: _StubResponse(0.10, choice="implementation"),
        )
        assert record["decision"] == DECISION_DISPATCH
        assert dispatch.calls == 1
        assert record["missing_kind"] == "implementation"

    def test_resolve_kwargs_are_forwarded(self, script_assembler, monkeypatch):
        """Seams flow through preflight untouched (contract the CLI relies on)."""
        script_assembler()
        monkeypatch.setenv("GITREINS_OPENROUTER_KEY", _fake_key("pf"))
        seen: dict = {}

        def poster(endpoint, key, body, timeout):
            seen["endpoint"] = endpoint
            return _StubResponse(0.87)

        record = preflight("q?", workdir=".", keys=["whatever"], poster=poster)
        assert record["decision"] == DECISION_SKIP
        assert seen["endpoint"] == resolution.JEV_ENDPOINT

    def test_module_exports_the_policy_api(self):
        """The names a foreman-side caller imports exist (smoke, keeps drift loud)."""
        for name in ("preflight", "decide", "DECISION_SKIP", "DECISION_NOTE", "DECISION_DISPATCH"):
            assert hasattr(preflight_module, name)
