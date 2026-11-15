"""Persistent second-pass finding state (GR-211).

The verdict history is the source of truth for review state.  This module only
reconciles the current structured review with the prior persisted review; it
does not invent findings when the prior record cannot be trusted.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from typing import Any

from engine.fingerprint import compute as compute_fingerprint

ACTIVE = "ACTIVE"
RESOLVED = "RESOLVED"
DISMISSED = "DISMISSED"
_VALID_STATES = frozenset({ACTIVE, RESOLVED, DISMISSED})
_FINGERPRINT = re.compile(r"^[0-9a-f]{64}$")


def _normalized(value: Any) -> str:
    text = unicodedata.normalize("NFC", str(value or ""))
    return " ".join(text.split()).casefold()


def finding_fingerprint(finding: dict[str, Any]) -> str:
    """Return the stable identity of a finding's location and normalized body."""
    identity = {
        "file": _normalized(finding.get("file")).replace("\\", "/"),
        "line": int(finding.get("line", 0)),
        "header": _normalized(finding.get("detection")),
        "body": _normalized(finding.get("detail")),
    }
    payload = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _finding_with_state(finding: dict[str, Any], state: str, fingerprint: str) -> dict[str, Any]:
    result = dict(finding)
    result["fingerprint"] = fingerprint
    result["state"] = state
    return result


def _validated_state_block(block: Any) -> list[dict[str, Any]] | None:
    """Validate a persisted block, returning entries or None (fail closed)."""
    if not isinstance(block, dict) or block.get("version") != 1:
        return None
    raw = block.get("findings")
    if not isinstance(raw, list):
        return None
    entries: list[dict[str, Any]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            return None
        fingerprint = entry.get("fingerprint")
        state = entry.get("state")
        finding = entry.get("finding")
        if not isinstance(fingerprint, str) or not _FINGERPRINT.fullmatch(fingerprint):
            return None
        if state not in _VALID_STATES or not isinstance(finding, dict):
            return None
        try:
            expected = finding_fingerprint(finding)
        except (TypeError, ValueError, OverflowError):
            return None
        if expected != fingerprint:
            return None
        entries.append({"fingerprint": fingerprint, "state": state, "finding": dict(finding)})
    return entries


def _prior_review_entries(review: Any, tree_fingerprint: str) -> tuple[str, list[dict[str, Any]]] | None:
    """Read a prior review, accepting legacy blocks but rejecting malformed state."""
    if not isinstance(review, dict):
        return None
    block = review.get("finding_state")
    if block is not None:
        entries = _validated_state_block(block)
        if entries is None or not isinstance(block.get("tree_fingerprint"), str):
            return None
        return block["tree_fingerprint"], entries

    # GR-171 records predate GR-211. They are safe to read as active findings,
    # but only when the verdict has an authoritative tree fingerprint.
    raw_findings = review.get("findings")
    if not isinstance(raw_findings, list):
        return None
    entries = []
    for finding in raw_findings:
        if not isinstance(finding, dict):
            return None
        try:
            fingerprint = finding_fingerprint(finding)
        except (TypeError, ValueError, OverflowError):
            return None
        entries.append({"fingerprint": fingerprint, "state": ACTIVE, "finding": dict(finding)})
    return tree_fingerprint, entries


def _empty_state(tree_fingerprint: str) -> dict[str, Any]:
    return {
        "version": 1,
        "tree_fingerprint": tree_fingerprint,
        "findings": [],
        "summary": {"carried": 0, "new": 0, "resolved": 0, "dismissed": 0},
    }


def reconcile_review(workdir: str, review: dict[str, Any], prior: dict[str, Any] | None) -> dict[str, Any]:
    """Apply persistent finding state to one review block.

    A missing/unavailable or malformed prior record is deliberately treated as
    no prior state. The current review remains visible, but no finding is
    fabricated as carried. The caller supplies the prior verdict read from the
    existing history surface.
    """
    current_fp = compute_fingerprint(workdir)
    if current_fp.state != "authoritative" or not current_fp.digest:
        return review

    tree_fingerprint = current_fp.digest
    current_raw = review.get("findings")
    if not isinstance(current_raw, list):
        return review
    current: list[dict[str, Any]] = [item for item in current_raw if isinstance(item, dict)]
    prior_data = None
    if isinstance(prior, dict):
        prior_data = _prior_review_entries(prior.get("review"), str(prior.get("working_tree_content_fingerprint", "")))
    state = _empty_state(tree_fingerprint)
    state_entries: list[dict[str, Any]] = []
    published: list[dict[str, Any]] = []

    if prior_data is not None:
        prior_tree, entries = prior_data
        unchanged = bool(prior_tree) and prior_tree == tree_fingerprint
        by_fp = {entry["fingerprint"]: entry for entry in entries}
        current_fps = set()
        for item in current:
            fp = finding_fingerprint(item)
            current_fps.add(fp)
            if unchanged and fp in by_fp and by_fp[fp]["state"] == DISMISSED:
                continue
            if unchanged and fp in by_fp and by_fp[fp]["state"] == ACTIVE:
                continue
            published_item = _finding_with_state(item, ACTIVE, fp)
            published.append(published_item)
            state_entries.append({"fingerprint": fp, "state": ACTIVE, "finding": published_item})
            state["summary"]["new"] += 1

        for entry in entries:
            fp = entry["fingerprint"]
            if entry["state"] == DISMISSED:
                state_entries.append(entry)
                if unchanged:
                    state["summary"]["dismissed"] += 1
                continue
            if unchanged and entry["state"] == ACTIVE:
                carried = _finding_with_state(entry["finding"], ACTIVE, fp)
                published.append(carried)
                state_entries.append({"fingerprint": fp, "state": ACTIVE, "finding": carried})
                state["summary"]["carried"] += 1
            elif entry["state"] == ACTIVE and fp not in current_fps:
                state_entries.append({**entry, "state": RESOLVED})
                state["summary"]["resolved"] += 1
    else:
        for item in current:
            fp = finding_fingerprint(item)
            published_item = _finding_with_state(item, ACTIVE, fp)
            published.append(published_item)
            state_entries.append({"fingerprint": fp, "state": ACTIVE, "finding": published_item})
            state["summary"]["new"] += 1

    state["findings"] = state_entries
    result = dict(review)
    result["findings"] = published
    result["finding_state"] = state
    return result
