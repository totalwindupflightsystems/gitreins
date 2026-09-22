"""Judge pre-screen (tier 1.5) + per-criterion attribution (JEVRES-004).

Spec: ``docs/jev-resolution-gate.md`` §4 rows 2 and 4. The engine
(``engine/resolution.py``, JEVRES-001) answers "is the code we can point at
enough to resolve this question?" — this module applies that question to a
task's acceptance criteria BEFORE the expensive tier-2 judge loop runs, and
carries the answer into the judge input and the verdict artifact:

    GOAL A (pre-screen)  — one batched Jev call over the criteria resolves
                           per-criterion probability, ``missing_kind`` and
                           evidence quality. The block is injected into the
                           evaluator prompt as INPUT ONLY: a criterion at 0.9
                           with ``missing_kind='none'`` and one at 0.3 with
                           ``missing_kind='test'`` should not cost the judge
                           the same amount of reasoning. The judge's authority
                           is untouched — the pre-screen never skips tier 2
                           (that is JEVRES-005's measurement, not this change).
    GOAL B (attribution) — the verdict's per-criterion items carry the
                           resolution probability and the cited code path, so
                           "3/3 PASS" becomes
                           "3/3 PASS, 0.91/0.88/0.93, citing engine/x.py:12".

Fail-closed degraded to today's path: when Jev is unavailable (no key, every
key refused, transport error, malformed answer, no hilo / empty bundle) the
pre-screen reports ``abstained`` with the engine's named reason, the evaluator
logs ONE warning line and runs the judge exactly as before — no prompt
injection, no verdict fields, no behavioural drift (acceptance criterion 3).

Persistence rides the existing surface: the full engine verdict dict is
attached to the parsed Verdict (``Verdict.prescreen``) and lands in
``.gitreins/history`` through ``engine.persist.build_verdict_data`` — no new
persistence store, nothing that can drift from the verdict ``gitreins serve``
reads (DF-GITREINS-POC-23 lesson).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from engine.resolution import (
    VERDICT_ABSTAIN,
    ResolutionVerdict,
    resolve,
    rubric_position,
)

__all__ = [
    "PRESCREEN_KEY",
    "WARN_TEMPLATE",
    "CriterionPrescreen",
    "PrescreenResult",
    "assemble_prescreen_task",
    "attach_prescreen",
    "attribute_items",
    "build_prescreen_question",
    "criterion_citation_paths",
    "run_prescreen",
]

#: Where an injected pre-screen rides on the evaluator's task dict. This is
#: the test seam (a :class:`PrescreenResult` here is used as-is, no Jev call)
#: and the forward hook for a pipeline that wants to pass a cached pre-screen.
PRESCREEN_KEY = "prescreen"

#: The one warning line an ABSTAIN costs (logged by the evaluator).
WARN_TEMPLATE = "jev pre-screen unavailable (%s) — tier-2 judge runs at full authority"

#: Criteria are plain text (the board stores them that way), so only the plain
#: text makes it into the resolution question — a criterion that is a dict is
#: skipped rather than str()ed into noise.
MAX_CRITERION_CHARS = 600

#: ``engine/x.py``, ``engine/x.py:12`` or ``engine/x.py:12-40`` — a code path a
#: criterion text names directly. Extensions are the repo's own mix; a bare
#: line number or a version number never matches.
_CITED_PATH_RE = re.compile(
    r"(?<![\w./-])"
    r"(?P<spec>[\w./-]+\.(?:py|go|ts|tsx|js|jsx|rs|java|rb|sh|md|yaml|yml|json|toml)"
    r"(?::\d+(?:-\d+)?)?)"
    r"(?![\w.-])"
)

#: Direction a missing piece pushes the whole-set probability per criterion.
#: The evidence is ONE answer for the batch (one flat-cost call, spec §2), so
#: the per-criterion spread is a deterministic derivation from the batch
#: answer — never a second model opinion: implementation-missing cannot make a
#: criterion MORE likely satisfied than the evidence says the set is.
_MISSING_DELTA: dict[str, float] = {
    "none": 0.0,
    "docs": -0.05,
    "config": -0.10,
    "wiring": -0.15,
    "test": -0.15,
    "implementation": -0.25,
}

#: Fallback for a ``missing_kind`` label outside the criteria map (a new label
#: from a newer Jev build must degrade, not crash the judge input).
_MISSING_DELTA_DEFAULT = -0.15

_MISSING_LABELS: dict[str, str] = {
    "none": "nothing — the evidence already resolves it",
    "implementation": "the implementation itself",
    "test": "a test verifying the behaviour",
    "wiring": "the wiring from the real entry point",
    "config": "the configuration that enables it",
    "docs": "the documentation of it",
}
_MISSING_LABEL_DEFAULT = "an unnamed piece ({kind})"


def criterion_citation_paths(criterion: str, *, limit: int = 5) -> list[str]:
    """Code paths the criterion text itself names, in order, deduped.

    A criterion that says "wired in engine/evaluator.py:1180" carries its own
    attribution; this is the cheap, deterministic half of GOAL B — the citation
    exists even when the bundle's top-ranked file is a different one. Excluded
    paths (``.env`` etc., the spec §6.3 set) are never cited.
    """
    from engine.resolution import is_excluded_path

    out: list[str] = []
    seen: set[str] = set()
    for match in _CITED_PATH_RE.finditer(criterion or ""):
        spec = match.group("spec")
        path = spec.split(":", 1)[0]
        if path in seen or is_excluded_path(path):
            continue
        seen.add(path)
        out.append(spec)
        if len(out) >= limit:
            break
    return out


def build_prescreen_question(criteria: list[str]) -> str:
    """The question the ONE pre-screen Jev call resolves (spec §3.4 shape).

    The criteria travel numbered and whitespace-normalized, clipped per
    criterion so a pathological board row cannot eat the bundle budget.
    """
    lines = [
        "For each numbered acceptance criterion of this task, is the repository's "
        "own code sufficient evidence that the criterion is satisfied?",
        "",
    ]
    for index, criterion in enumerate(criteria, 1):
        text = " ".join(str(criterion).split())
        if len(text) > MAX_CRITERION_CHARS:
            text = text[:MAX_CRITERION_CHARS].rstrip() + "…"
        lines.append(f"{index}. {text}")
    return "\n".join(lines)


def _evidence_quality_label(verdict: ResolutionVerdict) -> str:
    """The rubric band the bundle landed on, as readable text."""
    position = verdict.evidence_quality
    legend = verdict.evidence_quality_legend or {}
    if position is not None and str(position) in legend:
        return legend[str(position)]
    labels = {
        0: "mentions only",
        1: "adjacent code",
        2: "the exact code path",
        3: "the exact code path plus its test",
    }
    if position in labels:
        return labels[position]
    return "unknown"


def _missing_kind_label(kind: str) -> str:
    return _MISSING_LABELS.get(kind, _MISSING_LABEL_DEFAULT.format(kind=kind))


def _per_criterion_probabilities(probability: float | None, kinds: list[str]) -> list[float | None]:
    """Spread the one batch probability across the criteria.

    Deterministic derivation, clamped into [0, 1] — the batch answer is the
    only model opinion here (one call, one flat cost, spec §2), and the deltas
    only ever move DOWN from what the evidence supports, except the
    ``missing_kind='none'`` case which moves nothing.
    """
    if probability is None:
        return [None] * len(kinds)
    out: list[float | None] = []
    for kind in kinds:
        delta = _MISSING_DELTA.get(kind, _MISSING_DELTA_DEFAULT)
        out.append(round(min(1.0, max(0.0, probability + delta)), 2))
    return out


@dataclass
class CriterionPrescreen:
    """One criterion's pre-screen row — the GOAL A / GOAL B unit."""

    criterion: str
    probability: float | None
    missing_kind: str
    evidence_quality: str
    citations: list[str] = field(default_factory=list)

    @property
    def cited_path(self) -> str:
        return ", ".join(self.citations)

    def to_dict(self) -> dict[str, Any]:
        return {
            "criterion": self.criterion,
            "probability": self.probability,
            "missing_kind": self.missing_kind,
            "evidence_quality": self.evidence_quality,
            "cited_path": self.cited_path,
        }


@dataclass
class PrescreenResult:
    """The tier 1.5 answer: batch verdict, per-criterion rows, provenance.

    ``verdict_dict`` is the engine's full :meth:`ResolutionVerdict.to_dict` —
    bundle manifest, model build id, token counts, cost, disclosure — so the
    persisted artifact is traceable without this module growing a second
    persistence shape. ``abstained`` is the fail-closed path: the judge runs
    exactly as today and no field of the verdict changes.
    """

    question: str = ""
    band: str = ""
    probability: float | None = None
    missing_kind: str = ""
    missing_kind_label: str = ""
    evidence_quality: str = ""
    evidence_quality_position: int | None = None
    criteria: list[CriterionPrescreen] = field(default_factory=list)
    bundle_disclosure: str = ""
    verdict_dict: dict[str, Any] | None = None
    abstained: bool = False
    abstain_reason: str | None = None
    abstain_detail: str | None = None

    @classmethod
    def from_verdict(cls, verdict: ResolutionVerdict, criteria: list[str]) -> "PrescreenResult":
        kinds = [verdict.missing_kind or "none"] * len(criteria)
        probabilities = _per_criterion_probabilities(verdict.probability, kinds)
        quality = _evidence_quality_label(verdict)
        rows = [
            CriterionPrescreen(
                criterion=criterion,
                probability=probabilities[i],
                missing_kind=kinds[i],
                evidence_quality=quality,
                citations=criterion_citation_paths(criterion),
            )
            for i, criterion in enumerate(criteria)
        ]
        return cls(
            question=verdict.question,
            band=verdict.verdict,
            probability=verdict.probability,
            missing_kind=verdict.missing_kind or "none",
            missing_kind_label=_missing_kind_label(verdict.missing_kind or "none"),
            evidence_quality=quality,
            evidence_quality_position=rubric_position(
                verdict.evidence_quality_score, verdict.evidence_quality_legend
            )
            if verdict.evidence_quality_score is not None
            else verdict.evidence_quality,
            criteria=rows,
            bundle_disclosure=verdict.clip_disclosure,
            verdict_dict=verdict.to_dict(),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "band": self.band,
            "probability": self.probability,
            "missing_kind": self.missing_kind,
            "missing_kind_label": self.missing_kind_label,
            "evidence_quality": self.evidence_quality,
            "evidence_quality_position": self.evidence_quality_position,
            "criteria": [row.to_dict() for row in self.criteria],
            "bundle_disclosure": self.bundle_disclosure,
            "verdict": dict(self.verdict_dict) if self.verdict_dict else None,
            "abstained": self.abstained,
            "abstain_reason": self.abstain_reason,
        }


def run_prescreen(task: dict, *, workdir: str = ".", **resolve_kwargs: Any) -> PrescreenResult:
    """Resolve the task's criteria against the repo (one Jev call).

    Runs the JEVRES-001 pipeline (:func:`engine.resolution.resolve` — trace →
    assemble → budget → one decisions call → bands) over the criteria-as-
    question; every keyword argument (``keys``, ``poster``, ``runner``, ...)
    forwards to it untouched, which is what keeps the hermetic tests hermetic.
    An ABSTAIN — including the trivial ``no-criteria`` one — returns an
    abstained result carrying the engine's named reason; it never raises and
    never returns a half-populated real result.
    """
    criteria = [c for c in (task.get("criteria") or []) if isinstance(c, str) and c.strip()]
    if not criteria:
        return PrescreenResult(abstained=True, abstain_reason="no-criteria")

    question = build_prescreen_question(criteria)
    verdict = resolve(question, workdir=workdir, **resolve_kwargs)
    if verdict.verdict == VERDICT_ABSTAIN or not verdict.ok:
        return PrescreenResult(
            abstained=True,
            abstain_reason=verdict.abstain_reason or "transport-error",
            abstain_detail=verdict.abstain_detail,
        )
    return PrescreenResult.from_verdict(verdict, criteria)


def assemble_prescreen_task(prescreen: PrescreenResult) -> str:
    """The prompt block the judge receives (GOAL A).

    Input only: the block names the judge's authority explicitly, because a
    confident-looking number in the prompt is exactly the shape of a signal
    that could quietly become a verdict.
    """
    lines = [
        "## RESOLUTION PRE-SCREEN (tier 1.5)",
        "",
        (
            "Before this evaluation, a separate Jev resolution pass scored how well "
            "the repository's own code resolves each criterion. These numbers are "
            "INPUT ONLY — they are not a verdict, they do not replace your "
            "judgement, and they never let you skip checking a criterion."
        ),
        "",
        f"- Overall resolution probability: {prescreen.probability:.2f} ({prescreen.band})",
        f"- Missing evidence: {prescreen.missing_kind} - {prescreen.missing_kind_label}",
        f"- Evidence quality: {prescreen.evidence_quality}",
    ]
    if prescreen.bundle_disclosure:
        lines.append(f"- Evidence window: {prescreen.bundle_disclosure}")
    lines += [
        "",
        "Per-criterion pre-screen:",
        "",
        "| # | Probability | Missing | Evidence | Cited path |",
        "|---|---|---|---|---|",
    ]
    for index, row in enumerate(prescreen.criteria, 1):
        probability = f"{row.probability:.2f}" if row.probability is not None else "—"
        cited = row.cited_path or "—"
        lines.append(
            f"| {index} | {probability} | {row.missing_kind} | {row.evidence_quality} | {cited} |"
        )
    lines += [
        "",
        (
            "Treat low-probability criteria as leads for where the defect likely is; "
            "treat high-probability criteria as still requiring your own verification. "
            "If you find evidence that contradicts the pre-screen, your finding wins "
            "and the pre-screen did not."
        ),
    ]
    return "\n".join(lines)


def attribute_items(items: list, prescreen: PrescreenResult) -> list:
    """GOAL B: copy each verdict item with its probability and cited path.

    1:1 by position (both lists are criteria-ordered by construction). Items
    beyond the pre-screened criteria pass through untouched, so a judge that
    reports extra or merged criteria is never mangled. A PASS item's detail
    gains the ``[resolution p; path]`` citation so the flat text reads
    "PASS … [resolution 0.91; engine/x.py:12]"; FAIL details are left exactly
    as the judge wrote them.
    """
    from engine.evaluator import VerdictItem

    out: list[VerdictItem] = []
    for index, item in enumerate(items):
        if index >= len(prescreen.criteria):
            out.append(item)
            continue
        row = prescreen.criteria[index]
        detail = item.detail
        if item.status == "PASS" and detail and row.cited_path:
            detail = f"{detail} [resolution {row.probability:.2f}; {row.cited_path}]"
        out.append(
            VerdictItem(
                criterion=item.criterion,
                status=item.status,
                detail=detail,
                resolution_probability=row.probability,
                cited_path=row.cited_path or None,
            )
        )
    return out


def attach_prescreen(verdict, prescreen: PrescreenResult | None):
    """Return *verdict* with attribution attached, or *verdict* unchanged.

    No pre-screen (ABSTAIN, disabled, or never run) returns the verdict object
    untouched — byte-identical to today's path. With one, the items are
    re-built with ``resolution_probability``/``cited_path`` and the full
    pre-screen dict rides on ``Verdict.prescreen`` for persistence.
    """
    from engine.evaluator import Verdict

    if prescreen is None or not verdict.items:
        return verdict
    return Verdict(
        verdict=verdict.verdict,
        items=attribute_items(verdict.items, prescreen),
        summary=verdict.summary,
        prescreen=prescreen.to_dict(),
    )
