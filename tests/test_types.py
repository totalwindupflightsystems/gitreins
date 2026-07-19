"""Dedicated tests for guard result types."""

from dataclasses import FrozenInstanceError

import pytest

from engine.types import GuardResult, Tier1Result


@pytest.mark.parametrize(
    ("name", "output", "detail"),
    [
        ("secrets", "", " — clean"),
        ("lint", "", " — ok"),
        ("go_lint", "", " — ok"),
        ("go_build", "", " — ok"),
        ("go_vet", "", " — ok"),
        ("tests", "3 passed", " — passed"),
        ("go_tests", "ok package/name", " — passed"),
        ("tests", "no tests collected", ""),
        ("custom", "ok", ""),
    ],
)
def test_guard_result_pass_detail(name, output, detail):
    result = GuardResult(name=name, passed=True, output=output)

    assert result._pass_detail() == detail


def test_guard_result_defaults_and_frozen_contract():
    result = GuardResult(name="lint", passed=True)

    assert result.output == ""
    assert result.error == ""
    with pytest.raises(FrozenInstanceError):
        setattr(result, "passed", False)


def test_tier1_summary_formats_passes_failures_and_empty_output():
    result = Tier1Result(
        passed=False,
        results=[
            GuardResult("secrets", True),
            GuardResult("tests", True, "2 PASSED"),
            GuardResult("lint", False, "E501 line too long\nsecond line"),
            GuardResult("custom", False, error="command failed"),
        ],
    )

    assert result.summary == "\n".join(
        [
            "  ✓ secrets — clean",
            "  ✓ tests — passed",
            "  ✗ lint — E501 line too long",
            "  ✗ custom",
        ]
    )


def test_tier1_summary_counts_failed_lines_over_first_detail():
    result = Tier1Result(
        passed=False,
        results=[
            GuardResult(
                "tests",
                False,
                "intro\nFAILED tests/test_one.py\nFAIL package/two\nignored",
            )
        ],
    )

    assert result.summary == "  ✗ tests — 2 failure(s)"


def test_tier1_summary_truncates_long_first_output_line():
    result = Tier1Result(
        passed=False,
        results=[GuardResult("lint", False, "x" * 101)],
    )

    assert result.summary == f"  ✗ lint — {'x' * 97}..."


def test_tier1_mutable_defaults_are_isolated_and_instance_is_frozen():
    first = Tier1Result(passed=True)
    second = Tier1Result(passed=True)

    first.results.append(GuardResult("lint", True))
    first.extra["key"] = "value"
    first.warnings.append("warning")

    assert second.results == []
    assert second.extra == {}
    assert second.warnings == []
    with pytest.raises(FrozenInstanceError):
        setattr(first, "passed", False)
