"""The verdict gate — one authority for what a stored verdict MEANS.

Two questions live here, and nowhere else:

* **GR-167** — does a COMPLETE verdict carry per-criterion evidence? A
  verdict with no items, or one that does not grade every criterion, is not a
  pass. ``verdict_is_passing`` is the only passing test a caller may use.
* **GR-166** — did Tier 2 ever run? A ``--skip-tier2`` run is a Tier 1
  grade, not a judged pass. ``pass_class`` records which one a verdict is,
  and the merge-back refuses ``tier1-only`` unless the owner's waiver token
  is recorded in the verdict itself.

Reading is deliberately tolerant: the same functions accept the live
``Verdict`` / ``JudgeResult`` objects AND a persisted ``verdict.json`` dict,
and a verdict written before these fields existed is classified from its
stages instead of failing closed on a missing key (GR-166 AC1).
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
from dataclasses import dataclass
from typing import Any, Mapping

logger = logging.getLogger("gitreins.gate")

#: Tier 2 (the only stage that reads the code) produced a verdict.
PASS_CLASS_JUDGED = "judged"
#: Tier 2 never ran: the verdict grades Tier 1 alone.
PASS_CLASS_TIER1_ONLY = "tier1-only"

#: GR-178: the status for a criterion the judge could not assess from the code
#: at all — a UI actually rendering, an external API contract, a migration
#: against real data. A third claim distinct from PASS ("verified good"),
#: FAIL ("verified broken") and UNVERIFIED ("the budget stopped before this
#: was reached"): the judge LOOKED and code review alone cannot settle it.
#: The item names what would settle it, and the merge recommendation
#: degrades to ``needs_review`` while one exists. Fail-closed: it is never
#: counted as PASS by the coverage gate.
STATUS_NOT_VERIFIABLE = "NOT_VERIFIABLE"

#: A prefix match needs this much shared text to mean anything; shorter
#: criteria ("c1" vs "c10") match by exact text only.
_PREFIX_MIN_CHARS = 10

#: Env var carrying the owner's tier-1-only merge waiver token. Takes
#: precedence over ``gate.tier2_waiver`` in ``.gitreins/config.yaml``. Never
#: read from a commit message: the author controls that text.
TIER2_WAIVER_ENV = "GITREINS_TIER2_WAIVER"

#: Verdict key holding the digest of the owner's waiver token (see
#: :func:`waiver_digest`). A digest, not the token: verdicts are committed.
WAIVER_DIGEST_KEY = "tier2_waiver_digest"


def _field(obj: object, key: str, default: Any = None) -> Any:
    """Read *key* from a dict-shaped record or an attribute-bearing object."""
    if isinstance(obj, Mapping):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _tier2_evidence(source: object) -> bool:
    """True when *source* carries a Tier 2 verdict (live or persisted shape).

    Three shapes exist and all three are read here, because a gate that
    understands only one of them refuses the verdicts produced by the others:

    * live legacy result — ``JudgeResult.tier2`` is a :class:`Verdict`;
    * live/persisted pipeline result — a stage step's ``data["verdict"]``;
    * persisted legacy verdict — top-level ``items`` were written by a Tier 2
      run even though no stage dict was recorded.
    """
    for candidate in (source, _field(source, "pipeline_result", None)):
        if candidate is None:
            continue
        if _field(candidate, "tier2", None) is not None:
            return True
        stages = _field(candidate, "stages", None)
        if isinstance(stages, Mapping):
            for stage in stages.values():
                for step in _field(stage, "steps", None) or []:
                    data = _field(step, "data", None)
                    if isinstance(data, Mapping) and data.get("verdict"):
                        return True
    return False


def pass_class(source: object) -> str:
    """``judged`` or ``tier1-only`` for a verdict, a result, or a record.

    GR-166: an explicit ``pass_class`` wins; a record written before the field
    existed is classified from its stages — ``judged`` when Tier 2 evidence is
    present, ``tier1-only`` when it is not.
    """
    explicit = _field(source, "pass_class", None)
    if explicit in (PASS_CLASS_JUDGED, PASS_CLASS_TIER1_ONLY):
        return str(explicit)
    return PASS_CLASS_JUDGED if _tier2_evidence(source) else PASS_CLASS_TIER1_ONLY


def waiver_digest(token: str) -> str:
    """SHA-256 of the owner's waiver token.

    The verdict records this digest rather than the token: ``verdict.json`` is
    committed to the repository, so the token itself must never appear in it.
    The digest is only reproducible by someone who already holds the token.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def configured_waiver(workdir: str) -> str | None:
    """The owner's tier-1-only merge waiver token, or ``None``.

    Read from ``GITREINS_TIER2_WAIVER`` first, then ``gate.tier2_waiver`` in
    the repo's ``.gitreins/config.yaml``. Deliberately NOT read from the commit
    message, a trailer, or the verdict: all three are author-controlled, and
    the whole point of the waiver is that the author cannot mint it.
    """
    token = os.environ.get(TIER2_WAIVER_ENV, "").strip()
    if token:
        return token
    try:
        from engine.config import load_raw_config

        raw = load_raw_config(workdir)
    except Exception:  # a broken config must not turn the gate into a crash
        logger.debug("waiver lookup failed for %s", workdir, exc_info=True)
        return None
    section = raw.get("gate") if isinstance(raw, Mapping) else None
    value = section.get("tier2_waiver") if isinstance(section, Mapping) else None
    token = str(value).strip() if value is not None else ""
    return token or None


def recorded_waiver_digest(verdict: object) -> str | None:
    """The waiver digest a verdict carries, or ``None`` when it carries none."""
    value = _field(verdict, WAIVER_DIGEST_KEY, None)
    return value if isinstance(value, str) and value else None


def waiver_satisfied(verdict: object, workdir: str) -> bool:
    """True when *verdict* records the waiver the owner configured for *workdir*.

    Both halves are required: a configured token alone does not retroactively
    waive a verdict produced without it, and a recorded digest alone does not
    survive an owner who has since removed the token.
    """
    recorded = recorded_waiver_digest(verdict)
    token = configured_waiver(workdir)
    if not recorded or not token:
        return False
    return hmac.compare_digest(recorded, waiver_digest(token))


# ── GR-167: per-criterion coverage ──────────────────────────────────────────


def verdict_outcome(verdict: object) -> str:
    """The verdict's own word: ``COMPLETE`` / ``INCOMPLETE`` / ``""``."""
    return str(_field(verdict, "verdict", "") or "").strip().upper()


def verdict_items(verdict: object) -> list[Any]:
    """The verdict's per-criterion items, whatever shape it is stored in."""
    items = _field(verdict, "items", None)
    return list(items) if isinstance(items, (list, tuple)) else []


def _norm(text: object) -> str:
    return " ".join(str(text or "").split()).casefold()


def _matches(wanted: str, graded: str) -> bool:
    """Exact text first, then a long-enough prefix in either direction."""
    if wanted == graded:
        return True
    if len(graded) < _PREFIX_MIN_CHARS or len(wanted) < _PREFIX_MIN_CHARS:
        return False
    return graded.startswith(wanted) or wanted.startswith(graded)


@dataclass(frozen=True)
class Coverage:
    """Which task criteria a verdict actually graded, and how."""

    criteria: tuple[str, ...]
    graded: tuple[str, ...]
    ungraded: tuple[str, ...]
    failing: tuple[str, ...]
    #: GR-178: criteria whose item is NOT_VERIFIABLE — the judge could look
    #: at the code and still not settle them (a browser check, an external
    #: API, live data). Listed separately from ``failing`` because "cannot be
    #: verified from here" is not "verified broken", and routed to a human
    #: (merge recommendation ``needs_review``) instead of graded.
    not_verifiable: tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        """Every criterion graded, and every graded criterion PASSing.

        An empty ``criteria`` is NOT complete: a verdict that graded nothing
        cannot be evidence that everything is done (GR-167: an empty task
        criteria list must not silently pass). A NOT_VERIFIABLE item is also
        not complete (GR-178): a criterion only a human can settle is not a
        criterion the machine verified.
        """
        return (
            bool(self.criteria)
            and not self.ungraded
            and not self.failing
            and not self.not_verifiable
        )

    @property
    def reason(self) -> str:
        """One line naming what is missing, for the verdict summary."""
        if not self.criteria:
            return "no task criteria were supplied, so nothing was graded"
        parts: list[str] = []
        if self.ungraded:
            parts.append("ungraded: " + "; ".join(self.ungraded))
        if self.not_verifiable:
            parts.append("not verifiable — needs human review: " + "; ".join(self.not_verifiable))
        if self.failing:
            parts.append("not passing: " + "; ".join(self.failing))
        return ", ".join(parts)


def _criterion_of(item: object) -> str:
    return str(_field(item, "criterion", "") or "")


def _status_of(item: object) -> str:
    return str(_field(item, "status", "") or "").strip().upper()


def check_coverage(verdict: object, criteria: Any) -> Coverage:
    """Match *criteria* against the verdict's items, one item per criterion."""
    wanted = [str(c) for c in (criteria or []) if str(c).strip()]
    items = verdict_items(verdict)
    used: set[int] = set()
    graded: list[str] = []
    ungraded: list[str] = []
    failing: list[str] = []
    not_verifiable: list[str] = []

    for criterion in wanted:
        needle = _norm(criterion)
        match: int | None = None
        for index, item in enumerate(items):
            if index not in used and _norm(_criterion_of(item)) == needle:
                match = index
                break
        if match is None:
            for index, item in enumerate(items):
                if index not in used and _matches(needle, _norm(_criterion_of(item))):
                    match = index
                    break
        if match is None:
            ungraded.append(criterion)
            continue
        used.add(match)
        graded.append(criterion)
        status = _status_of(items[match])
        if status == STATUS_NOT_VERIFIABLE:
            # GR-178: "code review cannot settle this" is its own bucket —
            # routed to a human, never silently a PASS and never conflated
            # with a verified FAIL.
            not_verifiable.append(criterion)
        elif status != "PASS":
            failing.append(criterion)

    return Coverage(
        criteria=tuple(wanted),
        graded=tuple(graded),
        ungraded=tuple(ungraded),
        failing=tuple(failing),
        not_verifiable=tuple(not_verifiable),
    )


def verdict_is_passing(verdict: object, criteria: Any) -> bool:
    """The ONLY passing test a gate may use (GR-167).

    A COMPLETE verdict passes only when its items grade every task criterion
    and every graded criterion is PASS. An empty item list never passes, and an
    empty criteria list never passes either. The persisted ``passed`` value is
    not consulted: before GR-167 it was ``verdict == "COMPLETE"`` and nothing
    more, which is the hole this function closes.
    """
    if verdict_outcome(verdict) != "COMPLETE":
        return False
    if not verdict_items(verdict):
        return False
    return check_coverage(verdict, criteria).complete


def ungraded_note(coverage: Coverage) -> str:
    """The line appended to a verdict summary when coverage is short."""
    if coverage.complete:
        return ""
    return f"Coverage FAILED — {coverage.reason}"


# ── GR-178: merge recommendation ────────────────────────────────────────────

#: Merge recommendations, mirroring PR-Agent's three-valued schema
#: (no_concerns_found / needs_review / changes_required). Advisory only:
#: the merge gate keys on ``pass_class`` (GR-166), never on this value, so
#: the recommendation cannot become a new bypass.
MERGE_RECOMMENDATION_OK = "no_concerns_found"
MERGE_RECOMMENDATION_REVIEW = "needs_review"
MERGE_RECOMMENDATION_CHANGES = "changes_required"


def merge_recommendation(verdict: object, criteria: Any = None) -> str:
    """The three-valued merge recommendation for *verdict* (GR-178).

    ``changes_required`` when any item grades FAIL; ``needs_review`` when any
    item is NOT_VERIFIABLE (a criterion only a human can settle — the whole
    point of GR-178) or a supplied criterion went ungraded; ``no_concerns_found``
    only when every item is PASS and every criterion was graded. The merge
    gate does NOT read this: ``pass_class`` still governs merging, so this is
    routing advice for a human, never a key.
    """
    items = verdict_items(verdict)
    statuses = [_status_of(item) for item in items]
    if "FAIL" in statuses:
        return MERGE_RECOMMENDATION_CHANGES
    if STATUS_NOT_VERIFIABLE in statuses:
        return MERGE_RECOMMENDATION_REVIEW
    if criteria:
        coverage = check_coverage(verdict, criteria)
        if coverage.ungraded or coverage.not_verifiable:
            return MERGE_RECOMMENDATION_REVIEW
    return MERGE_RECOMMENDATION_OK
