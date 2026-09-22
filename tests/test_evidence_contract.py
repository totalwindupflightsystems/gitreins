"""Evidence contract v1 (gitreins.evidence/v1) — schema conformance tests.

Ported from external PR #1 by rorca-hermes (adopted design, credited), then
ADAPTED to the 0.15.0 tree. The PR's original tests drove `gitreins guard
--json`, `judge --ephemeral`, `report --json` and the `engine.evidence`
dumps/guard helpers; none of those surfaces exist on main yet — the emitting
implementation is a later row (EVID-002). The contract itself (the schema
file and the contract document) is published here verbatim, so this module
validates the SCHEMA against sample evidence documents instead of deleting
the coverage:

- the schema file is valid JSON Schema draft 2020-12,
- well-formed guard/judge/report documents validate against it,
- malformed documents (missing required fields, unknown fields, bad enum
  values, oversized strings, over-cap check arrays) are rejected,
- the contract document's normative promises (single JSON document, exit
  codes, 32 KiB cap, always-on redaction, additive-only evolution) are
  pinned by asserting the exact statements stay in the doc.

Coverage from the PR that could NOT be restructured here (the features do
not exist on main, so there is nothing to exercise): the working-tree scope
collection/no-index-mutation behaviour and the LSP file selection filter.
Their coverage lands with EVID-002, where the emitting implementation is
built and these tests grow back their CLI halves.
"""

import json
import os

import pytest

try:
    # Available in the dev environment transitively (python-lsp-server in the
    # `dev` extra pulls jsonschema; it is also pinned in uv.lock). The guard
    # keeps collection alive if that chain ever changes; the structural
    # assertions below do the heavy lifting without the library either way.
    from jsonschema import Draft202012Validator

    HAS_JSONSCHEMA = True
except ImportError:  # pragma: no cover - only hit on a stripped dev env
    HAS_JSONSCHEMA = False

SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "..", "schemas", "evidence-v1.schema.json")
CONTRACT_DOC_PATH = os.path.join(os.path.dirname(__file__), "..", "docs", "evidence-contract-v1.md")

SCHEMA_URL = "https://gitreins.dev/schemas/evidence/v1.json"

pytestmark = pytest.mark.skipif(not HAS_JSONSCHEMA, reason="jsonschema not installed")


def _base_evidence(**overrides):
    """A minimal valid v1 evidence document (guard variant), overridable."""
    doc = {
        "$schema": SCHEMA_URL,
        "schemaVersion": "1.0",
        "producer": {"name": "gitreins", "version": "0.15.0"},
        "command": "guard",
        "generatedAt": "2026-09-22T12:00:00Z",
        "scope": "staged",
        "outcome": "pass",
        "passed": True,
        "summary": "guards passed",
        "checks": [{"id": "secrets", "outcome": "pass", "passed": True, "summary": "clean"}],
        "metadata": {"redacted": True, "redactionsApplied": False, "truncated": False},
    }
    doc.update(overrides)
    return doc


def _validator(schema):
    """Compile the schema once and return a validate(document) -> [messages]."""

    def _validate(document):
        found = Draft202012Validator(schema).iter_errors(document)
        return sorted(err.message for err in found)

    return _validate


@pytest.fixture(scope="module")
def schema():
    with open(SCHEMA_PATH, encoding="utf-8") as handle:
        return json.load(handle)


@pytest.fixture(scope="module")
def validate(schema):
    Draft202012Validator.check_schema(schema)
    return _validator(schema)


# Adapted from the PR's test_guard_cli_json_is_single_v1_document preamble
# (`--json` invocation replaced by loading the published schema file): the
# contract is only useful if the published file is a valid draft 2020-12
# schema and pins the v1 identity.
def test_schema_file_is_valid_json_schema_draft_2020_12(schema, validate):
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["$id"] == SCHEMA_URL
    assert schema["type"] == "object"
    assert validate(_base_evidence()) == []


# Adapted from the PR's _parse_v1 identity assertions: every emitted document
# must self-identify with the v1 URL, schemaVersion "1.0" and producer
# "gitreins" — the consts are what make additive-only evolution enforceable.
def test_schema_pins_the_v1_identity(schema, validate):
    assert schema["properties"]["$schema"]["const"] == SCHEMA_URL
    assert schema["properties"]["schemaVersion"]["const"] == "1.0"
    assert schema["properties"]["producer"]["properties"]["name"]["const"] == "gitreins"
    for broken, field in [
        ({**_base_evidence(), "$schema": "https://example.invalid/other.json"}, "$schema"),
    ]:
        assert validate(broken), f"expected rejection for wrong {field}"
    assert validate(_base_evidence(schemaVersion="2.0"))
    assert validate(_base_evidence(producer={"name": "other", "version": "1"}))


# Adapted from the PR's _parse_v1 redaction assertion and the metadata half of
# test_guard_json_contract_is_bounded_and_redacted: redaction is ALWAYS on, so
# the schema must reject a document that claims otherwise and must not accept
# one that omits the truncation/redaction bookkeeping.
def test_redaction_and_truncation_metadata_are_mandatory(schema, validate):
    stripped = _base_evidence()
    del stripped["metadata"]
    assert validate(stripped)
    assert validate(
        _base_evidence(metadata={"redacted": False, "redactionsApplied": False, "truncated": False})
    )
    minimal = _base_evidence()
    del minimal["metadata"]["truncated"]
    assert validate(minimal)
    # metadata deliberately has no additionalProperties:false — it is the
    # documented additive-evolution seam of the contract.
    assert "additionalProperties" not in schema["properties"]["metadata"]


# Adapted from the PR's test_guard_cli_json_is_single_v1_document body
# (payload["command"] == "guard", payload["scope"] == "working-tree"): the
# command/scope vocabulary is pinned here until the CLI half exists.
def test_command_and_scope_vocabularies(schema, validate):
    assert schema["properties"]["command"]["enum"] == ["guard", "judge", "report"]
    assert schema["properties"]["scope"]["enum"] == ["staged", "working-tree", "history"]
    working_tree = _base_evidence(scope="working-tree")
    assert validate(working_tree) == []
    assert validate(_base_evidence(command="bogus"))
    assert validate(_base_evidence(scope="universe"))


# Adapted from the PR's test_guard_json_contract_is_bounded_and_redacted
# (32 KiB cap + outcome assertions): byte-budget enforcement lives in the
# emitter (EVID-002); what the SCHEMA can pin is the outcome vocabulary, the
# per-check shape and the check-array cap the budget fills.
def test_checks_array_shape_and_cap(schema, validate):
    checks = schema["properties"]["checks"]
    assert checks["maxItems"] == 32
    item = checks["items"]
    assert item["additionalProperties"] is False
    assert sorted(item["required"]) == ["id", "outcome", "passed", "summary"]

    at_cap = _base_evidence(
        checks=[
            {"id": f"check-{n}", "outcome": "pass", "passed": True, "summary": "ok"}
            for n in range(32)
        ]
    )
    assert validate(at_cap) == []
    over_cap = _base_evidence(
        checks=[
            {"id": f"check-{n}", "outcome": "pass", "passed": True, "summary": "ok"}
            for n in range(33)
        ]
    )
    assert validate(over_cap)
    assert validate(
        _base_evidence(
            checks=[{"id": "x", "outcome": "pass", "passed": True, "summary": "ok", "extra": 1}]
        )
    )
    assert validate(_base_evidence(checks=[{"id": "x", "outcome": "pass", "passed": True}]))
    assert validate(_base_evidence(outcome="ok", checks=[]))


# Adapted from the PR's test_report_cli_json_contract_redacts_history
# (payload["scope"] == "history", payload["command"] == "report"): report is
# the history-scoped member of the contract.
def test_report_document_uses_history_scope(schema, validate):
    report = _base_evidence(command="report", scope="history", passed=None)
    assert validate(report) == []
    # outcome "unknown" pairs with passed null — the honest no-data shape.
    assert (
        validate(_base_evidence(command="report", scope="history", outcome="unknown", passed=None))
        == []
    )


# Adapted from the PR's test_ephemeral_judge_has_no_task_history_stash_or_
# branch_side_effects (payload["subject"]["ephemeral"] is True): the
# side-effect-free ephemeral behaviour cannot be exercised until the
# `judge --ephemeral` CLI exists (EVID-002); the subject shape is pinned here.
def test_judge_document_subject_shape(schema, validate):
    subject = schema["properties"]["subject"]
    assert subject["additionalProperties"] is False
    assert sorted(subject["required"]) == ["ephemeral", "taskId", "title"]
    judge = _base_evidence(
        command="judge",
        scope="working-tree",
        subject={"taskId": "rorca-run-42-US-001", "title": "Story gate", "ephemeral": True},
    )
    assert validate(judge) == []
    assert validate(_base_evidence(subject={"taskId": "t", "title": "T"}))  # ephemeral missing
    assert validate(_base_evidence(subject={"taskId": 1, "title": "T", "ephemeral": True}))


# Adapted from the PR's compatibility promise + truncation test: strings are
# capped at the schema level, unknown top-level fields are rejected (so an
# incompatible shape is impossible without a new schema URL), and types are
# strict (booleans are not ints, strings are not numbers).
def test_length_caps_types_and_closed_top_level(schema, validate):
    props = schema["properties"]
    assert props["summary"]["maxLength"] == 2048
    assert props["producer"]["properties"]["version"]["maxLength"] == 64
    assert schema["additionalProperties"] is False

    assert validate(_base_evidence(summary="x" * 2048)) == []
    assert validate(_base_evidence(summary="x" * 2049))
    assert validate(_base_evidence(producer={"name": "gitreins", "version": "v" * 65}))
    assert validate(_base_evidence(unexpected="field"))
    assert validate(_base_evidence(passed="yes"))
    assert validate(_base_evidence(generatedAt=1758542400))
    assert validate(_base_evidence(checks="all good"))


# New in this adaptation (no PR equivalent needed it): the contract document
# is the normative statement of the automation surface, so each acceptance
# promise must stay present — this is the doc-drift half of the contract.
def test_contract_doc_states_the_normative_promises():
    with open(CONTRACT_DOC_PATH, encoding="utf-8") as handle:
        doc = handle.read()
    for promise in [
        # one UTF-8 JSON document per command, nothing else on stdout
        "writes exactly one UTF-8 JSON document to stdout",
        # the normative schema is identified by URL and schemaVersion
        SCHEMA_URL,
        'schemaVersion: "1.0"',
        "schemas/evidence-v1.schema.json",
        # exit codes: 0 pass, 1 non-pass, 2 usage error
        "exit `0` only for a passing result and `1` for a non-passing result",
        "CLI usage errors exit `2`",
        # 32 KiB cap with truncation reported
        "JSON is capped at 32 KiB",
        "`metadata.truncated` reports any truncation",
        # redaction always on
        "secret-redaction boundary",
        "`metadata.redacted` is always `true`",
        "`redactionsApplied` indicates a detected replacement",
        # additive-only evolution within v1
        "Additive metadata fields may be introduced",
        "new schema URL and major contract version",
    ]:
        assert promise in doc, f"contract doc lost its promise: {promise}"
