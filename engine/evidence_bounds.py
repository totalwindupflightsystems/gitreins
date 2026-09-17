"""Line-bound evidence truncation shared by every surface that persists output.

DF-GITREINS-POC-8 introduced the head+tail bound for pipeline step evidence;
DF-GITREINS-POC-19 found the same defect still live in two sibling helpers
(``engine/worktree_fleet.py`` and ``engine/worktree_disposable.py``), which
bound a worktree/QA run's output with a head-only slice at a raw character
offset — so the evidence the QA ledger records ended in a half-written line
and dropped the tail, exactly where pytest's short test summary lives.

The implementation lives here so the three surfaces cannot drift apart again:
``engine.pipeline``, ``engine.worktree_fleet`` and ``engine.worktree_disposable``
all call :func:`_bound_step_evidence` with the same
:data:`MAX_EVIDENCE_CHARS` budget.
"""

from __future__ import annotations

import re

from engine.types import _FAILED_TEST_LINE

# One budget for every surface that persists command output. The old behavior
# (``output[:500]`` in ``StepResult.to_dict``, ``output[:MAX-40]`` in the
# worktree helpers) kept only the banner and threw away pytest's short test
# summary at the END of the output — the part that names the failing test.
MAX_EVIDENCE_CHARS = 4000
# Back-compat alias: engine/pipeline.py named the same budget this way, and
# tests import it from there.
MAX_STEP_EVIDENCE_CHARS = MAX_EVIDENCE_CHARS

# pytest short-summary ERROR lines ("ERROR tests/test_x.py::test_setup - ...")
# mirror engine.types._FAILED_TEST_LINE for collection/setup errors.
_ERROR_TEST_LINE = re.compile(r"^ERROR \S+::")

_MAX_HOISTED_LINES = 20
# Every hoisted FAILED/ERROR line is itself bounded: the marker reports on a
# budget of *cap* chars and must not spend the budget it is describing.
_MAX_HOISTED_CHARS = 1000


def _head_end_for_budget(output: str, budget: int) -> int:
    """Index of the largest prefix of COMPLETE lines that fits in *budget*.

    ``0`` means no complete line fits (a single line longer than the budget).
    """
    end = 0
    for line in output.splitlines(keepends=True):
        if end + len(line) > budget:
            break
        end += len(line)
    return end


def _tail_start_for_budget(output: str, budget: int) -> int:
    """Index of the largest suffix of COMPLETE lines that fits in *budget*.

    ``len(output)`` means no complete line fits.
    """
    start = len(output)
    used = 0
    for line in reversed(output.splitlines(keepends=True)):
        if used + len(line) > budget:
            break
        used += len(line)
        start -= len(line)
    return start


def _hoist_summary_lines(omitted: str, budget: int = _MAX_HOISTED_CHARS) -> tuple[list[str], int]:
    """FAILED/ERROR short-summary lines from *omitted*, deduped, order kept.

    Returns ``(lines, dropped)`` where *dropped* counts the matching lines the
    count/char budget could not carry (reported in the marker, never silently
    swallowed). *budget* is the room the marker has left for id lines — a small
    cap carries no ids but still counts them.
    """
    hoisted: list[str] = []
    seen: set[str] = set()
    used = 0
    dropped = 0
    for line in omitted.split("\n"):
        stripped = line.strip()
        if not (_FAILED_TEST_LINE.match(stripped) or _ERROR_TEST_LINE.match(stripped)):
            continue
        if stripped in seen:
            continue
        seen.add(stripped)
        if len(hoisted) >= _MAX_HOISTED_LINES or used + len(stripped) > budget:
            dropped += 1
            continue
        hoisted.append(stripped)
        used += len(stripped)
    return hoisted, dropped


def _omission_marker(
    omitted: str,
    *,
    partial_line_cut: bool,
    hoisted: list[str],
    dropped_hoisted: int = 0,
) -> str:
    """The omission marker: how much went, on which lines, plus hoisted ids."""
    detail = f"{len(omitted)} chars omitted — {len(omitted.splitlines())} line(s)"
    if partial_line_cut:
        detail += "; one over-budget line was cut mid-line"
    text = f"\n… [{detail}] …\n"
    if hoisted:
        text += "\n".join(hoisted) + "\n…\n"
    if dropped_hoisted:
        text += f"… [{dropped_hoisted} further FAILED/ERROR line(s) not hoisted] …\n"
    return text


def _marker_for(omitted: str, *, partial_line_cut: bool, budget: int) -> str:
    """The marker for *omitted*, fitted into *budget* chars.

    The base line (char/line counts) is always reported; the hoisted FAILED/
    ERROR ids and their drop count only spend what is left of *budget* after
    it, so a small cap reports the tally and the number of ids it could not
    carry instead of overshooting the cap it is describing.
    """
    base = _omission_marker(
        omitted, partial_line_cut=partial_line_cut, hoisted=[], dropped_hoisted=0
    )
    hoist_budget = min(_MAX_HOISTED_CHARS, max(0, budget - len(base) - 40))
    hoisted, dropped = _hoist_summary_lines(omitted, hoist_budget)
    marker = _omission_marker(
        omitted,
        partial_line_cut=partial_line_cut,
        hoisted=hoisted,
        dropped_hoisted=dropped,
    )
    if len(marker) <= budget:
        return marker
    # Not even the tally plus the drop note fits: report the tally alone.
    return base


def _bound_step_evidence(output: str, cap: int = MAX_EVIDENCE_CHARS) -> str:
    """Bound step evidence to *cap* chars on LINE boundaries, keeping BOTH ends.

    Output at or under the cap is returned byte-identical. Longer output is
    kept as head (~60% of the budget) + an omission marker + tail (~40%) —
    the tail carries pytest's short test summary, so it is never dropped.

    DF-GITREINS-POC-5: the head and tail are filled with COMPLETE lines, so a
    reader never meets a half-written line (`tests/test_mod.py::test_case_50
    PASSED [` and a fragment of its percentage) at either cut, and the marker
    names how many chars and lines went plus how many FAILED/ERROR short-
    summary lines were hoisted out of the middle. The one exception is a
    single line longer than its side's budget (a minified JSON blob, one
    enormous traceback line): that line IS cut mid-line and the marker says
    so. The cap is a real bound — the marker is charged against it, not added
    on top. Any FAILED/ERROR short-summary line inside the omitted middle is
    hoisted into the marker region (deduped, order preserved) so a failing
    test id survives even when the suite was interrupted mid-run and the tail
    holds no summary.

    DF-GITREINS-POC-19: ``engine.worktree_fleet`` and
    ``engine.worktree_disposable`` bound the output they record for a
    ``worktree fresh|repro|dogfood`` run with this same function, so QA
    evidence obeys the identical rules instead of a head-only slice.
    """
    if len(output) <= cap:
        return output

    # The marker is part of the budget, not an addition to it: keep room for it
    # and split the rest 60/40 between the head and the tail.
    marker_reserve = min(120, cap // 4)
    usable = max(0, cap - marker_reserve)
    head_budget = (usable * 6) // 10
    tail_budget = usable - head_budget

    head_end = _head_end_for_budget(output, head_budget)
    partial_line_cut = head_end == 0
    if partial_line_cut:
        head_end = head_budget

    tail_start = _tail_start_for_budget(output, tail_budget)
    if tail_start >= len(output):
        # No complete line fits in the tail budget either (the payload's last
        # line alone is longer than 40% of the cap) — cut mid-line and flag
        # it; a suffix is kept, so the summary line still survives.
        partial_line_cut = True
        tail_start = len(output) - tail_budget
    tail_start = max(tail_start, head_end)

    head = output[:head_end]
    tail = output[tail_start:]
    marker = ""
    # The marker is charged against the cap, so shrink the evidence until
    # head + marker + tail fits. Whole lines go first; a side that is one long
    # line is char-cut (still a prefix/suffix) and the marker says so.
    for _ in range(12):
        omitted = output[len(head) : len(output) - len(tail)]
        marker = _marker_for(
            omitted,
            partial_line_cut=partial_line_cut,
            budget=max(0, cap - len(head) - len(tail)),
        )
        over = len(head) + len(marker) + len(tail) - cap
        if over <= 0:
            break
        # Drop whole lines from the tail's head until at least *over* chars are
        # gone (a side that is one long line is char-cut instead — still a
        # suffix/prefix — and the marker is told).
        if tail:
            drop_len = 0
            while drop_len < over:
                newline = tail.find("\n")
                if newline == -1:
                    cut = min(over - drop_len, len(tail))
                    tail = tail[cut:]
                    drop_len += cut
                    partial_line_cut = True
                    break
                tail = tail[newline + 1 :]
                drop_len += newline + 1
            continue
        if head:
            drop_len = 0
            while drop_len < over and head:
                newline = head.rfind("\n")
                if newline <= 0:
                    cut = min(over - drop_len, len(head))
                    head = head[: len(head) - cut]
                    drop_len += cut
                    partial_line_cut = True
                    break
                drop_len += len(head) - newline
                head = head[:newline]
            continue
        break
    result = head + marker + tail
    if len(result) > cap:
        # Degenerate cap: even with both sides emptied the marker (or a single
        # unshrinkable line) is larger than the cap. The bound wins over the
        # report here, and it is the only path that can cut the marker itself.
        result = result[:cap]
    return result


# Public name for callers outside engine.pipeline; the underscore form stays
# because engine/pipeline.py and its tests import it under that name.
bound_evidence = _bound_step_evidence
