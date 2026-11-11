# SPEC-GR222 — Composable Gates, Verdicts, and Pre-Commit Exit Codes

## 1. Purpose

Define composable finding and policy gates that block only on the configured predicate, preserve baseline/out-of-range exceptions, and feed the persisted verdict and pre-commit exit code from one decision.

**Normative requirements**

- **R1** A finding gate MUST evaluate severity, confidence, diff introduction, changed-line status, dataflow exception, and disposition as typed predicates. `test: test_composable_block_predicate`.
- **R2** The v1 block predicate is `severity_at_least AND confidence_at_least AND introduced_by_diff AND disposition == unresolved`. `test: test_block_predicate_requires_all_terms`.
- **R3** `baseline_existing` MUST NOT block. `outside_changed_lines` MUST NOT block unless `direct_dataflow_from_changed_code` is true. `test: test_baseline_and_outside_line_exceptions`.
- **R4** Gate evaluation MUST be persisted in the verdict and the pre-commit hook MUST use that same result for its exit code. `test: test_verdict_and_hook_share_gate_result`.
- **R5** A contract-load error, unavailable required pass, failed required pass, or locked-gate conflict is deny-and-report and cannot become a passing gate through `allow_skips`. `test: test_gate_fail_closed_on_infrastructure_error`.

## 2. Interface

Implementation belongs in `engine/review_gates.py`; the judge and pre-commit integrations use the same pure evaluator.

```python
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Literal

Severity = Literal["critical", "high", "medium", "low", "trivial", "info", "observation"]
Disposition = Literal["unresolved", "resolved", "accepted", "suppressed", "baseline_existing"]

@dataclass(frozen=True)
class FindingEvidence:
    file: str
    line: int
    end_line: int
    severity: Severity
    confidence: float
    introduced_by_diff: bool
    outside_changed_lines: bool
    baseline_existing: bool
    direct_dataflow_from_changed_code: bool
    disposition: Disposition
    category: str
    pass_id: str
    evidence: tuple[str, ...] = ()

@dataclass(frozen=True)
class GatePredicate:
    severity_at_least: Severity | None = None
    confidence_at_least: float | None = None
    introduced_by_diff: bool | None = None
    disposition: Disposition | None = None
    require: tuple[str, ...] = ()

@dataclass(frozen=True)
class GateConfig:
    finding_policy: Mapping[str, object]
    security: Mapping[str, object]
    path_rules: tuple[Mapping[str, object], ...] = ()

@dataclass(frozen=True)
class GateEvaluation:
    gate_id: str
    passed: bool
    blocking: bool
    action: Literal["allow", "request_changes", "fail", "deny_and_report"]
    reasons: tuple[str, ...]
    finding_ids: tuple[str, ...]

@dataclass(frozen=True)
class VerdictGateResult:
    passed: bool
    disposition: Literal["allow", "request_changes", "fail", "deny_and_report"]
    evaluations: tuple[GateEvaluation, ...]
    blocking_finding_ids: tuple[str, ...]
    diagnostics: tuple["ReviewDiagnostic", ...]

def finding_matches_block_predicate(
    finding: FindingEvidence,
    predicate: GatePredicate,
) -> bool: ...

def evaluate_gates(
    gates: GateConfig,
    *,
    findings: Sequence[FindingEvidence],
    pass_results: Sequence["PassResult"],
    policy_diagnostics: Sequence["ReviewDiagnostic"] = (),
) -> VerdictGateResult: ...

def precommit_exit_code(result: VerdictGateResult) -> int: ...
```

`finding_matches_block_predicate` is pure and must implement the exceptions before returning a block match:

```python
if finding.baseline_existing:
    return False
if finding.outside_changed_lines and not finding.direct_dataflow_from_changed_code:
    return False
return (
    severity_at_least(finding.severity, predicate.severity_at_least)
    and confidence_at_least(finding.confidence, predicate.confidence_at_least)
    and finding.introduced_by_diff is True
    and finding.disposition == "unresolved"
)
```

The implementation MUST reject confidence outside `[0.0, 1.0]`, unknown severities/dispositions, and unknown predicate names. Severity order is `observation < info < trivial < low < medium < high < critical` for threshold comparison; the canonical output preserves the original severity word.

`precommit_exit_code` MUST return: `0` for `allow`, `1` for an ordinary blocking gate (`request_changes` or `fail`), and `2` for `deny_and_report` or an incomplete required pass. This is the review-contract result code; existing unrelated guard result codes remain unchanged when no review contract is loaded.

## 3. Data Model

The normative gate YAML is:

```yaml
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
```

The normalized v1 predicate is explicit even when YAML uses the compact design shape:

```python
@dataclass(frozen=True)
class NormalizedFindingPolicy:
    block: GatePredicate = GatePredicate(
        severity_at_least="high",
        confidence_at_least=0.85,
        introduced_by_diff=True,
        disposition="unresolved",
    )
    baseline_existing_exempt: bool = True
    outside_changed_lines_exempt_without_direct_dataflow: bool = True
```

Every finding trace MUST carry the gate evaluation that considered it:

```python
@dataclass(frozen=True)
class FindingTrace:
    pass_id: str
    matched_layers: tuple[str, ...]
    matched_globs: tuple[str, ...]
    appended_instructions: tuple[str, ...]
    effective_model: str
    effective_tools: tuple[str, ...]
    config_hash: str
    diff_range: tuple[str, int, int]
    gate_evaluation: GateEvaluation
```

Persisted verdict data adds a top-level `review_contract` block and per-item `trace` block without deleting existing `engine.persist.build_verdict_data` keys:

```json
{
  "review_contract": {
    "config_hash": "sha256:...",
    "source_ref": "origin/main",
    "source_commit": "<base-sha>",
    "passed": false,
    "disposition": "request_changes",
    "gates": [{"gate_id": "finding_policy", "passed": false, "blocking": true, "action": "request_changes", "reasons": ["finding f-1 meets block predicate"]}]
  },
  "items": [{"criterion": "...", "trace": {"pass_id": "security", "gate_evaluation": {"blocking": true}}}]
}
```

The actual persisted schema MUST retain existing task/pipeline/stage/item fields and add these keys only when the review contract ran. A contract denial still writes a verdict record when persistence is enabled, with the diagnostics and `exit_code: 2`. If persistence fails after an in-memory green result, the command exits non-zero and emits no pass/green claim.

## 4. Wiring

1. SPEC-GR221 emits findings and pass outcomes. SPEC-GR220 supplies path-rule context. SPEC-GR219 supplies policy diagnostics and locked-field results.
2. `engine/judge.py:Judge.evaluate_task` calls one final decision function after all triggered pass results are collected. That function combines Tier 1, Tier 2, contract gates, `pass_class`, and required-pass status; its typed result is the only authority for `JudgeResult.passed`, persistence, and the hook exit code.
3. `engine/persist.py:build_verdict_data` serializes `review_contract`, gate evaluations, blocking finding IDs, and each finding's trace. Existing verdict keys remain backward-compatible.
4. The pre-commit hook invokes the same review gate evaluation through the guard/judge command. It MUST NOT parse human summary text to decide whether to commit.
5. The CLI returns `precommit_exit_code(gate_result)`. `0` permits commit, `1` rejects a normal finding/gate failure, and `2` rejects an unverifiable/contract denial. The stdout summary may say `PASS`, `FAIL`, or `DENY_AND_REPORT`, but exit status is authoritative.
6. `allow_skips` can permit an existing guard's degraded no-test state only where that guard already allows it; it cannot waive review contract denial, a locked security gate, or a required triggered pass.

## 5. Error Catalog

| Code | Trigger | Behavior |
|---|---|---|
| `REVIEW_GATE_UNKNOWN_SEVERITY` | Threshold or finding severity is unknown | Deny-and-report; no lexical fallback. |
| `REVIEW_GATE_CONFIDENCE_RANGE` | Confidence is not numeric in `[0,1]` | Deny-and-report. |
| `REVIEW_GATE_UNKNOWN_PREDICATE` | Unknown predicate or `unless` rule | Deny-and-report. |
| `REVIEW_GATE_MISSING_EVIDENCE` | Required evidence predicate cannot be evaluated | Gate is `deny_and_report`, exit 2. |
| `REVIEW_GATE_BASELINE_CONFLICT` | Finding marked both baseline-existing and introduced-by-diff without provenance | Deny-and-report; do not choose one. |
| `REVIEW_GATE_REQUIRED_PASS_UNAVAILABLE` | Required pass unavailable, disabled, or timed out | Gate fails with exit 2 and names pass/reason. |
| `REVIEW_GATE_REQUIRED_PASS_INCOMPLETE` | Required pass exhausted budget or returned invalid output | Gate fails with exit 2. |
| `REVIEW_GATE_LOCKED_VIOLATION` | Locked gate was weakened by a lower layer | Deny-and-report. |
| `REVIEW_GATE_PERSISTENCE_FAILURE` | Verdict cannot be persisted | Command fails non-zero and MUST NOT claim a green result. |

## 6. Edge Cases

- A high-severity finding with confidence `0.84` does not block a `0.85` threshold; equality does block.
- A baseline-existing finding never blocks, even if it is on a changed line; its trace records the exemption.
- An outside-changed-line finding does not block unless direct dataflow from changed code is proven. Direct dataflow is a typed evidence fact, not a model assertion in free text.
- A finding with `disposition=accepted` or `suppressed` does not block; the trace retains the disposition and gate reason.
- Multiple gates are composable: all gate evaluations are recorded, and any blocking gate makes the aggregate non-passing. The strongest action is `deny_and_report` > `fail`/`request_changes` > `allow`.
- No findings does not bypass a required pass gate. A pass marked unavailable is not equivalent to an empty finding list.
- A 1,000+ file diff emits bounded per-finding gate records and aggregates blocking IDs without changing predicate semantics. Tests assert bounded records and exact match sets, not tight elapsed durations.
- Existing verdicts without `review_contract` remain readable; they are not retroactively judged under the new policy.

## 7. Testing

1. Test each predicate term independently and in combination; assert one missing term prevents blocking.
2. Test severity ordering, exact threshold equality, confidence bounds, disposition values, and invalid types.
3. Test baseline-existing, outside-changed-lines without dataflow, and outside-changed-lines with direct dataflow.
4. Test a high-confidence high-severity introduced unresolved finding blocks; resolved/accepted/suppressed variants do not.
5. Test security `locked: true`, migration path rules, and `on_fail` action mapping.
6. Test required pass statuses: completed, disabled, unavailable, timeout, budget exhausted, invalid output; assert only completed can satisfy the requirement.
7. Test multi-gate aggregation and deterministic evaluation order.
8. Run the **verdict/exit-code identity test**: build one failing gate, call `evaluate_gates` through judge and pre-commit paths, assert identical serialized gate evaluations, `passed=False`, and exit `1`; repeat with contract denial and assert exit `2`.
9. Persist a verdict and read it back; assert `review_contract.config_hash`, base source commit, gate results, blocking IDs, and item traces are present without removing legacy fields.
10. Run a 1,200-file fixture and assert aggregate result is stable and bounded.
11. Test persistence failure and assert a non-zero command result with no success claim.
12. Run focused tests, `ruff check`, and the repository test command.

## 8. Hilo Impact

This spec depends on SPEC-GR219's merge/lock diagnostics and SPEC-GR221's pass result contract. It feeds `engine/judge.py`, `engine/persist.py`, the pre-commit hook, and SPEC-GR223's gate explanation. Hilo must connect `evaluate_gates` to both the judge and hook callers; a gate implementation wired to only one surface is non-conforming.

## PANEL VERDICTS (binding amendments)

These decisions override conflicting text above and are the contract for GR-222.

1. **One final decision function (D7).** Define one typed final decision function combining Tier 1, Tier 2, contract gates, `pass_class`, and required-pass statuses. Its result is the sole authority consumed by `JudgeResult`, persisted verdict fields, and the pre-commit hook exit code. Never infer status from summary text, `merge_recommendation`, or an advisory field.
2. **Gate precedence (D2).** Evaluate locked organization gates (`gates.security`) first, then pass-level gates, then repository/path overlay gates, then defaults. Lower layers may only add restrictions; they cannot weaken higher layers. `deny_and_report` always wins regardless of evaluation order.
3. **Persistence failure (D7).** If persistence fails after a green in-memory decision, return a non-zero exit and emit no pass/green claim, label, or success receipt. A persisted claim requires a successful atomic write/readback.
4. **Gate digest (D7).** Attach `source_commit`, worktree fingerprint, policy hash, and run id as a gate digest to `JudgeResult`, the persisted verdict, and the pre-commit hook receipt. The digest binds the decision to the evaluated tree and policy.
5. **Processing order (D3).** Recompute gates/outcomes only after citation check, coverage downgrade, and standards filtering, in that order. All pass output must satisfy GR-222's typed predicate fields.
6. **Versioned envelope (D1).** Contract-era verdicts carry `contract_schema_version`, `verdict_schema_version: 2`, and `writer_version`; old records remain readable but never claim equivalence. Existing `pass_class` semantics remain represented, but cannot override the final decision function.
7. **Write-safety and tests (panels A/C/D9).** Use one immutable snapshot and atomic/locked persistence. Tests assert predicate truth tables, exact gate evaluations, exit codes, digests, and bounded records; timeout paths use an injectable clock/fake scheduler and no tight elapsed-duration assertions.
