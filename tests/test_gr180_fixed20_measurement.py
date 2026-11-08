"""GR-180 rework: the fixed-20 reflection measurement harness.

Pins the artifact scripts/gr180_fixed20_measurement.py to its contract:
exactly 20 fixed cases, independent fixture truth labels, the shipped
FindingReflector code path (stub only at the LLM seam), the required printed
summary lines, and the before/after numbers the evidence artifact quotes.

ch:trace row=GR-180 spec=engine/review_profiles.py#FindingReflector \
wave=none:serial tick test=tests/test_gr180_fixed20_measurement.py::test_fixed_case_count_and_metrics \
doc=docs/measurement/gr180-fixed20.md commit=none:foreman-binds-after-merge
"""

from __future__ import annotations

import io
import json
from contextlib import redirect_stdout

import pytest

import scripts.gr180_fixed20_measurement as m
from engine.review_profiles import DEFAULT_REFLECTION_THRESHOLD


def test_fixed_case_count_and_metrics() -> None:
    """AC4: exactly 20 fixed cases; before/after precision and fabricated
    counts computed from independent labels through the real filter."""
    assert len(m.CASES) == 20
    assert m.FIXED_CASE_COUNT == 20
    result = m.measure()
    assert result["case_count"] == 20
    assert result["surviving_before"] == 20
    assert result["surviving_after"] == 13
    assert result["fabricated_before"] == 7
    assert result["fabricated_after"] == 1
    assert result["precision_before"] == pytest.approx(13 / 20)
    assert result["precision_after"] == pytest.approx(12 / 13)


def test_labels_are_independent_and_explicit() -> None:
    """Every case carries an explicit correct/fabricated truth label and an
    expected outcome authored in the fixture — no label is derived from the
    stub score, and the fabricated set is non-empty (else the precision proxy
    would measure nothing)."""
    labels = {c["label"] for c in m.CASES}
    assert labels == {"correct", "fabricated"}
    for case in m.CASES:
        assert case["expected_outcome"] in ("kept", "dropped")
        assert case["id"] and case["finding"]["detection"]
    fabricated = [c for c in m.CASES if c["label"] == "fabricated"]
    assert len(fabricated) >= 2
    # fabricated findings cite files/lines the fixed diff cannot confirm:
    # at least one names a file that is not in the diff at all.
    assert any("app/pricing.py" == c["finding"]["file"] for c in fabricated)
    assert any("app/auth.py" == c["finding"]["file"] for c in fabricated)


def test_filter_removes_labeled_fabricated_and_keeps_labeled_correct() -> None:
    """The measurement demonstrates the required direction: at least one
    labeled fabricated finding is removed while every labeled correct finding
    that the fixture expects kept actually survives the shipped filter."""
    result = m.measure()
    outcomes = {o["id"]: o for o in result["outcomes"]}
    # a labeled fabricated finding WAS removed by the filter
    assert outcomes["case-13"]["survived"] is False
    # labeled correct findings expected kept survived
    for case in m.CASES:
        if case["label"] == "correct" and case["expected_outcome"] == "kept":
            assert outcomes[case["id"]]["survived"] is True, case["id"]
    # the disclosed stub misses keep the measurement honest: case-12 is a
    # correct finding the scorer wrongly drops, case-20 a fabricated finding
    # the scorer wrongly keeps. precision_after < 1.0 proves the harness did
    # not rig a perfect number.
    assert outcomes["case-12"]["survived"] is False
    assert outcomes["case-20"]["survived"] is True
    assert 0.0 < result["precision_after"] < 1.0
    assert result["precision_after"] > result["precision_before"]


def test_expected_outcomes_match_actual() -> None:
    """The fixture's independent expected-outcome column agrees with what the
    shipped reflect() produced, case by case — the ledger's 'actual' column is
    not decoration."""
    result = m.measure()
    for case, outcome in zip(m.CASES, result["outcomes"]):
        actual = "kept" if outcome["survived"] else "dropped"
        assert actual == case["expected_outcome"], case["id"]


def test_fail_closed_paths_are_exercised() -> None:
    """AC2 inside the fixed corpus: a missing score entry and an unusable
    score each drop exactly their finding (per-finding fail-closed), with the
    reasons recorded (AC3 visibility)."""
    result = m.measure()
    by_index = {d.index: d for d in result["dropped"]}
    assert "no score" in by_index[16].reason
    assert "unusable reflection score" in by_index[17].reason
    assert result["error"] is None  # per-finding path, not a total drop


def test_reflection_failure_drops_everything_fail_closed() -> None:
    """AC2 on the corpus itself: when the stubbed reply is unparseable, the
    shipped reflect() drops all 20 findings and surviving_after is 0 — the
    filter never promotes on failure."""
    corpus = m.CASES
    reply = "these all look fine to me"
    findings = [m.SecondPassFinding(**c["finding"]) for c in corpus]
    reflector = m.FindingReflector(
        m.StubReflectionLLM(reply), workdir=str(m.REPO_ROOT), threshold=DEFAULT_REFLECTION_THRESHOLD
    )
    result = reflector.reflect(findings, diff_text=m.FIXED_DIFF)
    assert result.kept == []
    assert len(result.dropped) == 20
    assert result.error is not None and "unparseable" in result.error
    record = result.to_dict()
    assert record is not None and record["dropped"] == 20


def test_required_summary_lines_are_printed() -> None:
    """The harness prints every line the brief names: case count=20, and
    candidate / surviving / fabricated / precision, each with before and after."""
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = m.main()
    out = buf.getvalue()
    assert rc == 0
    for needle in (
        "case count=20",
        "candidate findings before=",
        "candidate findings before=20 after=20",
        "surviving findings before=20 after=13",
        "fabricated findings before=7 after=1",
        "precision before=0.6500 after=0.9231",
    ):
        assert needle in out, needle
    # the proxy disclaimer rides with the numbers, not behind them
    assert "LABEL PROXY" in out
    assert "case count=20" in out.splitlines()[2]


def test_no_network_is_used() -> None:
    """The stub LLM answers every chat() with the canned fixture reply; the
    run makes exactly one reflection call and imports nothing that talks."""
    calls: list[int] = []

    class CountingStub(m.StubReflectionLLM):
        def chat(self, messages, tools=None, max_tokens=None):  # type: ignore[override]
            calls.append(1)
            return super().chat(messages, tools, max_tokens)

    findings = [m.SecondPassFinding(**c["finding"]) for c in m.CASES]
    reflector = m.FindingReflector(
        CountingStub(m.build_scores_reply(m.CASES)), workdir=str(m.REPO_ROOT)
    )
    result = reflector.reflect(findings, diff_text=m.FIXED_DIFF)
    assert len(calls) == 1
    assert len(result.kept) == 13


def test_stub_reply_is_valid_json_with_19_entries_and_one_omission() -> None:
    """The fixture reply covers 19 of 20 indices (case-17 is omitted on
    purpose — the no-score fail-closed path) and parses cleanly."""
    data = json.loads(m.build_scores_reply(m.CASES))
    indices = [e["index"] for e in data["scores"]]
    assert len(indices) == 19
    assert 16 not in indices
    assert indices == sorted(set(indices))


def test_metrics_are_deterministic_across_runs() -> None:
    """Two runs over the fixed corpus produce byte-identical outcomes — the
    quoted evidence artifact is reproducible, not a sampling fluke."""
    first = json.dumps(m.measure()["outcomes"], sort_keys=True)
    second = json.dumps(m.measure()["outcomes"], sort_keys=True)
    assert first == second
