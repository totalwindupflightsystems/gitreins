"""Dedicated tests for guard result types."""

from dataclasses import FrozenInstanceError

import pytest

from engine.types import (
    SCANNER_CLEAN,
    SCANNER_NOT_RUN,
    GuardResult,
    Tier1Result,
    first_failing_test_detail,
    parse_first_failing_test,
    parse_gitleaks_finding_count,
    render_secrets_scanners,
    scanner_finding_status,
)


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
            "  ✗ lint — second line",
            "  ✗ custom",
        ]
    )


def test_tier1_summary_counts_failed_lines_and_shows_tail():
    # Only anchored pytest "FAILED <path>::<test>" lines count: "FAIL
    # package/two" merely contains the "FAIL" substring and must not inflate
    # the count (DF-021 substring-overcount regression). TRUST-003: the line
    # also names the FIRST failing test id parsed from the output.
    result = Tier1Result(
        passed=False,
        results=[
            GuardResult(
                "tests",
                False,
                "intro\nFAILED tests/test_one.py::test_a\nFAIL package/two\nignored",
            )
        ],
    )

    assert result.summary == (
        "  ✗ tests — FAIL (tests/test_one.py::test_a [first failing id]; 1 failure(s))"
    )


def test_tier1_summary_banner_tail_still_shows_failed_test_id():
    # Banner-tail regression: pytest's last output line is the
    # "=== N failed, M passed ===" banner — the summary must still name the
    # failing test id (DF-021, now via the parsed first id — TRUST-003 AC1).
    result = Tier1Result(
        passed=False,
        results=[
            GuardResult(
                "tests",
                False,
                "FAILED tests/test_bad.py::test_broken - assert 0 == 1\n"
                "=== 1 failed, 2 passed in 0.12s ===",
            )
        ],
    )

    assert result.summary == (
        "  ✗ tests — FAIL (tests/test_bad.py::test_broken [first failing id]; 1 failure(s))"
    )


def test_tier1_summary_substring_fail_lines_not_counted():
    # Substring-overcount regression: lines shaped like pytest output but
    # lacking the "FAILED <path>::<test>" anchor (no :: separator, bare FAIL)
    # are not pytest failures and contribute zero to the count.
    result = Tier1Result(
        passed=False,
        results=[
            GuardResult(
                "tests",
                False,
                "BUILD FAIL\nFAILED something went wrong\n"
                "tests/test_x.py::test_y FAILED\n=== 1 failed, 1 passed in 0.1s ===",
            )
        ],
    )

    assert result.summary == "  ✗ tests — === 1 failed, 1 passed in 0.1s ==="


def test_tier1_summary_counts_multiple_failed_lines():
    # Multi-FAILED regression: every anchored FAILED line is counted and the
    # line names the FIRST one (TRUST-003 AC1 — the first failure is the one
    # to fix first; the count keeps the blast radius visible).
    result = Tier1Result(
        passed=False,
        results=[
            GuardResult(
                "tests",
                False,
                "FAILED tests/test_a.py::test_one\n"
                "FAILED tests/test_b.py::test_two - assert boom\n"
                "FAILED tests/test_c.py::test_three",
            )
        ],
    )

    assert result.summary == (
        "  ✗ tests — FAIL (tests/test_a.py::test_one [first failing id]; 3 failure(s))"
    )


def test_tier1_summary_truncates_long_tail_line():
    result = Tier1Result(
        passed=False,
        results=[GuardResult("lint", False, "x" * 101)],
    )

    assert result.summary == f"  ✗ lint — {'x' * 97}..."


def test_tier1_summary_shows_failure_tail_not_first_line():
    # pytest-style output: the first line is a session banner, the error is at
    # the end — the summary must show the tail, not the banner.
    result = Tier1Result(
        passed=False,
        results=[
            GuardResult(
                "lint",
                False,
                "=== test session starts ===\n...\nERROR: the real failure is at the end",
            )
        ],
    )

    assert result.summary == "  ✗ lint — ERROR: the real failure is at the end"


def test_tier1_summary_combines_failure_count_with_tail():
    result = Tier1Result(
        passed=False,
        results=[
            GuardResult(
                "tests",
                False,
                "=== test session starts ===\n\nFAILED tests/test_x.py::test_y - AssertionError: boom",
            )
        ],
    )

    assert result.summary == (
        "  ✗ tests — FAIL (tests/test_x.py::test_y [first failing id]; 1 failure(s))"
    )


def test_tier1_summary_secrets_guard_includes_gitleaks_file_line():
    output = (
        "Finding:    1\n"
        "Secret:     REDACTED\n"
        "RuleID:     github-pat\n"
        "File:       src/config.py\n"
        "Line:       12\n"
        "Commit:     deadbeef\n"
        "\n"
        "Finding:    2\n"
        "Secret:     REDACTED\n"
        "File:       .env\n"
        "Line:       3\n"
    )
    result = Tier1Result(passed=False, results=[GuardResult("secrets", False, output)])

    assert result.summary == "  ✗ secrets — 2 finding(s): src/config.py:12, .env:3"


def test_tier1_summary_secrets_findings_keep_pairs_whole_on_overflow():
    # A single pair longer than the cap is dropped whole — never cut mid-path.
    long_path = "src/" + "a" * 80 + ".py"
    output = "\n".join(f"File:       {long_path}\nLine:       {i}" for i in range(1, 4))
    result = Tier1Result(passed=False, results=[GuardResult("secrets", False, output)])

    assert result.summary == "  ✗ secrets — 3 finding(s): …"


def test_tier1_summary_secrets_without_gitleaks_fields_uses_tail():
    # Built-in scanner output has no File:/Line: fields — falls back to tail.
    output = 'Potential secrets found:\n.env:3: [hardcoded API key] value="***"'
    result = Tier1Result(passed=False, results=[GuardResult("secrets", False, output)])

    assert result.summary == '  ✗ secrets — .env:3: [hardcoded API key] value="***"'


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


# ── TRUST-003: named failures + named secrets scanners ────────────
# Dogfood POC-14 / POC-15 follow-ons: a bounded console line said "tests —
# FAIL" without WHICH test and "secrets — clean/fail" without WHICH scanner,
# so every diagnosis started by re-running the tools by hand. These hermetic
# tests pin the parsers and the exact console strings both facts flow into.

_PYTEST_SHORT_SUMMARY = (
    "============================= test session starts =============================\n"
    "collected 7 items\n"
    "tests/test_x.py .....F.\n"
    "================================== FAILURES ===================================\n"
    "________________________________ TestY.test_z _________________________________\n"
    "E       assert 1 == 2\n"
    "========================= short test summary info =========================\n"
    "FAILED tests/test_x.py::TestY::test_z - AssertionError\n"
)


class TestFirstFailingTestParser:
    def test_first_failed_line_wins_not_the_last(self):
        output = (
            "FAILED tests/test_a.py::test_one - AssertionError: 1\n"
            "FAILED tests/test_b.py::test_two - AssertionError: 2\n"
        )

        assert parse_first_failing_test(output) == "tests/test_a.py::test_one"

    def test_pytest_short_summary_id_kept_verbatim(self):
        assert parse_first_failing_test(_PYTEST_SHORT_SUMMARY) == ("tests/test_x.py::TestY::test_z")

    def test_error_line_used_when_no_failed_line(self):
        output = "ERROR tests/test_setup.py::test_fixture - RuntimeError: no db\n"

        assert parse_first_failing_test(output) == "tests/test_setup.py::test_fixture"

    def test_failed_preferred_over_error(self):
        output = (
            "ERROR tests/test_setup.py::test_fixture - RuntimeError: no db\n"
            "FAILED tests/test_real.py::test_logic - AssertionError: nope\n"
        )

        assert parse_first_failing_test(output) == "tests/test_real.py::test_logic"

    def test_traceback_header_with_location_line_is_the_fallback(self):
        output = (
            "=================================== FAILURES ===================================\n"
            "____________________________ test_broken _____________________________\n"
            "tests/test_q.py:12: in test_broken\n"
            "    assert 1 == 2\n"
        )

        assert parse_first_failing_test(output) == "tests/test_q.py::test_broken"

    def test_unrecognized_output_is_none(self):
        # A bare "FAIL" / a FAILED-shaped line with no id must never invent one.
        assert parse_first_failing_test("") is None
        assert parse_first_failing_test("BUILD FAIL\nFAIL package/two\n") is None
        assert parse_first_failing_test("tests/test_x.py::test_y FAILED\n") is None


class TestFirstFailingTestDetail:
    def test_detail_names_the_id_and_the_count(self):
        assert first_failing_test_detail("FAILED tests/test_a.py::test_one\n", 3) == (
            "FAIL (tests/test_a.py::test_one [first failing id]; 3 failure(s))"
        )

    def test_detail_without_a_count_omits_the_clause(self):
        assert first_failing_test_detail("FAILED tests/test_a.py::test_one\n") == (
            "FAIL (tests/test_a.py::test_one [first failing id])"
        )

    def test_empty_detail_when_nothing_parses(self):
        assert first_failing_test_detail("no pytest output here", 0) == ""


class TestSecretsScannerAttribution:
    def test_scanner_finding_status_pluralizes(self):
        assert scanner_finding_status(1) == "1 finding"
        assert scanner_finding_status(2) == "2 findings"

    def test_clean_line_names_both_scanners(self):
        scanners = (("gitleaks", SCANNER_CLEAN), ("builtin", SCANNER_CLEAN))

        assert render_secrets_scanners(scanners) == "clean (gitleaks + builtin cross-check)"

    def test_failure_line_names_the_offending_scanner_first(self):
        scanners = (("gitleaks", SCANNER_CLEAN), ("builtin", "2 findings"))

        assert render_secrets_scanners(scanners) == (
            "FAIL (builtin cross-check: 2 findings; gitleaks: clean)"
        )

    def test_failure_line_names_both_when_both_find(self):
        scanners = (("gitleaks", "3 findings"), ("builtin", "1 finding"))

        assert render_secrets_scanners(scanners) == (
            "FAIL (gitleaks: 3 findings; builtin cross-check: 1 finding)"
        )

    def test_missing_gitleaks_is_named_not_implied(self):
        scanners = (("gitleaks", SCANNER_NOT_RUN), ("builtin", SCANNER_CLEAN))

        assert render_secrets_scanners(scanners) == (
            "clean (builtin cross-check; gitleaks not on PATH)"
        )

    def test_console_secrets_line_uses_the_attribution(self):
        clean = Tier1Result(
            passed=True,
            results=[
                GuardResult(
                    "secrets",
                    True,
                    "Scanned 3 files — clean",
                    scanners=(("gitleaks", SCANNER_CLEAN), ("builtin", SCANNER_CLEAN)),
                )
            ],
        )
        failed = Tier1Result(
            passed=False,
            results=[
                GuardResult(
                    "secrets",
                    False,
                    "Potential secrets found:\n"
                    '.env:1: [AWS access key] AWS_ACCESS_KEY_ID="***"\n'
                    'src/db.py:7: [hardcoded password] password="***"',
                    scanners=(("gitleaks", SCANNER_CLEAN), ("builtin", "2 findings")),
                )
            ],
        )

        assert clean.summary == "  ✓ secrets — clean (gitleaks + builtin cross-check)"
        assert failed.summary.split("\n")[0] == (
            "  ✗ secrets — FAIL (builtin cross-check: 2 findings; gitleaks: clean)"
        )
        # DF-004 is preserved: the built-in path:line locators still show.
        assert "      findings: 2 finding(s): .env:1, src/db.py:7" in failed.summary

    def test_scannerless_secrets_result_keeps_legacy_wording(self):
        legacy = GuardResult("secrets", True, "Scanned 1 files — clean")
        assert legacy._pass_detail() == " — clean"
        assert Tier1Result(passed=True, results=[legacy]).summary == "  ✓ secrets — clean"


class TestGitleaksFindingCountParser:
    def test_trailer_count_is_authoritative(self):
        output = "Finding:     x\nFile:        a.py\nLine:        1\nWRN leaks found: 1\n"

        assert parse_gitleaks_finding_count(output) == 1

    def test_finding_blocks_counted_without_the_trailer(self):
        output = "Finding:     a\nFile:        a.py\n\nFinding:     b\nFile:        b.py\n"

        assert parse_gitleaks_finding_count(output) == 2

    def test_file_fields_are_the_last_resort(self):
        assert parse_gitleaks_finding_count("File:        a.py\nFile:        b.py\n") == 2

    def test_unparseable_output_is_none_not_zero(self):
        # "clean" must never be inferred from output that carries no tally.
        assert parse_gitleaks_finding_count("") is None
        assert parse_gitleaks_finding_count("leak detected in config.py") is None
