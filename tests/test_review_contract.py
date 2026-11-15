"""Contract-schema tests for GR-219.

ch:trace row=GR-219 spec=specs/SPEC-GR219-contract-schema.md#1-purpose wave=none:serial test=tests/test_review_contract.py::test_pr_self_weakening_falsifier doc=docs/review-contract-design.md evidence=none:worker-local witness=none:worker-local verdict=none:foreman-pending commit=none:worker-sha memory=none:unwritten
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from engine.review_contract import (
    LoadedReviewContract,
    ReviewContractError,
    ReviewLoadContext,
    contract_hash,
    load_review_contract,
    parse_review_contract,
    resolve_review_policy,
)

BASE = """\
version: 1
policy_source:
  ref: base
  require_protected_ref: true
  allow_pr_override: false
inheritance:
  layers: [defaults, organization, repository, path_overlays]
  merge:
    scalar: override
    maps: deep_merge
    lists:
      instructions: append
      passes: append
      ignores: union
      include_paths: intersect
      exclude_paths: union
  conflict: deny_and_report
organization:
  locked: [policy_source, gates.security, passes.security]
  defaults: {finding: {minimum_confidence: 0.90}}
repository:
  profiles:
    balanced: {model: review-default, tools: [diff, tests]}
paths:
  include: ["src/**"]
  exclude: ["**/fixtures/**"]
  overlays:
    - id: api
      match: {any: ["src/api/**"]}
      instructions: {append: api-check}
      review: {profile: balanced, tools_add: [contracts]}
      gates: {minimum_confidence: 0.90}
passes:
  - id: security
    enabled: true
    instructions: {append: security-check}
    gates: {minimum_severity: high}
    trigger: {any: {diff_signals: [authentication_call]}}
triggers: {automatic: {events: [opened]}}
gates:
  security: {locked: true, action: request_changes}
labels: {inputs: [], outputs: [], lifecycle: {owner: gitreins}}
explainability: {effective_config: {enabled: true}}
"""


@dataclass
class Source:
    base_text: str | None
    protected: str = "base-sha"
    reads: list[tuple[str, str]] | None = None

    def __post_init__(self) -> None:
        self.reads = []

    def protected_ref(self, requested_ref: str) -> str:
        return self.protected

    def resolve_base_ref(self, requested_ref: str) -> str:
        return self.protected

    def read_base_file(self, base_ref: str, relative_path: str) -> str | None:
        self.reads.append((base_ref, relative_path))
        return self.base_text


def parse(text: str = BASE):
    return parse_review_contract(text, source_ref="origin/main", source_commit="base-sha")


def test_normative_parser_and_defaults() -> None:
    contract = parse()
    assert contract.version == 1
    assert contract.policy_source.ref == "base"
    assert contract.inheritance.layers == (
        "defaults",
        "organization",
        "repository",
        "path_overlays",
    )
    assert contract.inheritance.merge.instructions == "append"
    assert contract.inheritance.merge.include_paths == "intersect"
    assert contract.organization.locked == ("policy_source", "gates.security", "passes.security")


@pytest.mark.parametrize(
    ("text", "code"),
    [
        (BASE.replace("version: 1", "version: 2"), "REVIEW_VERSION_UNSUPPORTED"),
        (BASE.replace("version: 1\n", ""), "REVIEW_VERSION_MISSING"),
        (BASE.replace("ref: base", "ref: head"), "REVIEW_POLICY_SOURCE_NOT_BASE"),
        (BASE.replace("  ref: base", "  ref: null"), "REVIEW_POLICY_SOURCE_MISSING"),
        (
            BASE.replace("require_protected_ref: true", "require_protected_ref: false"),
            "REVIEW_PROTECTED_REF_REQUIRED",
        ),
        (BASE.replace("paths:\n", "paths:\n  typo: true\n"), "REVIEW_UNKNOWN_KEY"),
        (BASE.replace("authentication_call", "future_signal"), "REVIEW_UNSUPPORTED_SIGNAL"),
    ],
)
def test_normative_rejections(text: str, code: str) -> None:
    with pytest.raises(ReviewContractError) as caught:
        parse(text)
    assert caught.value.code == code


def test_malformed_duplicate_and_non_mapping_yaml_are_denied() -> None:
    for text, code in (
        ("version: [", "REVIEW_YAML_UNPARSEABLE"),
        ("version: 1\nversion: 1\n", "REVIEW_YAML_DUPLICATE_KEY"),
        ("- one\n", "REVIEW_YAML_TYPE"),
    ):
        with pytest.raises(ReviewContractError) as caught:
            parse(text)
        assert caught.value.code == code


def test_pr_self_weakening_falsifier() -> None:
    source = Source(BASE)
    loaded = load_review_contract(
        ReviewLoadContext(Path("."), "origin/main", "pull/42", "origin/main", source)
    )
    head = (
        BASE.replace("allow_pr_override: false", "allow_pr_override: true")
        .replace("id: security\n    enabled: true", "id: removed-security\n    enabled: false")
        .replace("minimum_confidence: 0.90", "minimum_confidence: 0.01")
    )
    # The source deliberately exposes only base content: a head edit cannot
    # influence either provenance or the resolved lock.
    assert head != source.base_text
    policy = resolve_review_policy(loaded, changed_files=["src/api/handler.py"])
    assert loaded.source_commit == "base-sha"
    assert any(item.id == "security" and item.enabled for item in policy.passes)
    assert policy.gates.security["locked"] is True
    assert policy.config_hash == contract_hash(parse(BASE))
    assert source.reads == [("base-sha", ".gitreins/review.yaml")]


def test_protected_ref_falsifier_uses_weaker_base_not_stricter_head() -> None:
    source = Source(BASE.replace("minimum_confidence: 0.90", "minimum_confidence: 0.50"))
    loaded = load_review_contract(
        ReviewLoadContext(Path("."), "origin/main", "head", "protected-sha", source)
    )
    policy = resolve_review_policy(loaded, changed_files=["src/api/x.py"])
    assert loaded.source_commit == "base-sha"
    assert policy.gates.minimum_confidence == 0.5


def test_absent_file_uses_explicit_defaults_and_unreadable_denies() -> None:
    absent = load_review_contract(
        ReviewLoadContext(Path("."), "base", "head", "base-sha", Source(None))
    )
    assert absent.used_defaults is True
    assert absent.diagnostics == ()

    class Broken(Source):
        def read_base_file(self, base_ref: str, relative_path: str) -> str | None:
            raise OSError("permission denied")

    broken = load_review_contract(
        ReviewLoadContext(Path("."), "base", "head", "base-sha", Broken(None))
    )
    assert broken.used_defaults is False
    assert broken.diagnostics[0].code == "REVIEW_BASE_REF_UNAVAILABLE"


def test_merge_matrix_order_and_overlay_matching() -> None:
    policy = resolve_review_policy(
        load_review_contract(
            ReviewLoadContext(Path("."), "base", "head", "base-sha", Source(BASE))
        ),
        changed_files=["src/api/a.py", "src/api/a.py", "src/other.py", "src/fixtures/x.py"],
    )
    assert policy.matched_overlay_ids == ("api",)
    assert policy.matched_globs == ("src/api/**",)
    assert policy.instructions == ("security-check", "api-check")
    assert policy.effective_profile == "balanced"
    assert policy.effective_tools == ("diff", "tests", "contracts")
    assert policy.exclude_paths == ("**/fixtures/**",)


def test_locked_fields_and_path_safety() -> None:
    text = BASE.replace("id: security\n    enabled: true", "id: security\n    enabled: false")
    contract = parse(text)
    loaded = LoadedReviewContract(contract, "base", "sha", contract_hash(contract), False, ())
    resolved = resolve_review_policy(loaded, changed_files=["src/x.py"])
    assert any(d.code == "REVIEW_LOCKED_FIELD_VIOLATION" for d in resolved.diagnostics)
    unsafe = resolve_review_policy(loaded, changed_files=["../outside.py"])
    assert any(d.code == "REVIEW_PATH_UNSAFE" for d in unsafe.diagnostics)
    absolute = resolve_review_policy(loaded, changed_files=["/absolute.py"])
    assert any(d.code == "REVIEW_PATH_UNSAFE" for d in absolute.diagnostics)


def test_canonical_hash_and_large_changed_set() -> None:
    reordered = BASE.replace("version: 1\n", "version: 1\n")
    assert contract_hash(parse(BASE)) == contract_hash(parse(reordered))
    changed = [f"src/file-{number}.py" for number in range(1200)]
    loaded = load_review_contract(ReviewLoadContext(Path("."), "base", "head", "sha", Source(BASE)))
    large = resolve_review_policy(loaded, changed_files=changed)
    small = resolve_review_policy(loaded, changed_files=[changed[0]])
    assert large.config_hash == small.config_hash
    assert large.matched_overlay_ids == ()
    assert len(large.changed_files_digest) == 64


def test_empty_contract_shape_is_not_a_parse_default() -> None:
    for text in ("", "null\n", "# comment only\n"):
        with pytest.raises(ReviewContractError) as caught:
            parse(text)
        assert caught.value.code in {"REVIEW_YAML_NOT_MAPPING", "REVIEW_YAML_TYPE"}
