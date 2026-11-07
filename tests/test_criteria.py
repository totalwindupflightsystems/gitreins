"""
Unit tests for engine/criteria.py — GR-172: criterion gap detection.

Each acceptance criterion must carry a labelled failure condition (``FAILIF:`` /
``FAILS WHEN:`` / ``FAILURE CONDITION:``) and a proof (``PROOF:`` / ``PROOF
COMMAND:``) whose payload is a command or a locator (path, ``path:line``,
``path::test``), not bare prose.
"""

from engine.criteria import FAILURE_MARKERS, PROOF_MARKERS, RUNNER_TOKENS, criterion_gaps


class TestCriterionGaps:
    """criterion_gaps returns the missing field names in canonical order."""

    def test_both_markers_present_returns_empty(self) -> None:
        """A criterion carrying both sections with a real proof payload has no gaps."""
        assert (
            criterion_gaps(
                "Accepts email+password as JSON body; FAILIF: returns 200 on bad "
                "credentials. PROOF: tests/test_login.py::test_bad_credentials"
            )
            == []
        )

    def test_only_failif_missing_proof(self) -> None:
        """A failure condition without a proof is missing exactly 'proof'."""
        assert criterion_gaps("FAILIF: returns 200 on bad credentials") == ["proof"]

    def test_only_proof_missing_failure_condition(self) -> None:
        """A proof without a failure condition is missing exactly 'failure_condition'."""
        assert criterion_gaps("PROOF: tests/test_login.py::test_bad_credentials") == [
            "failure_condition"
        ]

    def test_neither_marker_returns_both_gaps(self) -> None:
        """Plain prose is missing both fields, in canonical order."""
        assert criterion_gaps("Has tests for happy path and error cases") == [
            "failure_condition",
            "proof",
        ]

    def test_bare_proof_marker_counts_as_missing_proof(self) -> None:
        """A bare PROOF: with nothing after it is 'proof' missing (GR-172 rule)."""
        assert criterion_gaps("FAILIF: returns 200 on bad credentials; PROOF:") == ["proof"]

    def test_whitespace_only_proof_payload_counts_as_missing(self) -> None:
        """A proof payload of fewer than 3 non-space chars is missing."""
        assert criterion_gaps("FAILIF: returns 200; PROOF:      ") == ["proof"]

    def test_short_prose_proof_payload_counts_as_missing(self) -> None:
        """A two-character payload is below the non-space floor."""
        assert criterion_gaps("FAILIF: returns 200; PROOF: ok") == ["proof"]

    def test_prose_proof_payload_counts_as_missing(self) -> None:
        """A payload that is neither command-shaped nor locator-shaped is missing."""
        assert criterion_gaps("FAILIF: returns 200; PROOF: see the test suite") == ["proof"]

    def test_lowercase_failif_accepted(self) -> None:
        """Marker detection is case-insensitive."""
        assert criterion_gaps("failif: returns 200 on bad credentials. proof: make test") == []

    def test_fails_when_marker_accepted(self) -> None:
        """'FAILS WHEN:' is a recognised failure-condition marker."""
        assert criterion_gaps("FAILS WHEN: token expired\nPROOF: tests/test_auth.py:42") == []

    def test_failure_condition_marker_accepted(self) -> None:
        """'FAILURE CONDITION:' is a recognised failure-condition marker."""
        assert (
            criterion_gaps("FAILURE CONDITION: 401 returned. PROOF: curl -i http://localhost/login")
            == []
        )

    def test_proof_command_marker_accepted(self) -> None:
        """'PROOF COMMAND:' is an accepted proof marker alias."""
        assert (
            criterion_gaps(
                "FAILIF: no retry on 429; PROOF COMMAND: "
                ".venv/bin/python -m pytest -q tests/test_retry.py"
            )
            == []
        )

    def test_proof_locator_accepted(self) -> None:
        """A path::test locator is accepted as a proof payload."""
        assert criterion_gaps("FAILIF: idempotency broken; PROOF: tests/test_x.py::test_y") == []

    def test_proof_path_line_locator_accepted(self) -> None:
        """A path:line locator is accepted as a proof payload."""
        assert criterion_gaps("FAILIF: auth bypass; PROOF: src/auth.py:118") == []

    def test_proof_command_starting_with_runner_token_accepted(self) -> None:
        """A bare runner command with no slash/locator shape is still a proof."""
        assert criterion_gaps("FAILIF: build broken; PROOF: cargo test --quiet") == []

    def test_leading_text_before_markers_accepted(self) -> None:
        """Optional leading prose before the markers does not block detection."""
        assert (
            criterion_gaps(
                "Returns JWT on success\nFAILS WHEN: signature invalid; "
                "PROOF: pytest -q tests/test_jwt.py"
            )
            == []
        )


class TestMarkerTables:
    """The public marker tables carry the documented spellings."""

    def test_failure_marker_fragments(self) -> None:
        """FAILURE_MARKERS covers failif / fails when / failure condition."""
        joined = " ".join(FAILURE_MARKERS)
        assert "failif" in joined
        assert "when" in joined
        assert "condition" in joined

    def test_proof_marker_fragments(self) -> None:
        """PROOF_MARKERS covers proof and its command alias."""
        joined = " ".join(PROOF_MARKERS)
        assert "proof" in joined
        assert "command" in joined

    def test_runner_tokens_include_repo_venv_and_core_runners(self) -> None:
        """RUNNER_TOKENS covers the venv python and the common runners."""
        for token in (".venv/", "pytest", "python", "make", "go", "cargo", "gitreins"):
            assert token in RUNNER_TOKENS
