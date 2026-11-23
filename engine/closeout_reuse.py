"""Closeout verdict reuse (CH-MAINT-016).

`gitreins task complete` re-judges the delivered tree with a FULL tier2 run
under the same ``evaluator.max_time`` wall the plain `gitreins judge` path
uses. Measured 2026-11-22 on chimera-v2 (verdicts fa427f37 / 84f17bf2 /
74152442 / 3ad6e7b1, .gitreins/history/2026-11-22/): the plain judge graded
the same criteria on the same tree as PASS/COMPLETE in 10-18m, while the
closeout re-grade that followed minutes later burned the entire 20m of
tier2 time cap — 2,104,056 / 1,381,994 input tokens across 77.0 / 63.2
iterations — and persisted an INCOMPLETE 0-graded verdict twice out of two
runs. The token burn (~0.05-0.11M tok/min) is 10-50x under the fleet's
quiet-lane norm (off-by-one answer class 2798: a tier2 time-cap INCOMPLETE
whose burn is far under norm is LANE WAIT, not work), so a bare
``max_time`` raise is the last choice and the two-starvations rule does not
mandate one.

PRIMARY MECHANISM — reuse the verdict the store already persisted: when the
tree a closeout would re-grade is byte-identical (HEAD commit + working-tree
content fingerprint, `engine/fingerprint.py`) to a tree a COMPLETE, fully
graded verdict from the SAME task id already judged, the closeout adopts
that verdict instead of re-grading it. Adoption identity is strict on
purpose — head commit, workspace-CONTENT fingerprint (so an edited
``.gitreins/config.yaml`` or ``.gitleaks.toml`` also breaks identity),
criteria list, and the grading lane's model — and never adopts a partially
graded record (any UNVERIFIED item, e.g. the "Not examined — evaluation
ended before this criterion was reached" rows a time cap leaves behind).

The CH-MAINT-013 cap contract is untouched: ``max_time`` still binds a real
tier2 run and overruns still cap loudly with a persisted partial verdict.

Alternatives considered (not implemented):
  (a) a closeout-specific ceiling separate from ``evaluator.max_time`` —
      rejected as a budget raise in disguise: on a lane 10-50x under norm no
      principled ceiling exists, and 2 of 2 measured closeouts starved the
      SAME wall the plain judge had just passed under minutes earlier;
  (c) making ``EvalCap`` distinguish lane-wait from work — a token-rate
      probe can classify the run only after the wall has already burned;
      reuse avoids paying the wall at all.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass, field
from typing import Any

from engine.evaluator import Verdict, VerdictItem
from engine.gate import PASS_CLASS_DEGRADED, pass_class
from engine.judge import JudgeResult

logger = logging.getLogger("gitreins.closeout_reuse")

#: Item statuses a stored verdict may carry for its grade to be COMPLETE.
#: UNVERIFIED is deliberately excluded — it is the status a time cap leaves
#: on criteria the evaluator never reached, and adopting such a record would
#: launder a partial grade into a closeout.
ADOPTABLE_ITEM_STATUSES = frozenset({"PASS", "FAIL", "NOT_VERIFIABLE"})

#: How many stored verdicts per task id are inspected for a reusable match,
#: newest first (``VerdictPersister.list_verdicts`` ordering).
REUSE_LOOK_BACK = 25


@dataclass(frozen=True)
class ReusableVerdict:
    """A stored verdict a closeout adopted, with the identity it matched."""

    #: The ``<date>/<hash>`` history entry the verdict was read from.
    entry: str
    #: The stored record (with ``_date`` / ``_hash`` reader markers).
    verdict: dict[str, Any] = field(default_factory=dict)
    #: ISO stamp the original record carried, echoed in the adoption record.
    evaluated_at: str = ""


def _head_commit(workdir: str) -> str:
    """The full HEAD sha of ``workdir``, or ``""`` when it has none."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD"],  # noqa: S607 - git resolved via PATH by design
            capture_output=True,
            text=True,
            cwd=workdir,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def _item_status(item: dict[str, Any]) -> str:
    value = item.get("status")
    return str(value).upper() if isinstance(value, str) else ""


def _fully_graded(candidate: dict[str, Any]) -> bool:
    """True when every item of the stored verdict carries a real grade.

    The time-cap partial verdicts this check keeps out carry items with
    status UNVERIFIED (and a non-empty ``ungraded_criteria`` list via
    GR-196 when a tier2 stage folded it) — exactly the records a closeout
    must NOT mirror.
    """
    items = candidate.get("items")
    if not isinstance(items, list) or not items:
        return False
    if any(_item_status(item) not in ADOPTABLE_ITEM_STATUSES for item in items):
        return False
    ungraded = candidate.get("ungraded_criteria")
    if isinstance(ungraded, list) and ungraded:
        return False
    return True


def _criteria_match(candidate: dict[str, Any], criteria: list[str]) -> bool:
    stored = candidate.get("task_criteria")
    return isinstance(stored, list) and list(stored) == list(criteria)


def _tree_match(workdir: str, candidate: dict[str, Any]) -> bool:
    """Same HEAD sha and the same observed workspace CONTENT bytes."""
    head = _head_commit(workdir)
    stored_commit = candidate.get("commit")
    if not isinstance(stored_commit, str) or not stored_commit or stored_commit != head:
        return False
    stored_fingerprint = candidate.get("working_tree_content_fingerprint")
    if not isinstance(stored_fingerprint, str) or not stored_fingerprint:
        return False
    from engine.fingerprint import compute as compute_fingerprint

    fingerprint = compute_fingerprint(workdir)
    # A non-authoritative observation must never adopt: fail closed.
    if fingerprint.state != "authoritative":
        return False
    return fingerprint.digest == stored_fingerprint


def _lane_match(candidate: dict[str, Any], llm: Any) -> bool:
    """The stored verdict was graded by the lane the closeout would use.

    CH-MAINT-015 made lane identity explicit (the model named in the env is
    the model that must judge); adopting a verdict graded by a DIFFERENT
    model would silently violate that contract. Conservative: when either
    side cannot resolve a model name, no adoption.
    """
    if llm is None:
        return False
    current = str(getattr(llm, "model", "") or "").strip()
    if not current:
        return False
    stages = candidate.get("stages")
    tier2 = stages.get("tier2") if isinstance(stages, dict) else None
    if not isinstance(tier2, dict):
        return False
    stored = str(tier2.get("model") or "").strip()
    if not stored:
        return False
    return stored == current


def _not_superseded(candidate: dict[str, Any]) -> bool:
    superseded = candidate.get("superseded_by")
    return not (isinstance(superseded, str) and superseded)


def find_reusable_verdict(
    workdir: str,
    task: Any,
    llm: Any,
    *,
    look_back: int = REUSE_LOOK_BACK,
) -> ReusableVerdict | None:
    """The stored verdict a closeout may adopt instead of re-grading, or None.

    The candidate must be COMPLETE in every dimension the store can still
    falsify: same task id, same criteria list, not superseded, every item
    graded, same HEAD commit, byte-identical working tree, and graded by the
    lane the closeout is about to use. GR-240 (rework): a candidate whose
    second-pass review block classifies ``degraded`` (a reviewer-failure
    error with zero findings) is skipped like an ungraded one — it is not a
    trustworthy pass and must not be adopted as one. ``None`` always means
    "re-judge".
    """
    from engine.persist import VerdictPersister

    criteria = list(getattr(task, "criteria", None) or [])
    if not criteria:
        # A criterion-less task was never tier2-graded; there is nothing to
        # adopt and nothing a re-judge would wait on — keep the caller's flow.
        return None
    try:
        verdicts = VerdictPersister(workdir).list_verdicts(n=look_back, task_id=str(task.id))
    except Exception:
        logger.debug("closeout reuse: history listing failed", exc_info=True)
        return None
    for candidate in verdicts:
        if not _not_superseded(candidate):
            continue
        if not _fully_graded(candidate):
            continue
        # GR-240 (rework): a verdict whose second-pass review block carries a
        # reviewer-failure error with zero findings classifies ``degraded``
        # (engine.gate.pass_class) — it is not a trustworthy pass, so it is
        # skipped like an ungraded candidate instead of being adopted as one.
        if pass_class(candidate) == PASS_CLASS_DEGRADED:
            logger.info(
                "closeout reuse: skipping degraded verdict %s/%s for %s "
                "(review block shows a reviewer-failure error with zero findings)",
                candidate.get("_date", "?"),
                candidate.get("_hash", "?"),
                task.id,
            )
            continue
        if not _criteria_match(candidate, criteria):
            continue
        if not _tree_match(workdir, candidate):
            continue
        if not _lane_match(candidate, llm):
            continue
        entry = f"{candidate.get('_date', '?')}/{candidate.get('_hash', '?')}"
        logger.info("closeout reuse: adopting verdict %s for %s", entry, task.id)
        return ReusableVerdict(
            entry=entry,
            verdict=candidate,
            evaluated_at=str(candidate.get("evaluated_at", "")),
        )
    return None


def build_adopted_result(reusable: ReusableVerdict, task_id: str) -> JudgeResult:
    """A JudgeResult mirroring the stored verdict a closeout adopted.

    The verdict object carries the stored items (all graded — see
    ``_fully_graded``) so ``engine.gate.pass_class`` classifies the adoption
    record ``judged`` and ``build_verdict_data`` re-persists the same grade
    under a fresh evaluated_at; the caller stamps ``reused_from`` beside it
    so the pair of records names the original.

    GR-240 (rework): the stored second-pass review block is copied onto the
    adopted result's ``second_pass_review`` (from the candidate's ``review``
    key) so the adoption record means the same thing as the original run —
    in particular, a stored verdict whose review block carries a
    reviewer-failure error with zero findings classifies ``degraded`` (never
    ``judged``) even if it was adopted by a caller that missed the reuse
    skip. A candidate without a stored review block adopts exactly as
    before: ``second_pass_review`` stays ``None``.
    """
    candidate = reusable.verdict
    stored_items = [item for item in candidate.get("items", []) if isinstance(item, dict)]
    items = [
        VerdictItem(
            criterion=str(item.get("criterion", "")),
            status=_item_status(item) or "PASS",
            detail=str(item.get("detail", "")),
            resolution_probability=item.get("resolution_probability"),
            cited_path=item.get("cited_path"),
            derived=bool(item.get("derived", False)),
            not_verifiable_reason=item.get("not_verifiable_reason"),
        )
        for item in stored_items
    ]
    stages = candidate.get("stages")
    pipeline_result = {
        "passed": bool(candidate.get("passed", False)),
        "stages": dict(stages) if isinstance(stages, dict) else {},
    }
    verdict_obj = Verdict(
        verdict="COMPLETE",
        items=items,
        summary=str(candidate.get("summary", "")),
    )
    provenance = candidate.get("provenance")
    result = JudgeResult(
        task_id=task_id,
        passed=bool(candidate.get("passed", False)),
        pipeline_result=pipeline_result,
        verdict=verdict_obj,
        tier2=verdict_obj,
        provenance=dict(provenance) if isinstance(provenance, dict) else {},
    )
    # GR-240 (rework): the stored review block rides the adoption record so
    # pass_class sees the same second-pass evidence the original verdict
    # carried — a reviewed-by-error candidate that slips past the reuse skip
    # still classifies ``degraded``, never a fresh judged pass.
    stored_review = candidate.get("review")
    if isinstance(stored_review, dict):
        result.second_pass_review = dict(stored_review)
    # JudgeResult.summary is a derived read-only property rendered from
    # pipeline_result's stages — the stored stages ARE the honest mirror of
    # what was graded, so the adoption record prints like the original run
    # did; the ``reused_from`` extra stamp on the persisted record is the
    # durable provenance marker, NOT a rewritten summary.
    return result
