"""DF-GITREINS-POC-19: the worktree/QA evidence bound is a LINE bound.

DF-GITREINS-POC-5 fixed the pipeline's step evidence (head+tail, complete
lines, marker charged against the cap). The same defect was still live in the
two siblings that feed QA evidence — `_evidence` in engine/worktree_fleet.py
and engine/worktree_disposable.py sliced the head at a raw character offset
and dropped everything after it. These tests hold both surfaces to the shared
contract, so a future edit cannot quietly reintroduce a head-only slice:

  * the bound is real (never more than MAX_EVIDENCE_CHARS),
  * nothing in the bounded text is a fragment of a payload line,
  * the LAST line of the run output survives when it fits the tail budget,
  * the marker names the chars and lines it dropped,
  * a single over-budget line is the only thing that may be cut mid-line, and
    the marker says so.
"""

from __future__ import annotations

import pytest

from engine import evidence_bounds
from engine.worktree_disposable import MAX_EVIDENCE_CHARS as DISPOSABLE_CAP
from engine.worktree_disposable import _evidence as disposable_evidence
from engine.worktree_fleet import MAX_EVIDENCE_CHARS as FLEET_CAP
from engine.worktree_fleet import _evidence as fleet_evidence

# Both surfaces, named so a failure says which one broke.
SURFACES = [
    pytest.param(fleet_evidence, id="worktree_fleet"),
    pytest.param(disposable_evidence, id="worktree_disposable"),
]

_MARKER_START = "… ["
_MARKER_END = "] …"


def _payload_lines(bounded: str) -> list[str]:
    """Payload lines of *bounded*, i.e. everything that is not marker text.

    Marker regions start with the `… [` tally line and, when ids were hoisted,
    are followed by the hoisted lines and a bare `…` terminator. Those lines
    came from the omitted middle, so they cannot be compared line-for-line.
    """
    lines = bounded.split("\n")
    kept: list[str] = []
    in_marker = False
    for line in lines:
        if line.startswith(_MARKER_START) or line.startswith("… ["):
            in_marker = True
            continue
        if in_marker:
            # `… [N further FAILED/ERROR line(s) not hoisted] …` re-arms it.
            if line.startswith(_MARKER_START):
                in_marker = True
                continue
            if line == "…":
                in_marker = False
            continue
        kept.append(line)
    return kept


def _pytest_shaped_run() -> str:
    """A pytest transcript longer than the cap, with the summary at the END."""
    lines = ["============================= test session starts ============================="]
    lines.append("platform linux -- Python 3.11.15, pytest-9.1.1, pluggy-1.6.0")
    for index in range(400):
        lines.append(
            f"tests/test_deep_module.py::test_case_{index:03d} PASSED [ {index % 100:3d}%]"
        )
    lines.append("tests/test_deep_module.py::test_breaks_in_the_middle FAILED [ 71%]")
    for index in range(40):
        lines.append(f"tests/test_deep_module.py::test_tail_{index:03d} PASSED [ 91%]")
    lines.append("=================================== FAILURES ===================================")
    lines.append("______________ test_breaks_in_the_middle ______________")
    lines.append("    assert 1 == 2")
    lines.append("E   assert 1 == 2")
    lines.append("=========================== short test summary info ============================")
    lines.append("FAILED tests/test_deep_module.py::test_breaks_in_the_middle - assert 1 == 2")
    lines.append(
        "========================= 1 failed, 439 passed in 12.34s ========================="
    )
    return "\n".join(lines)


def test_both_surfaces_use_one_bounder_and_one_cap():
    """No second implementation: both helpers are the shared bounder."""
    assert FLEET_CAP == DISPOSABLE_CAP == evidence_bounds.MAX_EVIDENCE_CHARS == 4000
    assert fleet_evidence("x") == disposable_evidence("x") == "x"
    assert evidence_bounds.bound_evidence is evidence_bounds._bound_step_evidence


@pytest.mark.parametrize("surface", SURFACES)
def test_pytest_shaped_evidence_keeps_the_summary_and_whole_lines(surface):
    payload = _pytest_shaped_run()
    assert len(payload) > FLEET_CAP  # the fixture actually exercises the bound

    bounded = surface(payload)

    # 1. a real bound
    assert len(bounded) <= FLEET_CAP

    # 2. the last line of the run survives (the failing-test summary)
    assert bounded.endswith(payload.split("\n")[-1])

    # 3. the marker names chars AND lines dropped
    assert "chars omitted" in bounded
    assert "line(s)" in bounded

    # 4. the FAILED id from the omitted middle is hoisted into the marker
    assert "test_breaks_in_the_middle" in bounded

    # 5. nothing outside the marker is a fragment of a payload line
    whole = {line for line in payload.split("\n") if line}
    for line in _payload_lines(bounded):
        if not line:
            # Empty lines are marker scaffolding (the marker region opens and
            # closes with a newline); they carry no payload fragment.
            continue
        assert line in whole, f"bounded evidence carried a partial line: {line!r}"


@pytest.mark.parametrize("surface", SURFACES)
def test_single_over_budget_line_is_the_only_mid_line_cut(surface):
    payload = "BANNER " + "X" * (FLEET_CAP * 3)

    bounded = surface(payload)

    assert len(bounded) <= FLEET_CAP
    assert "one over-budget line was cut mid-line" in bounded
    assert "chars omitted" in bounded
    # The suffix of the single long line is kept, so its tail is still visible.
    assert bounded.endswith("X" * 100)


@pytest.mark.parametrize("surface", SURFACES)
def test_under_budget_evidence_is_untouched_and_stripped(surface):
    payload = "\n\n  tests/a.py::test_one PASSED  \n\n"

    assert surface(payload) == "tests/a.py::test_one PASSED"
    exact = "Y" * FLEET_CAP
    assert surface(exact) == exact
