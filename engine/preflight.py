"""Pre-dispatch premise check (JEVRES-003) — the dispatch policy over a resolution verdict.

Spec: ``docs/jev-resolution-gate.md`` §4 row 1. The engine (``engine/resolution.py``,
JEVRES-001) answers "is the code we can point at enough to resolve this question?" —
this module turns that answer into a DISPATCH DECISION:

    RESOLVED   (>= 0.85)  ->  skip-dispatch       the row is annotated, NO worker spawned
    REVIEW     (>= 0.50)  ->  dispatch-with-note  dispatch, carrying missing_kind + probability
    UNRESOLVED (< 0.50)   ->  dispatch            nothing changes
    ABSTAIN    (any reason) -> dispatch          fail OPEN, by design — with the reason recorded

The asymmetry is the point. The Jev gate itself fails CLOSED (an ABSTAIN is never
read as a pass, spec §3.5) because it guards a merge decision. This policy consumes
the same verdict as a DISPATCH signal, where the dangerous failure is the opposite
one: a transport blip or a dead key must never become the reason work silently
stops happening. So an ABSTAIN dispatches and carries ``abstain_reason`` for the
operator; only a real RESOLVED probability (>= 0.85) is ever allowed to skip a
dispatch, and every record — including the skip — carries the probability,
``missing_kind`` and the full verdict object (``verdict``, the same dict
``gitreins resolve --json`` prints), so no skip is blind and every row can
be annotated with what the gate saw (spec §4 row 1: "annotate the row with the
bundle + probability").

``gitreins preflight`` (JEVRES-002's CLI pattern) is the surface; the hermes-side
foreman wiring that calls it per board row is external to this repo.
"""

from __future__ import annotations

from typing import Any, Callable

from engine.persist import persist_resolution
from engine.resolution import (
    VERDICT_ABSTAIN,
    VERDICT_RESOLVED,
    ResolutionVerdict,
    resolve,
)

__all__ = [
    "DECISION_DISPATCH",
    "DECISION_NOTE",
    "DECISION_SKIP",
    "DECISIONS",
    "decide",
    "preflight",
]

#: The row is already done — annotate it, do NOT spawn a worker.
DECISION_SKIP = "skip-dispatch"
#: Dispatch, but carry what is missing into the worker brief.
DECISION_NOTE = "dispatch-with-note"
#: Dispatch exactly as before.
DECISION_DISPATCH = "dispatch"

#: The four verdict bands each map onto exactly one decision; ABSTAIN maps to
#: :data:`DECISION_DISPATCH` (fail open) regardless of its reason.
DECISIONS: dict[str, str] = {
    VERDICT_RESOLVED: DECISION_SKIP,
    "REVIEW": DECISION_NOTE,
    "UNRESOLVED": DECISION_DISPATCH,
    VERDICT_ABSTAIN: DECISION_DISPATCH,
}

#: The policy's one-line why, per decision — printed by the CLI and recorded so
#: a human reading the annotation never has to re-derive the mapping.
_REASONS: dict[str, str] = {
    DECISION_SKIP: (
        "the evidence in the repo already resolves the row (probability >= 0.85) "
        "— annotate the row instead of dispatching a worker"
    ),
    DECISION_NOTE: (
        "the evidence partially resolves the row — dispatch and carry "
        "missing_kind + probability into the worker brief"
    ),
    DECISION_DISPATCH: "the row is not resolved — dispatch as usual",
}


def decide(verdict: ResolutionVerdict) -> dict[str, Any]:
    """Map a :class:`ResolutionVerdict` onto its dispatch decision.

    Pure policy over the verdict — no resolution runs here. The returned record
    carries the band, the probability, ``missing_kind``, the decision, the
    reason, the abstain reason when the verdict abstained, and the full verdict
    as a first-class OBJECT (``verdict`` — ``ResolutionVerdict.to_dict()``, the
    exact dict ``gitreins resolve --json`` prints; DF-GITREINS-POC-37 removed
    the old escaped ``verdict_json`` STRING that forced a second parse): a skip
    must never be blind, and an annotation must be able to show what the gate
    actually saw.
    """
    decision = DECISIONS[verdict.verdict]
    record: dict[str, Any] = {
        "question": verdict.question,
        "band": verdict.verdict,
        "probability": verdict.probability,
        "missing_kind": verdict.missing_kind,
        "decision": decision,
        "reason": _REASONS[decision],
        "abstain_reason": verdict.abstain_reason,
        "verdict": verdict.to_dict(),
    }
    return record


def preflight(
    question: str,
    *,
    workdir: str = ".",
    dispatch: Callable[[], None] | None = None,
    surface: str = "predispatch",
    defaults: Any = None,
    **resolve_kwargs: Any,
) -> dict[str, Any]:
    """Resolve *question* and return the dispatch record for it (spec §4 row 1).

    Runs the JEVRES-001 pipeline (:func:`engine.resolution.resolve` — never
    rebuilt here) and maps the verdict onto a decision with :func:`decide`.
    When *dispatch* is given, the policy invokes it exactly for the dispatch
    decisions (``dispatch-with-note`` and ``dispatch``) and never for
    ``skip-dispatch`` — that hook is what makes "no worker spawned" testable
    and lets a foreman hand in its own dispatch step. An ABSTAIN dispatches
    too: this signal may skip a dispatch, it must never be the reason work
    silently stops.

    Config gate (JEVRES-006): the gate runs only when
    ``resolution.enabled.<surface>`` is true in config — *surface* defaults to
    ``"predispatch"`` here, the judge pre-screen passes its own. Disabled, no
    resolution runs at all and the record carries an ABSTAIN verdict with the
    named ``surface-disabled`` reason, which — like every ABSTAIN on this
    surface — maps to plain ``dispatch`` (fail open) with the reason recorded.

    All other keyword arguments (``keys``, ``poster``, ``runner``, ... — the
    test seams included) are forwarded to :func:`resolve` untouched.
    """
    from engine.resolution import (
        ResolutionVerdict,
        VERDICT_ABSTAIN,
        surface_enabled,
    )

    enabled, reason = surface_enabled(surface, workdir=workdir, defaults=defaults)
    if not enabled:
        verdict = ResolutionVerdict(
            question=question,
            verdict=VERDICT_ABSTAIN,
            abstain_reason=reason,
            abstain_detail=(
                f"resolution.enabled.{surface} is false (or absent) in"
                f" {workdir}/.gitreins/config.yaml"
            ),
        )
        record = decide(verdict)
        if dispatch is not None:
            dispatch()
        return record

    verdict = resolve(question, workdir=workdir, **resolve_kwargs)
    record = decide(verdict)
    # DF-GITREINS-POC-36: the decision is filed in the same history store the
    # judge writes to, through the SHARED helper — this surface must never grow
    # its own writer (the POC-12/POC-16 second-implementation drift). An ABSTAIN
    # (including the surface-disabled branch above, which returns before this
    # point) writes nothing: it is a non-event, not a decision. The write is
    # non-fatal by the helper's contract, so recording can never change the
    # dispatch decision this surface exists to make.
    persist_resolution(workdir, verdict, surface=surface)
    if dispatch is not None and record["decision"] != DECISION_SKIP:
        dispatch()
    return record
