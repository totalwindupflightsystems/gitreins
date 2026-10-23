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


def _jev_payload(noul: float, choice: str = "none", evidence_quality_score: float = 2.6) -> dict:
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
                "score": evidence_quality_score,
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

    def __init__(
        self, noul: float, choice: str = "none", evidence_quality_score: float = 2.6
    ) -> None:
        self._noul = noul
        self._choice = choice
        self._evidence_quality_score = evidence_quality_score

    def json(self) -> object:
        return _jev_payload(self._noul, self._choice, self._evidence_quality_score)


@pytest.fixture(autouse=True)
def _hermetic_credentials(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """No ambient credentials, no reachable .env, no real egress."""
    for var in resolution.CREDENTIAL_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    # JEVRES-006: the predispatch surface ships config-disabled; pin it OPEN
    # so the existing tests keep grading the policy they were written for.
    from engine.config import GitReinsDefaults

    defaults = GitReinsDefaults()
    defaults.resolution_enabled_predispatch = True
    monkeypatch.setattr(resolution, "resolution_config", lambda workdir=".": defaults)


@pytest.fixture
def script_assembler(monkeypatch: pytest.MonkeyPatch) -> object:
    """Replace hilo with a fixed bundle so the gate reaches the Jev call."""

    def _install() -> None:
        from engine.resolution import ManifestEntry, TraceSeed

        def fake_assemble_bundle(question: object, **kwargs: object) -> object:
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

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> None:
        self.calls += 1


class TestDecide:
    """``decide`` — pure policy over a verdict."""

    def test_resolved_maps_to_skip_dispatch(
        self, script_assembler: object, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        script_assembler()
        monkeypatch.setenv("GITREINS_OPENROUTER_KEY", _fake_key("dec"))
        verdict = resolution.resolve("q?", poster=lambda *a, **k: _StubResponse(0.87))
        record = decide(verdict)
        assert record["decision"] == DECISION_SKIP
        assert record["band"] == "RESOLVED"
        assert record["probability"] == pytest.approx(0.87)

    def test_weak_evidence_cannot_skip_a_false_premise(
        self, script_assembler: object, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Lexical overlap with a high noul must stay on the dispatch path."""
        script_assembler()
        monkeypatch.setenv("GITREINS_OPENROUTER_KEY", _fake_key("false-premise"))
        questions = (
            "Does the repo implement its guard engine in Go?",
            "Does the repo contain a Rust implementation of the guard engine?",
        )
        for question in questions:
            dispatch = _RecordingDispatch()
            record = preflight(
                question,
                workdir=".",
                dispatch=dispatch,
                poster=lambda *a, **k: _StubResponse(
                    0.91, choice="implementation", evidence_quality_score=1.2
                ),
            )
            assert record["band"] == "REVIEW"
            assert record["decision"] == DECISION_NOTE
            assert record["verdict"]["verdict"] == "REVIEW"
            assert dispatch.calls == 1

    def test_review_maps_to_dispatch_with_note(
        self, script_assembler: object, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        script_assembler()
        monkeypatch.setenv("GITREINS_OPENROUTER_KEY", _fake_key("dec"))
        verdict = resolution.resolve("q?", poster=lambda *a, **k: _StubResponse(0.50))
        record = decide(verdict)
        assert record["decision"] == DECISION_NOTE
        assert record["band"] == "REVIEW"

    def test_unresolved_maps_to_dispatch(
        self, script_assembler: object, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        script_assembler()
        monkeypatch.setenv("GITREINS_OPENROUTER_KEY", _fake_key("dec"))
        verdict = resolution.resolve("q?", poster=lambda *a, **k: _StubResponse(0.49))
        record = decide(verdict)
        assert record["decision"] == DECISION_DISPATCH
        assert record["band"] == "UNRESOLVED"

    def test_abstain_maps_to_dispatch_fail_open(self, script_assembler: object) -> None:
        script_assembler()
        # No key at all: the gate abstains (no-credentials); the policy must
        # still dispatch — this signal may skip work, never stop it.
        verdict = resolution.resolve("q?")
        assert verdict.verdict == "ABSTAIN"
        record = decide(verdict)
        assert record["decision"] == DECISION_DISPATCH
        assert record["abstain_reason"] == "no-credentials"

    def test_every_record_carries_probability_and_the_verdict_object(
        self, script_assembler: object, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No blind skip: even the skip record carries the probability + verdict."""
        script_assembler()
        monkeypatch.setenv("GITREINS_OPENROUTER_KEY", _fake_key("dec"))
        verdict = resolution.resolve("q?", poster=lambda *a, **k: _StubResponse(0.93))
        record = decide(verdict)
        assert record["probability"] == pytest.approx(0.93)
        assert record["missing_kind"] == "none"
        assert record["reason"]
        # DF-GITREINS-POC-37: the full verdict rides along as a first-class
        # OBJECT — the exact dict `resolve --json` prints, not an escaped
        # JSON string needing a second parse.
        assert isinstance(record["verdict"], dict)
        assert record["verdict"] == verdict.to_dict()
        assert record["verdict"]["verdict"] == "RESOLVED"
        assert record["verdict"]["probability"] == pytest.approx(0.93)
        # JEVRES-007: nothing was demoted, so there is no cause to name.
        assert record["demotion_reason"] is None


def _strong_entry() -> object:
    """One manifest entry that satisfies the floor's provenance requirement."""
    from engine.resolution import ManifestEntry

    return ManifestEntry(
        file="engine/guards.py",
        provenance="ast_exact",
        score=0.99,
        bytes=100,
        truncated=False,
        source="understand",
        lines=10,
    )


class TestEvidenceFloorDemotion:
    """JEVRES-007 — a floor demotion must name its cause, not just its band.

    ``effective_band_for`` returns a band and nothing else, so before this the
    record read ``band: REVIEW`` beside ``probability: 0.93`` with the generic
    "the evidence partially resolves the row" — misreporting evidence that fully
    resolves the row but misses the floor. The cause now rides in the additive
    ``demotion_reason`` field and in ``reason`` itself.
    """

    def test_low_evidence_quality_names_quality_score_and_floor(self) -> None:
        from engine.resolution import ResolutionVerdict

        verdict = ResolutionVerdict(
            question="Is the row already implemented?",
            verdict="RESOLVED",
            probability=0.93,
            evidence_quality=2,
            evidence_quality_score=2.0,
            manifest=[_strong_entry()],
        )
        cause = preflight_module.evidence_floor_reason(verdict)
        assert cause is not None
        # The observed score (integer), the field, and the floor — all named,
        # without dragging in the conditions that were satisfied.
        assert "evidence_quality 2 is below MIN_RESOLVED_EVIDENCE_QUALITY (3)" in cause
        assert "provenance" not in cause
        assert "clipped" not in cause

        record = decide(verdict)
        assert record["band"] == "REVIEW"
        assert record["decision"] == DECISION_NOTE
        assert record["demotion_reason"] == cause
        assert "evidence_quality 2 is below MIN_RESOLVED_EVIDENCE_QUALITY (3)" in record["reason"]
        # The misleading generic claim is gone, the action is preserved.
        assert "partially resolves" not in record["reason"]
        assert "dispatch-with-note" in record["reason"]

    def test_unreadable_evidence_quality_names_the_missing_answer(self) -> None:
        from engine.resolution import ResolutionVerdict

        verdict = ResolutionVerdict(
            question="q?",
            verdict="RESOLVED",
            probability=0.90,
            evidence_quality=None,
            evidence_quality_score=2.4,
            manifest=[_strong_entry()],
        )
        cause = preflight_module.evidence_floor_reason(verdict)
        assert cause is not None
        assert "evidence_quality could not be read" in cause
        assert "2.4" in cause  # the raw Jev score, since there is no integer
        assert "MIN_RESOLVED_EVIDENCE_QUALITY (3)" in cause

    def test_provenance_demotion_is_not_blamed_on_evidence_quality(self) -> None:
        """evidence_quality 3 is fine; the provenance condition is the cause."""
        from engine.resolution import ManifestEntry, ResolutionVerdict

        weak = ManifestEntry(
            file="engine/guards.py",
            provenance="lexical",
            score=0.99,
            bytes=100,
            truncated=False,
            source="bundle",
            lines=10,
        )
        verdict = ResolutionVerdict(
            question="q?",
            verdict="RESOLVED",
            probability=0.91,
            evidence_quality=3,
            manifest=[weak],
        )
        cause = preflight_module.evidence_floor_reason(verdict)
        assert cause is not None
        assert "no manifest entry has recognized provenance" in cause
        assert "lexical" in cause
        assert "evidence_quality" not in cause, "a satisfied floor must not be blamed"

        record = decide(verdict)
        assert record["band"] == "REVIEW"
        assert record["demotion_reason"] == cause
        assert "dispatch-with-note" in record["reason"]

    def test_clipping_is_named_as_the_cause(self) -> None:
        from engine.resolution import ResolutionVerdict

        verdict = ResolutionVerdict(
            question="q?",
            verdict="RESOLVED",
            probability=0.95,
            evidence_quality=3,
            manifest=[_strong_entry()],
            clipped=True,
        )
        cause = preflight_module.evidence_floor_reason(verdict)
        assert cause is not None
        assert "clipped" in cause
        assert "evidence_quality" not in cause

    def test_probability_review_is_untouched_and_names_no_demotion(
        self, script_assembler: object, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The normal REVIEW path keeps dispatch-with-note and a null cause."""
        script_assembler()
        monkeypatch.setenv("GITREINS_OPENROUTER_KEY", _fake_key("plain-review"))
        verdict = resolution.resolve("q?", poster=lambda *a, **k: _StubResponse(0.62))
        assert verdict.verdict == "REVIEW"
        record = decide(verdict)
        assert record["band"] == "REVIEW"
        assert record["decision"] == DECISION_NOTE
        assert record["demotion_reason"] is None
        assert record["reason"]
        assert "partially resolves" not in record["reason"]
        assert "dispatch-with-note" in record["reason"]


class TestPreflightDispatchHook:
    """``preflight`` — the dispatch hook fires exactly for dispatch decisions."""

    def test_skip_dispatch_never_invokes_the_hook(
        self, script_assembler: object, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        script_assembler()
        monkeypatch.setenv("GITREINS_OPENROUTER_KEY", _fake_key("pf"))
        dispatch = _RecordingDispatch()
        record = preflight(
            "q?", workdir=".", dispatch=dispatch, poster=lambda *a, **k: _StubResponse(0.85)
        )
        assert record["decision"] == DECISION_SKIP
        assert dispatch.calls == 0, "a RESOLVED premise must not spawn a worker"

    def test_evidence_floor_demotion_carries_the_cause_through_preflight(
        self, script_assembler: object, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """JEVRES-007 end to end: 0.91 + rubric position 1 is REVIEW with a cause.

        The live Jev answer scores ``evidence_quality`` 1 here (1.2 read off the
        0-3 legend), which the floor rejects. The record must still dispatch with
        the note — and must name ``evidence_quality``, the observed 1 and the
        floor 3, instead of claiming the evidence is merely partial.
        """
        script_assembler()
        monkeypatch.setenv("GITREINS_OPENROUTER_KEY", _fake_key("floor"))
        dispatch = _RecordingDispatch()
        record = preflight(
            "Is the row already implemented?",
            workdir=".",
            dispatch=dispatch,
            poster=lambda *a, **k: _StubResponse(0.91, evidence_quality_score=1.2),
        )
        assert record["probability"] == pytest.approx(0.91)
        assert record["band"] == "REVIEW"
        assert record["decision"] == DECISION_NOTE
        assert dispatch.calls == 1, "a REVIEW still dispatches, with the note"
        assert record["verdict"]["evidence_quality"] == 1
        cause = record["demotion_reason"]
        assert cause
        assert "evidence_quality 1 is below MIN_RESOLVED_EVIDENCE_QUALITY (3)" in cause
        assert "evidence floor" in record["reason"]
        assert "partially resolves" not in record["reason"]
        assert "dispatch-with-note" in record["reason"]

    def test_abstain_invokes_the_hook_and_records_the_reason(
        self, script_assembler: object
    ) -> object:
        """All keys invalid / poster failure -> work still proceeds, fail open."""
        script_assembler()

        def dead_poster(*_a: object, **_k: object) -> object:
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
        self, script_assembler: object, monkeypatch: pytest.MonkeyPatch
    ) -> None:
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

    def test_unresolved_invokes_the_hook(
        self, script_assembler: object, monkeypatch: pytest.MonkeyPatch
    ) -> None:
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

    def test_resolve_kwargs_are_forwarded(
        self, script_assembler: object, monkeypatch: pytest.MonkeyPatch
    ) -> object:
        """Seams flow through preflight untouched (contract the CLI relies on)."""
        script_assembler()
        monkeypatch.setenv("GITREINS_OPENROUTER_KEY", _fake_key("pf"))
        seen: dict = {}

        def poster(endpoint: object, key: str, body: str, timeout: float) -> object:
            seen["endpoint"] = endpoint
            return _StubResponse(0.87)

        record = preflight("q?", workdir=".", keys=["whatever"], poster=poster)
        assert record["decision"] == DECISION_SKIP
        assert seen["endpoint"] == resolution.JEV_ENDPOINT

    def test_module_exports_the_policy_api(self) -> None:
        """The names a foreman-side caller imports exist (smoke, keeps drift loud)."""
        for name in ("preflight", "decide", "DECISION_SKIP", "DECISION_NOTE", "DECISION_DISPATCH"):
            assert hasattr(preflight_module, name)


class TestSurfaceDisabledDetail:
    """DF-GITREINS-POC-81 — the disabled-surface record names the surface model.

    The enable flags are PER SURFACE and independent: a repo that runs
    ``gitreins resolve`` for real (``resolution.enabled.cli: true``) still
    abstains here, because ``predispatch`` is its own switch. The record used to
    carry only ``resolution.enabled.predispatch is false (or absent)``, which
    reads as "the gate is disabled in this repo" — the wrong conclusion, and the
    one that sent the reporter looking for a bug in the resolver. The detail now
    names the independence AND the surfaces that ARE enabled, so the way out
    (enable this one, or run the check on one that is already on) is in the
    record itself. `surface` remains the ONLY enable flag consulted: nothing
    falls back to another surface implicitly.
    """

    @staticmethod
    def _defaults(**enabled: bool) -> object:
        """Built-in defaults with exactly *enabled*'s surfaces flipped on."""
        from engine.config import GitReinsDefaults

        defaults = GitReinsDefaults()
        for surface, value in enabled.items():
            setattr(defaults, f"resolution_enabled_{surface}", value)
        return defaults

    def test_detail_names_the_surface_that_did_not_run_and_the_enabled_ones(self) -> None:
        record = preflight("row premise?", workdir=".", defaults=self._defaults(cli=True, mcp=True))
        assert record["band"] == "ABSTAIN"
        assert record["decision"] == DECISION_DISPATCH, "an ABSTAIN still fails open"
        detail = record["verdict"]["abstain_detail"]
        assert "resolution.enabled.predispatch is false" in detail
        assert "independently-switched" in detail
        assert "does not enable" in detail
        assert "now enabled: cli, mcp" in detail

    def test_detail_says_no_surface_is_enabled_when_none_is(self) -> None:
        """All four off: no sibling surface to point at, and the record says so."""
        record = preflight("row premise?", workdir=".", defaults=self._defaults())
        detail = record["verdict"]["abstain_detail"]
        assert "resolution.enabled.predispatch is false" in detail
        assert "no surface is enabled" in detail

    def test_the_cli_surface_runs_the_gate_in_a_cli_only_repo(
        self, script_assembler: object, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The reporter's repo shape: cli on, predispatch off.

        The resolver was never broken — the surface was. Naming the surface the
        repo does enable runs the same question through the real policy.
        """
        script_assembler()
        monkeypatch.setenv("GITREINS_OPENROUTER_KEY", _fake_key("poc81"))
        record = preflight(
            "row premise?",
            workdir=".",
            surface="cli",
            defaults=self._defaults(cli=True),
            poster=lambda *a, **k: _StubResponse(0.87),
        )
        assert record["band"] == "RESOLVED"
        assert record["decision"] == DECISION_SKIP
        assert record["abstain_reason"] is None
        assert record["probability"] == pytest.approx(0.87)

    def test_a_disabled_named_surface_does_not_fall_back_to_an_enabled_one(
        self, script_assembler: object, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`surface` is the ONLY switch consulted — no implicit fallback.

        A `preflight(surface="predispatch")` in a cli-only repo must abstain
        (fail open → dispatch), never quietly run the gate on `cli`: an
        unenabled surface must not egress, and a dispatch decision must not
        depend on which surface happened to be on.
        """
        script_assembler()
        monkeypatch.setenv("GITREINS_OPENROUTER_KEY", _fake_key("poc81"))
        calls: list = []
        record = preflight(
            "row premise?",
            workdir=".",
            surface="predispatch",
            defaults=self._defaults(cli=True),
            poster=lambda *a, **k: calls.append(1) or _StubResponse(0.87),
        )
        assert record["band"] == "ABSTAIN"
        assert record["abstain_reason"] == "surface-disabled"
        assert calls == [], "a disabled surface must not reach the provider"
