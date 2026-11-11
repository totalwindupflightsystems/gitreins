# SPEC-GR219 — Review Contract Schema and Layer Resolution

## 1. Purpose

Define the version-1 `.gitreins/review.yaml` contract, its typed Python representation, protected-base loading, validation failures, and deterministic layer merge so every review run evaluates one auditable policy.

The design authority is `docs/review-contract-design.md` on `github/main`, fetched before this spec was written. The YAML excerpt in §3 is normative: field names, values, and comments are contract text, not illustrative prose.

**Normative requirements**

- **R1** `review.yaml` MUST declare `version: 1` when the file exists. `test: test_review_contract_rejects_missing_or_unknown_version`.
- **R2** `policy_source.ref` MUST equal `base`; no other value is accepted in v1. `test: test_policy_source_base_is_mandatory`.
- **R3** The policy used for a PR MUST be loaded from the protected base branch, never from the PR head. `test: test_pr_self_weakening_falsifier`.
- **R4** Layer order MUST be `defaults < organization < repository < path_overlays`; `pr_policy` is deferred from v1. `test: test_layer_order_and_precedence`.
- **R5** Merge behavior MUST be scalar override, map deep-merge, instructions append, passes append, ignores union, include-paths intersect, exclude-paths union, and conflict deny-and-report. `test: test_merge_semantics_matrix`.

## 2. Interface

Implementation belongs in `engine/review_contract.py`; existing `engine/pipeline.py`, `engine/judge.py`, `engine/config_schema.py`, and `engine/review_profiles.py` are the integration surfaces inspected for this spec. The implementation MUST use standard-library dataclasses (frozen value objects where stated) and PyYAML's safe loader; it MUST NOT call an LLM to parse, merge, match, validate, or detect triggers.

```python
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

LayerName = Literal["defaults", "organization", "repository", "path_overlays"]
MergeKind = Literal["scalar", "maps", "instructions", "passes", "ignores", "include_paths", "exclude_paths"]

class PolicySource(Protocol):
    def read_base_file(self, base_ref: str, relative_path: str) -> str | None: ...
    def resolve_base_ref(self, requested_ref: str) -> str: ...
    def protected_ref(self, requested_ref: str) -> str: ...

@dataclass(frozen=True)
class ReviewLoadContext:
    workdir: Path
    base_ref: str
    head_ref: str
    protected_base_ref: str
    source: PolicySource

@dataclass(frozen=True)
class ReviewContractError(Exception):
    code: str
    message: str
    path: str = ""
    layer: LayerName | None = None
    details: tuple[str, ...] = ()

@dataclass(frozen=True)
class ReviewContract:
    version: Literal[1]
    policy_source: "PolicySourceConfig"
    inheritance: "InheritanceConfig"
    organization: "OrganizationConfig"
    repository: "RepositoryConfig"
    paths: "PathSelectionConfig"
    passes: tuple["PassConfig", ...]
    triggers: "TriggerConfig"
    gates: "GateConfig"
    labels: "LabelConfig"
    explainability: "ExplainabilityConfig"

@dataclass(frozen=True)
class LoadedReviewContract:
    contract: ReviewContract
    source_ref: str
    source_commit: str
    config_hash: str
    used_defaults: bool
    diagnostics: tuple["ReviewDiagnostic", ...]

def load_review_contract(
    context: ReviewLoadContext,
) -> LoadedReviewContract: ...

def parse_review_contract(
    yaml_text: str,
    *,
    source_ref: str,
    source_commit: str,
) -> ReviewContract: ...

def resolve_review_policy(
    loaded: LoadedReviewContract,
    *,
    changed_files: Sequence[str],
    labels: Sequence[str] = (),
    target_branch: str | None = None,
    is_draft: bool = False,
    title: str = "",
    changed_lines: Mapping[str, set[int]] | None = None,
) -> "ResolvedReviewPolicy": ...
```

`load_review_contract` MUST resolve the protected ref to an immutable commit SHA before reading `.gitreins/review.yaml`; it MUST read the file from that SHA and persist the SHA and config hash. `head_ref` is used only to compute the diff inputs supplied to `resolve_review_policy`; it MUST NOT be used to read policy. `parse_review_contract` is pure and is the unit-test seam for malformed YAML and schema errors.

The resolver returns a complete policy, not a patch:

```python
@dataclass(frozen=True)
class ResolvedReviewPolicy:
    layers: tuple[LayerName, ...]
    matched_overlay_ids: tuple[str, ...]
    matched_globs: tuple[str, ...]
    instructions: tuple[str, ...]
    passes: tuple["ResolvedPass", ...]
    include_paths: tuple[str, ...]
    exclude_paths: tuple[str, ...]
    effective_profile: str | None
    effective_tools: tuple[str, ...]
    gates: "GateConfig"
    labels: "ResolvedLabels"
    config_hash: str
    diagnostics: tuple["ReviewDiagnostic", ...]

@dataclass(frozen=True)
class ReviewDiagnostic:
    code: str
    severity: Literal["warning", "error"]
    message: str
    path: str = ""
    layer: LayerName | None = None
```

The resolver MUST preserve deterministic ordering: layer order, source list order, and first-seen list order are retained after union/append operations. A conflict diagnostic never disappears merely because a later layer supplies a value.

## 3. Data Model

The following YAML is quoted from the design document and is normative for v1. Implementations MUST accept these keys and preserve the shown semantics. The document's comments are normative constraints.

```yaml
version: 1

policy_source:
  ref: base                      # resolve policy from the PROTECTED base branch
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
  extends: org://engineering/review-baseline
  locked: [policy_source, gates.security, passes.security]   # org cannot be downgraded by repo
  defaults:
    finding:
      minimum_confidence: 0.85

repository:
  profiles:
    balanced: {model: review-default, tools: [diff, repository_search, tests]}
    strict:   {model: review-high-reasoning, tools: [diff, repository_search, tests, static_analysis]}
  ignores:
    paths: ["**/vendor/**", "**/generated/**", "**/*.lock"]
    findings:
      - rule_id: "STYLE-*"
        disposition: suppress
        reason: "enforced by formatter"

paths:
  include: ["src/**", "services/**", "migrations/**"]
  exclude: ["**/fixtures/**", "**/snapshots/**"]
  overlays:
    - id: api-boundary
      match: {any: ["api/**", "routes/**", "**/*controller*"]}
      instructions:
        append: |
          Treat this as an API boundary. Check authentication, authorization,
          input validation, output filtering, backward compatibility, and
          error disclosure. Require concrete evidence for every blocking finding.
      review: {profile: strict, tools_add: [api_contracts]}
      gates: {minimum_severity: high, minimum_confidence: 0.90}
      labels: {add: [gitreins:scope/api]}
    - id: tests
      match: {any: ["**/*_test.go", "**/*.spec.ts", "**/*.test.ts"]}
      instructions:
        append: |
          Check edge cases, negative paths, determinism, isolation, and whether
          the test exercises production wiring rather than a mock-only path.
      gates:
        require: [test_coverage_delta, changed_behavior_has_test]
    - id: generated
      match: {any: ["**/generated/**", "**/*.gen.*"]}
      action: exclude_from_review
      reason: "generated source"

passes:
  - id: correctness
    enabled: true
    scope: {include: ["**"]}
    instructions: {append: "Find correctness defects, unhandled errors, broken invariants, and regressions introduced by the diff."}
    execution: {model: review-default, budget: 120, timeout_seconds: 600, tools: [diff, repository_search, tests]}
    output: {finding_types: [bug, regression], blocking_severities: [critical, high]}
  - id: security
    enabled: true
    trigger:
      any:
        paths: ["auth/**", "security/**", "**/*middleware*", "**/*permission*"]
        diff_signals: [authentication_call, deserialization] # future-only names are rejected at contract load
    instructions:
      append: |
        Perform a focused security review. Trace attacker-controlled input to
        sensitive effects. Check authentication, authorization, injection,
        secret exposure, SSRF, unsafe deserialization, and auditability.
    execution: {model: security-review, budget: 180, timeout_seconds: 900, tools: [diff, repository_search, dependency_graph, security_scanner]}
    gates:
      minimum_severity: high
      minimum_confidence: 0.90
      require: [evidence_on_changed_line_or_direct_dataflow]
      on_fail: request_changes
    output: {labels: {add: [gitreins:pass/security]}}
  - id: performance
    enabled: true
    trigger:
      any:
        paths: ["**/*query*", "**/*repository*", "db/**", "storage/**"]
        diff_signals: [unbounded_query] # future-only names are rejected at contract load
    execution: {model: performance-review, budget: 150, tools: [diff, repository_search, query_analyzer, benchmark_lookup]}
    gates: {minimum_severity: high, minimum_confidence: 0.85, require: [evidence_or_reproducible_reasoning]}
  - id: public-api
    enabled: true
    trigger:
      any:
        paths: ["api/**", "openapi/**", "sdk/**"]
        diff_signals: [public_signature_change] # future-only names are rejected at contract load
    gates:
      require: [backward_compatibility_assessed]
      on_fail: request_changes

triggers:
  automatic: {events: [opened, synchronize, reopened, ready_for_review], review_new_pushes: true}
  skip:
    drafts: false
    labels: ["gitreins:skip"]
    title_patterns: ["^WIP:", "\\[skip-review\\]"]
  dynamic:
    - id: large-diff
      when: {any: [{files_changed_gt: 50}, {changed_lines_gt: 1000}]}
      then: {add_pass: architecture-impact, set_profile: strict}
    - id: high-risk-metadata
      when: {any: [{labels: ["security-sensitive", "breaking-change"]}, {target_branch: ["main", "release/**"]}]}
      then: {require_passes: [security, public-api], require_human_approval: true}

gates:
  security:
    locked: true
    when: {any_pass_has: {severity_at_least: high, confidence_at_least: 0.90}}
    action: request_changes
  path_rules:
    - match: ["migrations/**"]
      require: [rollback_plan, data_loss_analysis]
      action: fail
  finding_policy:
    block_when:
      all: [severity_at_least: high, confidence_at_least: 0.85, introduced_by_diff: true, disposition: unresolved]
    do_not_block_when:
      any:
        - baseline_existing: true
        - outside_changed_lines: true
          unless: direct_dataflow_from_changed_code

labels:
  inputs:
    - {name: gitreins:skip, effect: skip_automatic_review}
    - {name: security-sensitive, effect: require_pass, pass: security}
  outputs:
    - {name: gitreins:pass/security, when: pass.security.completed}
    - {name: gitreins:blocked, when: gate.failed}
  lifecycle: {owner: gitreins, remove_stale_outputs: true}

explainability:
  effective_config: {enabled: true, commands: ["gitreins review explain --base origin/main --head HEAD"]}
  trace_each_finding:
    include: [pass_id, matched_layers, matched_globs, appended_instructions,
              effective_model, effective_tools, config_hash, diff_range, gate_evaluation]
  dry_run: {enabled: true, show_suppressed_rules: true, show_unavailable_passes: true}
```

The typed model MUST include these structures and defaults:

```python
@dataclass(frozen=True)
class PolicySourceConfig:
    ref: Literal["base"] = "base"
    require_protected_ref: bool = True
    allow_pr_override: bool = False

@dataclass(frozen=True)
class InheritanceConfig:
    layers: tuple[LayerName, ...] = ("defaults", "organization", "repository", "path_overlays")
    merge: "MergeConfig" = MergeConfig()
    conflict: Literal["deny_and_report"] = "deny_and_report"

@dataclass(frozen=True)
class MergeConfig:
    scalar: Literal["override"] = "override"
    maps: Literal["deep_merge"] = "deep_merge"
    instructions: Literal["append"] = "append"
    passes: Literal["append"] = "append"
    ignores: Literal["union"] = "union"
    include_paths: Literal["intersect"] = "intersect"
    exclude_paths: Literal["union"] = "union"

@dataclass(frozen=True)
class ProfileConfig:
    model: str
    tools: tuple[str, ...] = ()

@dataclass(frozen=True)
class OrganizationConfig:
    extends: str | None = None
    locked: tuple[str, ...] = ()
    defaults: Mapping[str, Any] = dataclasses.field(default_factory=dict)

@dataclass(frozen=True)
class RepositoryConfig:
    profiles: Mapping[str, ProfileConfig] = dataclasses.field(default_factory=dict)
    ignores: "IgnoreConfig" = IgnoreConfig()

@dataclass(frozen=True)
class PathSelectionConfig:
    include: tuple[str, ...] = ("**",)
    exclude: tuple[str, ...] = ()
    overlays: tuple["PathOverlayConfig", ...] = ()
```

Unknown keys are errors inside typed contract objects, except extension maps explicitly typed as `Mapping[str, Any]` (`organization.defaults`, finding rule payloads, and profile names). Duplicate YAML mapping keys are errors. `version` must be an integer token equal to `1`, not a string.

## 4. Wiring

1. `engine/pipeline.py:load_pipeline_config` remains the compatibility loader for `.gitreins/config.yaml`; the review-contract loader is called before a review pipeline is built. It receives the same `workdir` and scan root, then contributes the resolved instructions, pass list, tools, and gates to the pipeline task context.
2. `engine/judge.py:Judge.evaluate_task` receives the resolved policy and attaches it to the run context. A review failure must be represented in `JudgeResult`, not raised as an untyped exception.
3. `engine/config_schema.py` gains review-contract validation only through a named adapter; existing `.gitreins/config.yaml` warnings MUST NOT silently accept a broken `.gitreins/review.yaml`.
4. The pre-commit path invokes resolution with the protected base branch and the staged changed-file set. A failed contract load returns `DENY_AND_REPORT` and a non-zero gate result.
5. The CLI explain surface is specified in SPEC-GR223. Its `--base` value selects the comparison ref, but policy still comes from the protected version of that ref.
6. The config hash is SHA-256 over canonical JSON of the parsed, validated base contract (sorted mapping keys, UTF-8, no whitespace variance). It is included in every resolved policy and finding trace.

## 5. Error Catalog

| Code | Trigger | Required behavior |
|---|---|---|
| `REVIEW_YAML_UNPARSEABLE` | `yaml.safe_load` raises `YAMLError` | Deny the review, emit the source path and line/column when available, never use silent defaults. |
| `REVIEW_YAML_NOT_MAPPING` | Root is not a mapping | Deny and report `<root>` plus the observed YAML type. |
| `REVIEW_VERSION_MISSING` / `REVIEW_VERSION_UNSUPPORTED` | Missing or non-`1` version | Deny and report `version`. |
| `REVIEW_POLICY_SOURCE_MISSING` / `REVIEW_POLICY_SOURCE_NOT_BASE` | `policy_source.ref` absent or not exactly `base` | Deny and report `policy_source.ref`. |
| `REVIEW_PROTECTED_REF_REQUIRED` | `require_protected_ref` is false or protected ref cannot be established | Deny; do not read policy from head. |
| `REVIEW_UNKNOWN_LAYER` | `inheritance.layers` contains an unknown/duplicate layer or changes required order | Deny and report the offending index. |
| `REVIEW_LOCKED_FIELD_VIOLATION` | Lower-precedence layer changes an organization-locked field | Deny and report field path, locking layer, attempted layer, and both values. |
| `REVIEW_CONFLICT` | Incompatible collection/scalar shapes or conflicting duplicate overlay/pass IDs | Deny and report every conflict; no last-writer choice. |
| `REVIEW_GLOB_PARSE_FAILURE` | Invalid glob in any include/exclude/overlay matcher | Deny and report the field path and exact pattern. |
| `REVIEW_BASE_REF_UNAVAILABLE` | Protected base cannot be resolved/read | Deny and report the ref and git/provider error without falling back to head. |
| `REVIEW_ORG_SOURCE_UNAVAILABLE` | `organization.extends` cannot be loaded | Deny unless the organization layer is explicitly absent; report URI and cause. |

A denied run has `passed: false`, `disposition: deny_and_report`, and a structured diagnostics list. It never reports an ordinary finding-only failure.

## 6. Edge Cases

- No `.gitreins/review.yaml` on the protected base returns explicit, versioned v1 defaults, with `used_defaults=True`; it does not invent contract gates. The result still carries a config hash of the canonical default object. An unreadable or malformed file is never treated as absent.
- Broken YAML is never treated as a missing file. This is the required broken-YAML denial falsifier.
- A PR that edits `.gitreins/review.yaml` is evaluated against the base copy. A head-only `policy_source.allow_pr_override: true`, lower threshold, removed security pass, or `gates: {}` MUST have no effect.
- Organization-locked fields include the exact paths listed in `organization.locked`; a lock applies recursively to the named map path. Repository and PR layers cannot remove, weaken, or replace those values.
- A path overlay ID is unique within the resolved policy. Two layers defining the same ID are a conflict unless the higher layer is an explicit merge-compatible overlay; v1 treats duplicate IDs as deny-and-report.
- Empty lists are explicit values: `passes: []` appends nothing; `ignores.paths: []` adds nothing; `include_paths: []` intersects to no paths. An omitted list is not an empty explicit list.
- A 1,000+ file diff is processed without recursion over the Cartesian product of files and rules: compile each glob once, scan each changed file once per required deterministic signal family, and retain bounded diagnostics.
- A symlink, path traversal (`../`), absolute path, NUL byte, or non-normalized changed path is rejected before matching.

## 7. Testing

Use `pytest` with temporary bare repositories and a fake `PolicySource`. Each test asserts both the result and diagnostics; no test may pass merely because an exception was raised.

1. Parse the normative YAML excerpt and assert `ReviewContract.version == 1`, `policy_source.ref == "base"`, ordered layers, and every merge operator.
2. Delete each required field in turn; assert the named validation code.
3. Insert unknown keys at root, `policy_source`, `inheritance`, `repository`, `paths`, a pass, a gate, and explainability; assert denial and the exact path.
4. Supply malformed YAML and assert `REVIEW_YAML_UNPARSEABLE`, `passed=False`, and no resolved defaults.
5. Run the **PR-self-weakening falsifier**: base contains locked security gate and `minimum_confidence: 0.90`; head changes `review.yaml` to `allow_pr_override: true`, removes security, and sets confidence `0.01`; resolve using base/head and assert the security pass, lock, confidence, and hash are from base.
6. Run the protected-ref falsifier: make the head policy stricter and the base policy weaker; assert the weaker base policy is used, proving policy provenance rather than “always choose strictest.”
7. Execute a merge-semantics matrix with scalar, nested map, instruction list, pass list, ignore list, include list, and exclude list rows. Assert exact output ordering, de-duplication, intersection, and conflict diagnostics.
8. Test organization locks at `policy_source`, `gates.security`, and `passes.security`; assert attempts to disable, replace, or lower them all deny.
9. Test absent file, empty file, root `null`, duplicate YAML keys, and a file containing only comments.
10. Generate a 1,200-file changed set and assert resolution completes, returns the same policy/hash as the small equivalent set, and does not duplicate matched overlay IDs.
11. Assert canonical config hashes are equal for semantically identical YAML with reordered mapping keys and unequal when a policy value changes.
12. Run `ruff check` and `pytest` for the new module and tests; run the repository's existing test command before implementation closeout.

## 8. Hilo Impact

This spec depends on the existing `engine/config.py` defaults, `engine/config_schema.py` issue vocabulary, `engine/review_profiles.py` profile and effort types, Git ref resolution, and the pipeline/judge result contracts. SPEC-GR220 consumes `PathSelectionConfig`; SPEC-GR221 consumes `PassConfig`; SPEC-GR222 consumes resolved gates; SPEC-GR223 consumes `LoadedReviewContract`, `ResolvedReviewPolicy`, diagnostics, and the config hash. No Hilo graph schema changes are required; the implementation MUST add edges from the loader to the pipeline/judge/CLI consumers so a graph audit can find dead contract code.

## PANEL VERDICTS (binding amendments)

These decisions override conflicting text above and are the contract for GR-219.

1. **Versioned verdict envelope (D1).** Every contract-era verdict MUST carry top-level `contract_schema_version`, `verdict_schema_version: 2`, and `writer_version`; legacy records remain readable but MUST NOT claim schema equivalence. The same envelope is used by GR-220–223.
2. **Protected-base provenance (D4).** Resolve the protected ref to an immutable commit SHA, read `review.yaml` from that SHA, and persist the source commit and config hash. An absent file uses explicit versioned defaults. An unreadable or malformed file is `deny_and_report`, never silent defaults. `pr_policy` is outside v1 and MUST be removed from layer lists, types, examples, and tests; record it as deferred.
3. **Gate precedence (D2).** The canonical precedence is locked organization gates (`gates.security`) > pass-level gates (GR-221) > repository/path overlay gates (GR-220) > defaults. A lower layer may only add restrictions, never weaken a higher layer; `deny_and_report` always wins.
4. **Processing order (D3).** The shared review pipeline order is citation check, coverage downgrade, standards filter, then gate/outcome recomputation. GR-219 supplies the resolved policy and provenance consumed by that order.
5. **Snapshot and concurrency (panel A).** The resolved policy is immutable for a run and carries source commit, config hash, normalized-path digest, and changed-file/diff digest. Reject NFC path collisions before execution. Persistence and any supersede/reconcile operation MUST use a store-wide lock/atomic transaction; partial or stale writes are not successful verdicts.
6. **Contract-load rejection (panels B/C).** Unknown keys, unsupported layers, malformed YAML, and unsupported signal names are structured denials with diagnostics. Missing policy is distinguishable from unreadable policy.
7. **Wall-clock discipline (D9/panel C).** Tests assert exact policies, diagnostics, operation counts, and bounded records. Timeout tests use an injectable clock/fake scheduler; no tight elapsed-duration assertion is normative.
