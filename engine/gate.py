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
#: GR-231: the record CLAIMS a judged pass but carries no Tier 2 artifact. Not a
#: weaker pass — not a pass at all. Downstream consumers route it to review.
PASS_CLASS_UNVERIFIED = "unverified-never-pass"

#: GR-240: Tier 2 graded the record, but the second-pass reviewer never
#: reported — the review block carries a reviewer-failure error (anything
#: except the benign "no diff to review" case) and zero findings, so the
#: 8-of-27 history shape "unparseable reviewer reply + findings=[] + passed"
#: cannot read as a judged pass. GR-232 adds the sibling cause to the same
#: class: a graded gate (Tier 1 under allow_skips) did no work — the stage
#: dict carries the structured ``degraded``/``skipped_steps`` marker — so the
#: record is a PARTIAL run, not a judged pass. Not a pass at all: the merge
#: gate refuses it, and the merge recommendation degrades to ``needs_review``.
PASS_CLASS_DEGRADED = "degraded"

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
        # The third documented shape: a pre-pipeline verdict whose only
        # surviving Tier 2 artifact is the rendered criterion list.  Read it —
        # but only when it actually graded something, so a record that merely
        # CLAIMS ``judged`` with an empty item list is still no artifact.
        items = _field(candidate, "items", None)
        if isinstance(items, (list, tuple)) and len(items) > 0:
            return True
    return False


def has_tier2_artifact(source: object) -> bool:
    """True when *source* carries structured Tier 2 evidence.

    GR-231: ``pass_class`` is persisted input, not proof — an author or a stale
    writer can record ``judged`` on a record whose Tier 2 never ran.  The class
    is only honoured when the artifact that class implies is actually present.
    """
    return _tier2_evidence(source)


#: The benign review error that is NOT a reviewer failure: there was nothing
#: to review, so the reviewer's silence is honest, not broken (GR-240).
_NO_DIFF_ERROR_PREFIX = "no diff to review"


def _review_block(source: object) -> Mapping[str, Any] | None:
    """The second-pass review block, in either live or persisted shape.

    GR-240: a live ``JudgeResult`` carries the block as its
    ``second_pass_review`` attribute; a persisted verdict dict carries the
    same block under the ``review`` key.
    """
    block = _field(source, "second_pass_review", None)
    if block is None:
        block = _field(source, "review", None)
    return block if isinstance(block, Mapping) else None


def _reviewer_never_reported_block(block: Any) -> bool:
    """The GR-240 test over one review BLOCK (the dict itself).

    Reviewer-failure: the block's ``error`` is set and is NOT the benign
    ``"no diff to review"`` case; zero findings: the block's ``findings`` list
    is empty (a block without the key counts as empty too). A record in this
    state was never actually reviewed — "the reviewer found nothing" and "the
    reviewer never reported" must not be confusable — so it must not read as a
    judged pass even when the evaluator's own verdict is COMPLETE.
    """
    if not isinstance(block, Mapping):
        return False
    error = block.get("error")
    if error is None or str(error).startswith(_NO_DIFF_ERROR_PREFIX):
        return False
    findings = block.get("findings")
    if isinstance(findings, (list, tuple)) and findings:
        return False
    return True


def _reviewer_never_reported(source: object) -> bool:
    """True when *source*'s review block shows the reviewer failed and reported nothing."""
    return _reviewer_never_reported_block(_review_block(source))


def _graded_stage_skipped(source: object) -> list[str]:
    """``stage(gates)`` for every stage whose graded work was skipped (GR-232).

    Reads ONLY the structured degradation markers — never the summary text —
    in both shapes:

    * a live ``JudgeResult`` carries ``pipeline_result["stages"]`` (pipeline
      path) or a :class:`Tier1Result` on ``tier1`` whose ``degraded`` property
      already applies the TRUST-001 substantive-gate semantics (config-disabled
      guards are not degradations, a language-appropriate Go replacement is
      not either);
    * a persisted ``verdict.json`` dict carries the same stage dicts under
      ``stages`` (``degraded`` / ``skipped_steps``).
    """
    skipped: list[str] = []
    tier1 = _field(source, "tier1", None)
    if tier1 is not None and getattr(tier1, "degraded", False):
        for step in getattr(tier1, "degraded_steps", None) or []:
            skipped.append(str(step.get("step", "?")))
    for holder in (source, _field(source, "pipeline_result", None)):
        if holder is None:
            continue
        stages = _field(holder, "stages", None)
        if not isinstance(stages, Mapping):
            continue
        for stage_id, stage in stages.items():
            if not isinstance(stage, Mapping) or not stage.get("degraded"):
                continue
            gates = ",".join(str(step) for step in stage.get("skipped_steps") or [])
            skipped.append(f"{stage_id}({gates or 'unknown checks'})")
    return skipped


def pass_class(source: object) -> str:
    """``judged`` / ``tier1-only`` / ``unverified-never-pass`` / ``degraded``.

    GR-166: an explicit ``pass_class`` wins; a record written before the field
    existed is classified from its stages — ``judged`` when Tier 2 evidence is
    present, ``tier1-only`` when it is not.

    GR-231: a record that CLAIMS ``judged`` while carrying no Tier 2 artifact is
    classified ``unverified-never-pass`` instead. The 2026-11-15 slip-through
    spot-check (4,104 PASS verdicts scanned, 18 confirmed contradictions) found
    8 of the 18 were records with no Tier 2 artifact behind them and 4 were
    commit drift — and treating the class claim as proof made forging it the
    cheapest way through the gate. This class is never a PASS: consumers route
    it to review, and no owner waiver applies to it (a waiver waives a Tier 2
    that openly did not run, not a claim that it did).

    GR-240: a record whose second-pass review block carries a reviewer-failure
    error with zero findings is classified ``degraded`` whenever it would
    otherwise read ``judged`` — a class claim (explicit or artifact-derived)
    is not proof the reviewer reported, and 8 of 27 poisoned review blocks in
    history read as judged passes. GR-231 keeps precedence for records with no
    Tier 2 artifact: those stay ``unverified-never-pass`` (the claim is still
    unsupported, whatever the reviewer did), and explicit ``tier1-only`` /
    ``unverified-never-pass`` claims are never upgraded by this check.

    GR-232: a record whose graded stage skipped checks (an allow_skips Tier 1
    — the structured ``degraded``/``skipped_steps`` stage marker, never the
    summary text) also classifies ``degraded`` whenever it would otherwise
    read ``judged`` — a partial run is not a judged pass, whatever the Tier 2
    artifacts say. Same precedence rules as GR-240.
    """
    explicit = _field(source, "pass_class", None)
    if explicit == PASS_CLASS_JUDGED:
        if not has_tier2_artifact(source):
            return PASS_CLASS_UNVERIFIED
        if _reviewer_never_reported(source) or _graded_stage_skipped(source):
            return PASS_CLASS_DEGRADED
        return PASS_CLASS_JUDGED
    if explicit in (PASS_CLASS_TIER1_ONLY, PASS_CLASS_UNVERIFIED):
        return str(explicit)
    if has_tier2_artifact(source):
        if _reviewer_never_reported(source) or _graded_stage_skipped(source):
            return PASS_CLASS_DEGRADED
        return PASS_CLASS_JUDGED
    return PASS_CLASS_TIER1_ONLY


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


def _nested_tier2_verdict(verdict: object) -> Mapping[str, Any] | None:
    """Find a persisted Tier 2 verdict when it is nested under ``stages``."""
    stages = _field(verdict, "stages", None)
    if not isinstance(stages, Mapping):
        return None
    for stage in stages.values():
        if not isinstance(stage, Mapping):
            continue
        for step in stage.get("steps") or ():
            if not isinstance(step, Mapping):
                continue
            data = step.get("data")
            if isinstance(data, Mapping) and isinstance(data.get("verdict"), str):
                return data
    return None


def verdict_outcome(verdict: object) -> str:
    """The verdict's own word: ``COMPLETE`` / ``INCOMPLETE`` / ``""``.

    Older persisted pipeline records store the evaluator verdict under the
    Tier 2 step rather than duplicating it at the top level.  Read that shape
    too, so coverage does not turn an otherwise complete persisted verdict into
    a false red.
    """
    outcome = str(_field(verdict, "verdict", "") or "").strip().upper()
    if outcome:
        return outcome
    nested = _nested_tier2_verdict(verdict)
    return str(nested.get("verdict", "") if nested else "").strip().upper()


def verdict_items(verdict: object) -> list[Any]:
    """The verdict's per-criterion items, whatever shape it is stored in."""
    items = _field(verdict, "items", None)
    if isinstance(items, (list, tuple)):
        return list(items)
    nested = _nested_tier2_verdict(verdict)
    nested_items = nested.get("items") if nested else None
    return list(nested_items) if isinstance(nested_items, (list, tuple)) else []


def _norm(text: object) -> str:
    return " ".join(str(text or "").split()).casefold()


def _matches(wanted: str, graded: str) -> bool:
    """Match an evaluator echo without weakening coverage to fuzzy similarity.

    Exact text remains the first choice.  Evaluator output can, however, replace
    a long interior token with ``...`` when echoing a criterion.  Treat that
    marker as a single elided span only when both sides retain substantial
    context that occurs in the authored criterion.  A short prefix or an
    unrelated paraphrase therefore remains ungraded.
    """
    if wanted == graded:
        return True
    if len(graded) < _PREFIX_MIN_CHARS or len(wanted) < _PREFIX_MIN_CHARS:
        return False
    # A bare prefix is a genuinely truncated echo, not evidence of identity.
    # Only an explicit ellipsis may stand for omitted interior text.
    if "..." not in graded:
        return False

    parts = [part for part in graded.split("...") if part]
    if len(parts) < 2 or len(parts[0]) < _PREFIX_MIN_CHARS or len(parts[-1]) < _PREFIX_MIN_CHARS:
        return False
    position = 0
    retained = 0
    for part in parts:
        found = wanted.find(part, position)
        if found < 0:
            return False
        position = found + len(part)
        retained += len(part)
    return retained >= 2 * _PREFIX_MIN_CHARS


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
    and every item is PASS. An INCOMPLETE label is accepted only when that
    complete item evidence contradicts the aggregate label. Empty item/criteria
    lists never pass, and the persisted ``passed`` value is never consulted.
    """
    outcome = verdict_outcome(verdict)
    if outcome not in ("COMPLETE", "INCOMPLETE"):
        return False
    items = verdict_items(verdict)
    if not items or any(_status_of(item) != "PASS" for item in items):
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


# ── GR-235: fix-verification cap ────────────────────────────────────────────

#: The stage id the fix-verification stage registers under (engine.pipeline).
FIX_VERIFICATION_STAGE_ID = "fix_verification"
#: The disposition values that leave a first-round finding open.
_UNHANDLED_DISPOSITIONS = ("not_fixed", "partially_fixed")
#: Severities whose open findings cap. ``""`` is a severity the finding text
#: did not name — treated as capping (fail-closed: an unclassified finding is
#: never provably low).
_CAPPING_SEVERITIES = ("critical", "high", "medium", "")


def fix_verification_unhandled(source: object) -> list[dict[str, Any]]:
    """The GR-235 fix-verification dispositions that cap *source* at needs_review.

    Reads ``stages.fix_verification.dispositions`` off a live pipeline result
    or a persisted verdict record (the shape ``StageResult.to_dict`` and the
    persister write). One definition, two callers: the advisory downgrade in
    :func:`merge_recommendation` and the merge-gate hold in
    ``WorktreeManager._refuse_unhandled_findings`` — imported, never copied.

    Returns ``[]`` for records without the stage (the historical shapes) and
    for every disposition that is ``fixed`` or ``superseded``; only open
    (``not_fixed`` / ``partially_fixed``) findings on a critical, high or
    medium severity are returned. An UNNAMED severity caps too (fail-closed);
    ``low``/``info`` never cap.
    """
    record_stages = _field(source, "stages", None)
    if not isinstance(record_stages, Mapping):
        return []
    stage = record_stages.get(FIX_VERIFICATION_STAGE_ID)
    if not isinstance(stage, Mapping):
        return []
    dispositions = stage.get("dispositions")
    if not isinstance(dispositions, list):
        return []
    unhandled: list[dict[str, Any]] = []
    for entry in dispositions:
        if not isinstance(entry, Mapping):
            continue
        if str(entry.get("disposition") or "") not in _UNHANDLED_DISPOSITIONS:
            continue
        severity = str(entry.get("severity") or "").strip().lower()
        if severity in _CAPPING_SEVERITIES:
            unhandled.append(dict(entry))
    return unhandled


def merge_recommendation(
    verdict: object, criteria: Any = None, stages: Any = None, review: Any = None
) -> str:
    """The three-valued merge recommendation for *verdict* (GR-178).

    ``changes_required`` when any item grades FAIL; ``needs_review`` when any
    item is NOT_VERIFIABLE (a criterion only a human can settle — the whole
    point of GR-178), when a supplied criterion went ungraded, when the
    record has no judged pass class at all (GR-231: ``tier1-only`` and
    ``unverified-never-pass`` both mean no Tier 2 artifact backs the record, so
    neither is ever ``no_concerns_found`` — an absent artifact cannot be routed
    to "no concerns"), when GR-240's review block shows the second-pass
    reviewer never reported (a degraded record — never "no concerns" over a
    review that never happened), or — GR-235 — when the fix-verification stage
    left a critical/high/medium finding open; ``no_concerns_found`` only when
    the record is a judged pass whose every item is PASS, whose every
    criterion was graded, and whose first-round findings were all handled. The
    merge gate does NOT read this: ``pass_class`` still governs merging, so
    this is routing advice for a human, never a key.

    ``stages`` may be supplied explicitly (persist.py passes the pipeline's
    stage dicts, which a live ``Verdict`` object does not carry); when absent
    the record's own ``stages`` field is read. ``review`` is the same deal for
    GR-240: persist.py passes the live result's second-pass review block,
    which a live ``Verdict`` object does not carry either; when absent the
    record's own block (``second_pass_review`` attribute or ``review`` key)
    is read.
    """
    if pass_class(verdict) != PASS_CLASS_JUDGED:
        return MERGE_RECOMMENDATION_REVIEW
    # GR-240: the review block may live outside *verdict* (the caller supplies
    # it for a live Verdict object); degrade the same way pass_class would.
    if _reviewer_never_reported_block(review if review is not None else _review_block(verdict)):
        return MERGE_RECOMMENDATION_REVIEW
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
    # GR-235: the fix-verification stage's open critical/high/medium findings
    # cap the recommendation at needs_review — never no_concerns_found while
    # flagged code survives the judged diff. The cap travels on the record's
    # ``stages.fix_verification`` dict (a live pipeline result or a persisted
    # verdict); a record without that stage is unaffected.
    record_stages = stages if stages is not None else _field(verdict, "stages", None)
    if isinstance(record_stages, Mapping) and fix_verification_unhandled({"stages": record_stages}):
        return MERGE_RECOMMENDATION_REVIEW
    return MERGE_RECOMMENDATION_OK
