"""Evidence contract v1 (gitreins.evidence/v1) — schema conformance tests.

Ported from external PR #1 by rorca-hermes (adopted design, credited), then
ADAPTED to the 0.15.0 tree. The PR's original tests drove `gitreins guard
--json`, `judge --ephemeral`, `report --json` and the `engine.evidence`
dumps/guard helpers. EVID-001 published the contract itself (the schema file
and the contract document) and pinned it with sample documents; EVID-002 landed
the emitters, so this module now grades BOTH halves:

- the published schema is valid JSON Schema draft 2020-12 and rejects
  malformed documents (missing required fields, unknown fields, bad enum
  values, oversized strings, over-cap check arrays),
- the emitters (`guard_evidence`/`judge_evidence`/`report_evidence` +
  `dumps_evidence`) produce documents that validate, stay under the 32 KiB
  cap, and honour the always-on redaction rules,
- the CLI surface really emits exactly one such document (`guard --json`,
  `judge --json`, `report --json`) on stdout, with the contract's exit codes,
- the `--scope working-tree` change set is collected READ-ONLY: it adds
  unstaged and non-ignored untracked files, excludes ignored ones, and leaves
  the git index byte-identical after the run (EVID-002 AC1/AC2/AC3),
- the contract document's normative promises stay in the doc.

`judge --ephemeral` is still unimplemented (EVID-003); its subject shape is
pinned at the schema level below.
"""

import hashlib
import json
import os
import subprocess
from types import SimpleNamespace

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

from engine.evidence import (
    EVIDENCE_SCHEMA,
    EVIDENCE_SCHEMA_VERSION,
    MAX_EVIDENCE_BYTES,
    MAX_TEXT_CHARS,
    dumps_evidence,
    guard_evidence,
    judge_evidence,
    redact_text,
    report_evidence,
)
from engine.guard_manager import GuardManager
from engine.types import GuardResult, Tier1Result
from tests.test_cli import run_cli

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


# ── EVID-002: emitters, --json surface and the working-tree scope ────────────
#
# Helpers. A REAL repository is required for the scope tests: the shared
# `tmp_workdir` fixture only fakes a `.git` directory (enough for
# `rev-parse`, not for an index), and the whole point of these tests is what
# git reports about the index and the working tree.


def _git(repo, *args, check=True):
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=check,
        capture_output=True,
        text=True,
    )


def _init_repo(path, name="scope-repo"):
    """A real repository with one commit, returned as a str path."""
    repo = os.path.join(str(path), name)
    os.makedirs(repo, exist_ok=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "GitReins Tests")
    _git(repo, "config", "user.email", "gitreins-tests@example.invalid")
    with open(os.path.join(repo, "base.txt"), "w", encoding="utf-8") as handle:
        handle.write("base\n")
    _git(repo, "add", "base.txt")
    _git(repo, "commit", "-qm", "initial")
    return repo


def _index_state(repo):
    """Everything an index mutation would change: the file, and git's view."""
    index_path = os.path.join(repo, ".git", "index")
    digest = None
    if os.path.exists(index_path):
        with open(index_path, "rb") as handle:
            digest = hashlib.sha256(handle.read()).hexdigest()
    cached = _git(repo, "diff", "--cached", "--binary").stdout
    return digest, cached


def _guard_config(repo, **guards):
    """Write a minimal .gitreins/config.yaml for the CLI guard path."""
    cfg_dir = os.path.join(repo, ".gitreins")
    os.makedirs(cfg_dir, exist_ok=True)
    settings = {"secrets": True, "lint": False, "tests": False, "allow_skips": True}
    settings.update(guards)
    lines = ["guards:"] + [f"  {key}: {str(value).lower()}" for key, value in settings.items()]
    with open(os.path.join(cfg_dir, "config.yaml"), "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


def _guard_manager(repo, scope="staged"):
    """A fast guard manager: secrets only, on the given scope."""
    return GuardManager(
        repo,
        {"guards": {"secrets": True, "lint": False, "tests": False}},
        scope=scope,
    )


# EVID-002 AC1/AC5: the working-tree change set is staged + unstaged +
# non-ignored untracked, and the ignored file stays out of it.
def test_working_tree_scope_collects_unstaged_and_untracked_but_not_ignored(tmp_path):
    repo = _init_repo(tmp_path)
    with open(os.path.join(repo, ".gitignore"), "w", encoding="utf-8") as handle:
        handle.write("ignored.py\n")
    _git(repo, "add", ".gitignore")
    _git(repo, "commit", "-qm", "ignore rules")
    with open(os.path.join(repo, "tracked.py"), "w", encoding="utf-8") as handle:
        handle.write("value = 1\n")
    _git(repo, "add", "tracked.py")
    _git(repo, "commit", "-qm", "tracked")

    # Unstaged modification, untracked file, ignored file, staged addition.
    with open(os.path.join(repo, "tracked.py"), "w", encoding="utf-8") as handle:
        handle.write("value = 2\n")
    with open(os.path.join(repo, "untracked.py"), "w", encoding="utf-8") as handle:
        handle.write("safe = True\n")
    with open(os.path.join(repo, "ignored.py"), "w", encoding="utf-8") as handle:
        handle.write("safe = True\n")
    with open(os.path.join(repo, "staged_only.py"), "w", encoding="utf-8") as handle:
        handle.write("value = 3\n")
    _git(repo, "add", "staged_only.py")

    assert _guard_manager(repo).changed_files == ["staged_only.py"]
    assert _guard_manager(repo, scope="working-tree").changed_files == [
        "staged_only.py",
        "tracked.py",
        "untracked.py",
    ]


# EVID-002 AC1/AC5: a working-tree-scope run mutates NOTHING in the index —
# not the file, not git's own diff of it — and still finds an unstaged secret
# the staged scope cannot see (which is the reason the scope exists).
def test_working_tree_scope_grades_unstaged_content_read_only(tmp_path):
    repo = _init_repo(tmp_path)
    secret = 'api_key = "sk-abcdefghijklmnop1234XYZ"\n'
    leaky = os.path.join(repo, "leaky_module.py")
    with open(leaky, "w", encoding="utf-8") as handle:
        handle.write(secret)
    with open(os.path.join(repo, "untracked.py"), "w", encoding="utf-8") as handle:
        handle.write("safe = True\n")

    before = _index_state(repo)
    result = _guard_manager(repo, scope="working-tree").run_all()

    assert result.passed is False, "an unstaged secret must fail a working-tree run"
    assert "leaky_module.py" in result.summary
    assert "abcdefghijklmnop1234XYZ" not in result.summary, (
        "the scanner must not echo the secret value it found"
    )
    assert result.extra["changed_count"] == 2, "the scope holds leaky_module.py + untracked.py"
    assert _index_state(repo) == before, (
        "working-tree collection must use read-only git commands only"
    )

    # Default scope, same tree: nothing is in the index, so nothing is graded —
    # the unstaged/untracked files above are simply not part of that change set.
    staged = _guard_manager(repo).run_all()
    assert staged.passed is True
    assert "leaky_module.py" not in staged.summary
    assert _guard_manager(repo).changed_files == []


# EVID-002 AC1: a scope-specific value the GuardManager refuses outright (the
# CLI turns this into a usage error before the manager is built).
def test_unknown_scope_is_refused_by_the_manager(tmp_path):
    repo = _init_repo(tmp_path)
    with pytest.raises(ValueError):
        GuardManager(repo, {"guards": {"secrets": False}}, scope="universe")


# EVID-002 AC4: the guard document validates, honours the 32 KiB cap, and
# redacts + reports truncation at both levels.
def test_guard_evidence_validates_is_bounded_and_redacted(validate):
    secret = "Bearer abcdefghijklmnopqrstuvwxyz.1234567890"
    result = Tier1Result(
        passed=False,
        results=[GuardResult("secrets", False, (secret + "\n") * 1000)],
        extra={"changed_count": 1},
    )
    payload = dumps_evidence(guard_evidence(result, "working-tree"))
    decoded = json.loads(payload)

    assert len(payload.encode("utf-8")) <= MAX_EVIDENCE_BYTES
    assert validate(decoded) == []
    assert secret not in payload
    assert "[REDACTED]" in payload
    assert decoded["metadata"]["redacted"] is True
    assert decoded["metadata"]["redactionsApplied"] is True
    assert decoded["metadata"]["truncated"] is True
    assert decoded["outcome"] == "fail"
    assert decoded["command"] == "guard"
    assert decoded["scope"] == "working-tree"
    check = decoded["checks"][0]
    assert check["id"] == "secrets"
    assert check["outcome"] == "fail"
    assert check["passed"] is False
    assert len(check["summary"]) == MAX_TEXT_CHARS
    assert set(check) == {"id", "outcome", "passed", "summary"}


# EVID-002: a DEGRADED pass is passed=true with the skipped gates reported as
# the honest no-grade shape — never as a green check.
def test_guard_evidence_reports_a_degraded_pass_honestly(validate):
    result = Tier1Result(
        passed=True,
        results=[
            GuardResult("secrets", True, "gitleaks: clean"),
            GuardResult(
                "lint",
                True,
                "No Python files staged",
                skipped=True,
                skip_reason="no staged files",
            ),
            GuardResult(
                "tests",
                True,
                "No files staged — skipped",
                skipped=True,
                skip_reason="no staged files",
            ),
        ],
    )
    decoded = json.loads(dumps_evidence(guard_evidence(result, "staged")))

    assert validate(decoded) == []
    assert decoded["passed"] is True
    assert decoded["outcome"] == "pass"
    assert decoded["metadata"]["degraded"] is True
    assert decoded["metadata"]["skippedSteps"] == ["lint", "tests"]
    assert decoded["metadata"]["truncated"] is False
    assert "DEGRADED PASS" in decoded["summary"]
    lint_check = next(check for check in decoded["checks"] if check["id"] == "lint")
    assert lint_check["outcome"] == "unknown"
    assert lint_check["passed"] is None
    assert "no staged files" in lint_check["summary"]


# EVID-002: component text is capped BEFORE the document, and both levels
# report their truncation.
def test_evidence_caps_component_text_and_the_document(validate):
    result = Tier1Result(
        passed=True,
        results=[GuardResult("lint", True, "x" * (MAX_TEXT_CHARS * 3))],
    )
    decoded = json.loads(dumps_evidence(guard_evidence(result, "staged")))
    assert validate(decoded) == []
    assert decoded["metadata"]["truncated"] is True
    assert len(decoded["checks"][0]["summary"]) <= MAX_TEXT_CHARS

    # A pathological document (the maximum 32 checks, each at its cap) is
    # trimmed from the tail until it fits — and says so.
    payload = dumps_evidence(
        guard_evidence(
            Tier1Result(
                passed=True,
                results=[
                    GuardResult(f"check-{index}", True, "y" * MAX_TEXT_CHARS) for index in range(32)
                ],
            ),
            "staged",
        )
    )
    decoded = json.loads(payload)
    assert len(payload.encode("utf-8")) <= MAX_EVIDENCE_BYTES
    assert validate(decoded) == []
    assert len(decoded["checks"]) < 32
    assert decoded["metadata"]["truncated"] is True
    assert decoded["metadata"]["checkCount"] == 32


# EVID-002: the redaction boundary itself — secret shapes are replaced, the
# label is kept, and the flags say a replacement happened.
def test_redaction_replaces_secret_shapes():
    text, redacted, truncated = redact_text(
        "token = sk-abcdefghijklmnopqrstuvwxyz and Bearer abcdefghijklmnop.12345"
    )
    assert redacted is True
    assert truncated is False
    assert "abcdefghijklmnopqrstuvwxyz" not in text
    assert "abcdefghijklmnop.12345" not in text
    assert text.startswith("token = [REDACTED]")
    assert "[REDACTED]" in text

    clean, redacted, _ = redact_text("nothing to hide here")
    assert clean == "nothing to hide here"
    assert redacted is False


# EVID-002 AC3/AC4: the CLI emits exactly ONE document, exit 0 on a pass.
def test_guard_cli_json_is_a_single_valid_document(tmp_path, validate):
    repo = _init_repo(tmp_path)
    _guard_config(repo)
    before = _index_state(repo)

    result = run_cli("guard", "--json", "--scope", "working-tree", cwd=repo)

    assert result.returncode == 0, result.stderr
    assert result.stdout.count("\n") == 1, "stdout must hold exactly one document"
    payload = json.loads(result.stdout)
    assert validate(payload) == []
    assert payload["$schema"] == EVIDENCE_SCHEMA
    assert payload["schemaVersion"] == EVIDENCE_SCHEMA_VERSION
    assert payload["producer"]["name"] == "gitreins"
    assert payload["command"] == "guard"
    assert payload["scope"] == "working-tree"
    assert payload["outcome"] == "pass"
    assert payload["metadata"]["redacted"] is True
    assert len(result.stdout.encode("utf-8")) <= MAX_EVIDENCE_BYTES + 1  # + newline
    assert _index_state(repo) == before


# EVID-002 AC4: a non-passing run exits 1 and reports the failing check, and
# the exit code is the only signal a script needs.
def test_guard_cli_json_exits_1_on_a_working_tree_failure(tmp_path, validate):
    repo = _init_repo(tmp_path)
    _guard_config(repo)
    with open(os.path.join(repo, "leaky_module.py"), "w", encoding="utf-8") as handle:
        handle.write('api_key = "sk-abcdefghijklmnop1234XYZ"\n')
    before = _index_state(repo)

    result = run_cli("guard", "--json", "--scope", "working-tree", cwd=repo)

    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert validate(payload) == []
    assert payload["outcome"] == "fail"
    assert payload["passed"] is False
    assert payload["checks"][0]["id"] == "secrets"
    assert payload["checks"][0]["outcome"] == "fail"
    assert payload["metadata"]["changedFileCount"] >= 1, (
        "the document must say how many files were in scope"
    )
    assert "abcdefghijklmnop1234XYZ" not in result.stdout
    assert _index_state(repo) == before


# EVID-002 AC2: with no --scope flag the CLI grades the INDEX, exactly as it
# always has — the unstaged secret above is invisible to it.
def test_guard_cli_json_defaults_to_the_staged_scope(tmp_path, validate):
    repo = _init_repo(tmp_path)
    _guard_config(repo)
    with open(os.path.join(repo, "leaky_module.py"), "w", encoding="utf-8") as handle:
        handle.write('api_key = "sk-abcdefghijklmnop1234XYZ"\n')

    staged_run = run_cli("guard", "--json", cwd=repo)
    working_run = run_cli("guard", "--json", "--scope", "working-tree", cwd=repo)

    assert json.loads(staged_run.stdout)["scope"] == "staged"
    assert staged_run.returncode == 0, "the unstaged secret is not in the index"
    assert json.loads(working_run.stdout)["scope"] == "working-tree"
    assert working_run.returncode == 1


# EVID-002 AC3: without --json the human output is unchanged, and the scope
# note appears only for a non-default scope.
def test_guard_human_output_is_unchanged_without_json(tmp_path):
    repo = _init_repo(tmp_path)
    _guard_config(repo)

    default_run = run_cli("guard", cwd=repo)
    assert "Tier 1 Guards: PASS" in default_run.stdout
    assert "scope:" not in default_run.stdout

    scoped_run = run_cli("guard", "--scope", "working-tree", cwd=repo)
    assert "Tier 1 Guards: PASS" in scoped_run.stdout
    assert ", scope: working-tree" in scoped_run.stdout


# EVID-002: an invalid --scope is a CLI usage error (argparse exits 2), which
# is the contract's third exit code.
def test_guard_scope_rejects_an_unknown_value_at_the_cli(tmp_path):
    repo = _init_repo(tmp_path)
    _guard_config(repo)
    result = run_cli("guard", "--scope", "universe", cwd=repo)
    assert result.returncode == 2
    assert "universe" in result.stderr


# EVID-002 AC5 + the PR's LSP half: an explicit scope is filtered to what the
# configured server can actually grade.
def test_select_lsp_files_filters_by_tool_language(tmp_workdir):
    from engine.lsp import select_lsp_files

    module = os.path.join(tmp_workdir, "module.py")
    readme = os.path.join(tmp_workdir, "README.md")
    with open(module, "w", encoding="utf-8") as handle:
        handle.write("value = 1\n")
    with open(readme, "w", encoding="utf-8") as handle:
        handle.write("# Documentation — not Python\n")

    assert select_lsp_files("pylsp", tmp_workdir, ["module.py", "README.md"]) == [module]
    assert select_lsp_files("pylsp", tmp_workdir, ["gone.py"]) == []


# EVID-002: the judge document carries the subject, the tier-2 criteria and the
# tier-1 evidence, and validates.
def test_judge_evidence_document_shape(validate):
    task = SimpleNamespace(
        id="EVID-002",
        title="Adopt the v1 evidence emitters",
        criteria=["guard emits it", "judge emits it"],
    )
    verdict = SimpleNamespace(
        items=[
            SimpleNamespace(criterion="guard emits it", status="PASS", detail="ok"),
            SimpleNamespace(criterion="judge emits it", status="FAIL", detail="missing"),
        ]
    )
    result = SimpleNamespace(
        passed=False,
        verdict=verdict,
        tier1=None,
        pipeline_result={
            "stages": {"tier1": {"passed": True, "summary": "secrets+lint+tests"}},
        },
        summary="Judge Result: EVID-002",
    )

    decoded = json.loads(dumps_evidence(judge_evidence(result, task, "working-tree")))

    assert validate(decoded) == []
    assert decoded["command"] == "judge"
    assert decoded["scope"] == "working-tree"
    assert decoded["outcome"] == "fail"
    assert decoded["subject"] == {
        "taskId": "EVID-002",
        "title": "Adopt the v1 evidence emitters",
        "ephemeral": False,
    }
    assert [check["id"] for check in decoded["checks"]] == [
        "criterion-1",
        "criterion-2",
        "stage-tier1",
    ]
    assert decoded["checks"][1]["passed"] is False
    assert decoded["metadata"]["historyPersisted"] is True
    assert decoded["metadata"]["criterionCount"] == 2
    assert decoded["metadata"]["checkCount"] == 3


# EVID-002: the pipeline path buries the tier-2 verdict in a stage step; the
# criteria must still reach the document.
def test_judge_evidence_reads_criteria_from_the_pipeline_path(validate):
    task = SimpleNamespace(id="EVID-003", title="Ephemeral judge", criteria=["c"])
    result = SimpleNamespace(
        passed=True,
        verdict=None,
        tier1=None,
        pipeline_result={
            "stages": {
                "tier1": {"passed": True, "summary": "secrets"},
                "tier2": {
                    "passed": True,
                    "steps": [
                        {
                            "data": {
                                "verdict": "COMPLETE",
                                "items": [
                                    {"criterion": "c", "status": "PASS", "detail": "verified"}
                                ],
                                "summary": "all good",
                            }
                        }
                    ],
                },
            }
        },
        summary="Judge Result: EVID-003",
    )

    decoded = json.loads(dumps_evidence(judge_evidence(result, task, "staged")))

    assert validate(decoded) == []
    assert decoded["passed"] is True
    criteria = [check for check in decoded["checks"] if check["id"].startswith("criterion-")]
    assert len(criteria) == 1
    assert criteria[0]["outcome"] == "pass"
    assert "verified" in criteria[0]["summary"]


# EVID-002: `judge --json` emits one document on stdout — the evaluator's and
# the persister's narration is captured — and the document names the subject.
def test_judge_cli_json_is_a_single_document(tmp_workdir, validate):
    verdict_json = json.dumps(
        {
            "verdict": "COMPLETE",
            "items": [{"criterion": "c1", "status": "PASS", "detail": "ok"}],
            "summary": "all good",
        }
    )
    run_cli("task", "create", "evid-judge", "Judge JSON", "c1", cwd=tmp_workdir)
    result = run_cli(
        "judge",
        "evid-judge",
        "--json",
        "--scope",
        "working-tree",
        cwd=tmp_workdir,
        extra_env={"GITREINS_MOCK_LLM_RESPONSE": json.dumps({"content": verdict_json})},
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.count("\n") == 1, "stdout must hold exactly one document"
    payload = json.loads(result.stdout)
    assert validate(payload) == []
    assert payload["command"] == "judge"
    assert payload["scope"] == "working-tree"
    assert payload["subject"]["taskId"] == "evid-judge"
    assert payload["subject"]["ephemeral"] is False
    assert payload["metadata"]["historyPersisted"] is True


# EVID-002: on the JSON surface the document channel stays clean even when no
# document can be produced — the "Task not found" prose goes to stderr.
def test_judge_cli_json_keeps_stdout_clean_for_an_unknown_task(tmp_workdir):
    result = run_cli("judge", "no-such-task", "--json", cwd=tmp_workdir)
    assert result.returncode == 1
    assert result.stdout.strip() == "", "nothing but a document may reach stdout"
    assert "Task not found" in result.stderr


# EVID-002: report is the history-scoped member — always exit 0, bounded,
# redacted, and its storage mode is the persister's.
def test_report_cli_json_is_a_bounded_history_document(tmp_workdir, validate):
    entry_dir = os.path.join(tmp_workdir, ".gitreins", "history", "2026-07-13", "abcdef12")
    os.makedirs(entry_dir, exist_ok=True)
    with open(os.path.join(entry_dir, "verdict.json"), "w", encoding="utf-8") as handle:
        json.dump(
            {
                "task_id": "history-task",
                "task_title": "Bearer abcdefghijklmnopqrstuvwxyz.1234567890",
                "passed": True,
            },
            handle,
        )

    result = run_cli("report", "--json", cwd=tmp_workdir)

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert validate(payload) == []
    assert payload["command"] == "report"
    assert payload["scope"] == "history"
    assert payload["passed"] is None
    assert payload["metadata"]["checkCount"] == 1
    assert payload["metadata"]["storage"] == "git"
    assert payload["metadata"]["redactionsApplied"] is True
    assert payload["checks"][0]["id"] == "history-task"
    assert payload["checks"][0]["passed"] is True
    assert "abcdefghijklmnopqrstuvwxyz.1234567890" not in result.stdout
    assert "1 recent verdicts" in payload["summary"]


# EVID-002: the report document for an EMPTY history is still a valid v1
# document (nothing to report is not an error).
def test_report_evidence_without_entries_still_validates(validate):
    payload = dumps_evidence(report_evidence([], "filesystem"))
    decoded = json.loads(payload)
    assert validate(decoded) == []
    assert decoded["checks"] == []
    assert decoded["metadata"]["checkCount"] == 0
    assert decoded["metadata"]["storage"] == "filesystem"
    assert decoded["outcome"] == "unknown"
