# SPEC-GR199 — Tier 2 Second Opinion

## 1. Purpose

Add an optional, non-recursive second Tier 2 grading pass that independently re-grades only a primary Tier 2 FAIL, using a configured judge with a different provider/model lineage, fresh context, and the same evaluator tools, then records the comparison without allowing the second pass to block persistence.

### Requirements

- **R1.** The feature MUST run only after a primary Tier 2 verdict has failed the existing gate (`engine.gate.verdict_is_passing(primary, task.criteria) is False`); it MUST NOT run for a passing verdict, a Tier 1-only result, a skipped Tier 2, or a run with no Tier 2 verdict.  
  **test:** `test_second_opinion_runs_only_for_tier2_fail` exercises all four non-triggering shapes and one failing shape.  
  **check:** `Judge._run_second_opinion` has one explicit gate predicate and no unconditional invocation from `evaluate_task`.
- **R2.** The second pass MUST use a provider/model lineage different from the primary judge; a configured pair that resolves to the same provider and model MUST be rejected before an LLM call.  
  **test:** `test_same_lineage_is_rejected_without_call` asserts the fake client call count remains zero.  
  **check:** lineage comparison uses resolved provider and model, not only the user-supplied model string.
- **R3.** The second pass MUST be non-recursive: its evaluator MUST NOT invoke `Judge.evaluate_task`, `_run_second_opinion`, or another second-opinion hook.  
  **test:** `test_second_opinion_cannot_recurse` uses a spy Judge and asserts no nested hook call.  
  **check:** the implementation constructs one `AgenticEvaluator` directly and does not pass second-opinion configuration into that evaluator.
- **R4.** The second pass MUST receive the same task criteria, repository root, and evaluator tool definitions as the primary pass, but MUST receive no primary messages, sandbox, tool-record, or parsed verdict state.  
  **test:** `test_second_opinion_has_same_tools_and_fresh_context` compares tool names and asserts distinct evaluator state and message lists.  
  **check:** the second evaluator is constructed after the primary result and starts with a new `AgenticEvaluator` instance.
- **R5.** A provider outage, timeout, configuration error, or malformed second verdict MUST be recorded as an unavailable second opinion and MUST leave the primary result and persistence path usable.  
  **test:** `test_second_opinion_outage_does_not_block_persistence` makes the provider raise and verifies `persist_evaluation` is still called and succeeds.  
  **check:** the second-opinion boundary catches provider/config/parse exceptions and returns a structured result instead of raising.

## 2. Interface

All signatures below are normative. Use `from __future__ import annotations` and the existing `typing.Any` conventions.

### 2.1 Configuration and result types

```python
from dataclasses import dataclass
from typing import Any, Protocol

@dataclass(frozen=True)
class SecondOpinionConfig:
    provider: str = ""
    model: str = ""
    timeout_seconds: int = 120

    @property
    def enabled(self) -> bool: ...

@dataclass(frozen=True)
class SecondOpinionResult:
    model: str
    verdict: str  # COMPLETE | INCOMPLETE | UNAVAILABLE
    agreement: bool | None
    error: str | None = None

class SecondOpinionJudge(Protocol):
    def grade(
        self,
        task: dict[str, Any],
        *,
        primary_verdict: str,
        primary_items: list[dict[str, Any]],
    ) -> SecondOpinionResult: ...
```

`SecondOpinionConfig.enabled` is true exactly when `provider.strip()` and `model.strip()` are non-empty. `timeout_seconds` is a positive integer. A missing `second_opinion` block is disabled.

### 2.2 Required functions and methods

```python
# engine/config_schema.py
def load_second_opinion_config(config: dict[str, Any]) -> SecondOpinionConfig: ...

def validate_second_opinion_config(value: object, *, strict: bool = False) -> list[ConfigIssue]: ...

# engine/second_opinion.py
def run_second_opinion(
    *,
    primary: Verdict,
    task: dict[str, Any],
    workdir: str,
    scan_root: str,
    primary_provider: str,
    primary_model: str,
    config: SecondOpinionConfig,
    eval_cap: EvalCap | None = None,
) -> SecondOpinionResult: ...

# engine/judge.py
class Judge:
    def _run_second_opinion(
        self,
        result: "JudgeResult",
        task: Task,
        config: dict[str, Any],
    ) -> None: ...

# engine/judge.py
class JudgeResult:
    second_opinion: dict[str, Any] | None
```

`run_second_opinion` MUST create the independent client from `config.provider` and `config.model`, create a fresh `AgenticEvaluator`, call `evaluate(task)`, and return the normalized result. `primary_items` is a serialized snapshot used only for diagnostics; it MUST NOT be inserted into the second evaluator's conversation as evidence.

### 2.3 Provider adapter boundary

```python
class SecondOpinionProvider(Protocol):
    provider_name: str
    model_name: str

    def evaluate(
        self,
        task: dict[str, Any],
        *,
        workdir: str,
        scan_root: str,
        eval_cap: EvalCap | None,
        timeout_seconds: int,
    ) -> Verdict: ...
```

The first implementation MUST provide an `LLMClient` adapter for every provider accepted by `second_opinion.provider` (`openai`, `anthropic`, and `local`). `local` MUST use the repository's configured local OpenAI-compatible endpoint; it MUST not shell out through a command string. Provider credentials MUST use the existing credential resolution and MUST never be serialized.

## 3. Data Model

### 3.1 Configuration schema

The accepted `.gitreins/config.yaml` shape is:

```yaml
second_opinion:
  provider: anthropic
  model: claude-3-7-sonnet
  timeout_seconds: 120
```

`engine/config_schema.py` MUST add the top-level section and exact keys:

```python
_SECOND_OPINION: dict[str, KeySpec] = {
    "provider": KeySpec("str"),
    "model": KeySpec("str"),
    "timeout_seconds": KeySpec("int"),
}
```

The section MUST be registered in `_SECTIONS` as `"second_opinion": _SECOND_OPINION`. Unknown nested keys warn under normal validation and become errors under `strict=True`, matching the existing schema policy. `timeout_seconds < 1` is invalid; an invalid or absent block disables the feature and emits the existing config issue rather than crashing a judge run.

### 3.2 Persisted verdict JSON

When the second pass is not invoked, the `second_opinion` key is absent. When it is invoked, `build_verdict_data()` MUST add exactly this top-level object:

```json
{
  "second_opinion": {
    "model": "claude-3-7-sonnet",
    "verdict": "INCOMPLETE",
    "agreement": false
  }
}
```

The object has exactly three persisted keys: `model` (resolved model identifier), `verdict` (`COMPLETE`, `INCOMPLETE`, or `UNAVAILABLE`), and `agreement` (`true`/`false` for a completed comparison, `null` for `UNAVAILABLE`). The provider, endpoint, API key, error text, prompts, and tool transcript MUST NOT be persisted in this object. An unavailable result MAY be logged through the existing logger but MUST not include credentials.

Agreement is computed as `primary.verdict == second.verdict`. A second `COMPLETE` is agreement only when the primary is also `COMPLETE`; because the hook runs only for a failed primary, a successful second `COMPLETE` produces `agreement: false`.

### 3.3 Result ownership

`JudgeResult.second_opinion` holds the exact three-key dictionary before persistence. `build_verdict_data()` copies it without recomputing the comparison. The existing primary `passed`, `tier1`, `tier2`, `pipeline_result`, `items`, `failure_modes`, and `summary` values remain unchanged by this feature.

### Requirements

- **R6.** The config schema MUST recognize exactly `second_opinion.provider`, `second_opinion.model`, and `second_opinion.timeout_seconds`, with the defaults and validation above.  
  **test:** `test_config_schema_accepts_second_opinion_and_rejects_unknown_nested_key` parses the YAML fixture and checks issue paths.  
  **check:** `known_top_level()` contains `second_opinion`, and `known_keys("second_opinion")` returns the three keys.
- **R7.** A successful invocation MUST persist exactly `verdict.second_opinion = {model, verdict, agreement}` with no credential or endpoint fields.  
  **test:** `test_build_verdict_data_persists_second_opinion_shape` compares the exact key set and values.  
  **check:** `build_verdict_data` copies `JudgeResult.second_opinion` and does not call a provider.
- **R8.** An unavailable second opinion MUST use `verdict: "UNAVAILABLE"` and `agreement: null`, while the primary verdict remains authoritative for the existing pass/fail fields.  
  **test:** `test_unavailable_second_opinion_is_additive` asserts primary fields and persistence output are unchanged apart from the new object.  
  **check:** no outage branch sets `result.passed` or rewrites primary items.

## 4. Wiring

1. `Judge.evaluate_task()` runs the existing pipeline/legacy path first.
2. It resolves the tier-2 `Verdict` from `result.tier2`, `result.verdict`, or the existing `_find_pipeline_verdict(result.pipeline_result)`. If no verdict exists, it returns without invoking the feature.
3. It loads raw config with `engine.config.load_raw_config(self.workdir)` and calls `load_second_opinion_config(raw_config)`.
4. It calls `_run_second_opinion()` only when the existing `verdict_is_passing(primary, task.criteria)` is false and the configuration is enabled.
5. `_run_second_opinion()` obtains primary provider/model identity from the actual primary `LLMClient` using the existing credential-free provenance fields. It compares lineage, then calls `run_second_opinion()`.
6. The returned three-key dictionary is attached to `JudgeResult.second_opinion`.
7. The existing persistence caller invokes `build_verdict_data()` and `VerdictPersister.persist()` exactly as before. The hook sits before `persist_evaluation()` builds/writes the payload; it does not write history itself.
8. The pipeline path and legacy path use the same hook. A `skip_tier2=True` run never reaches it.

### Requirements

- **R9.** The hook MUST sit after Tier 2 verdict extraction and before `persist_evaluation()`/`VerdictPersister.persist()` writes `verdict.json`.  
  **test:** `test_second_opinion_runs_before_persist` records event order `tier2`, `second_opinion`, `build_verdict_data`, `persist`.  
  **check:** no second-opinion code is placed in `engine.persist.py` after the file write.
- **R10.** Both modern pipeline and legacy judge results MUST use the same trigger and serialized shape.  
  **test:** `test_pipeline_and_legacy_failures_get_same_second_opinion_shape`.  
  **check:** the implementation has one hook, not path-specific duplicate logic.
- **R11.** The feature MUST never recurse through the judge or through a configured pipeline stage.  
  **test:** `test_second_opinion_uses_one_fresh_agentic_evaluator`.  
  **check:** the second evaluator receives no second-opinion config and no Judge object.

## 5. Error Catalog

| Code | Condition | Required behavior |
|---|---|---|
| `GR199_DISABLED` | Missing/empty provider or model | Do not invoke; omit `second_opinion`. |
| `GR199_SAME_LINEAGE` | Resolved provider and model equal primary lineage | Do not invoke; record `UNAVAILABLE` with `agreement: null`; log a redacted configuration error. |
| `GR199_PROVIDER_UNREACHABLE` | Network, local endpoint, or provider authentication failure | Record `UNAVAILABLE`; preserve primary; continue to persistence. |
| `GR199_TIMEOUT` | Second evaluator exceeds `timeout_seconds` | Record `UNAVAILABLE`; terminate the second request through the existing bounded client path; continue. |
| `GR199_PARSE` | Second model returns no valid `Verdict` | Record `UNAVAILABLE`; continue. |
| `GR199_CONFIG` | Malformed second-opinion block | Disable the hook for this run and emit the existing config warning/error; do not crash. |
| `GR199_PERSIST` | Failure while copying the additive result | Preserve the primary result and let the existing persistence error policy handle the write; never convert primary FAIL to PASS. |

### Requirements

- **R12.** Every error path MUST preserve primary verdict authority and persistence availability.  
  **test:** `test_each_gr199_error_path_preserves_primary_and_persistability` parametrizes outage, timeout, parse, same-lineage, and malformed config.  
  **check:** error handlers do not mutate `primary.items`, `primary.verdict`, or `result.passed`.
- **R13.** Error messages and logs MUST omit API keys, authorization headers, full endpoint URLs, prompts, and raw provider responses.  
  **test:** `test_second_opinion_errors_are_redacted`.  
  **check:** persisted JSON is scanned for configured secret values and endpoint credentials.

## 6. Edge Cases

- A primary with only `NOT_VERIFIABLE` items is a failed gate under GR-178 and triggers the second opinion; the second verdict is compared by top-level verdict, not by string similarity of details.
- If the two judges disagree on `COMPLETE` versus `INCOMPLETE`, `agreement` is `false`; the primary remains the merge-gate decision and the second opinion is advisory evidence.
- If both judges return `INCOMPLETE`, `agreement` is `true`; both sets of findings are not merged, and only the primary items remain the authoritative `items` list.
- A primary Tier 1 failure with no Tier 2 verdict does not trigger the feature.
- A primary verdict with zero criteria/items is not eligible unless the existing gate explicitly returns non-passing; this prevents a malformed no-evidence result from creating an unbounded extra call.
- Provider/model strings are compared after trimming and case-folding for identity only; the original resolved model identifier is persisted.
- Deleted files, files outside the diff, untracked files, and a clean index do not change second-opinion behavior: the second evaluator receives the same `scan_root` and tool scope as the primary.
- Concurrent judges each own an independent second evaluator and client; no mutable module-global second-opinion state is allowed.

### Requirements

- **R14.** NOT_VERIFIABLE disagreement MUST be represented only by the two verdict values and `agreement`; the implementation MUST NOT silently upgrade a NOT_VERIFIABLE item to PASS or merge second-pass details into primary items.  
  **test:** `test_not_verifiable_disagreement_is_advisory_only`.  
  **check:** primary item statuses are byte-for-byte unchanged after the hook.
- **R15.** A second opinion MUST use the same `scan_root`, including deleted-file and outside-diff context rules, as the primary evaluator.  
  **test:** `test_second_opinion_preserves_scan_root_for_deleted_and_context_files`.  
  **check:** the adapter receives `scan_root=self.scan_root` exactly.

## 7. Testing

Tests belong in the existing unit/integration test layout; this spec does not require real provider calls in the default suite.

| Scenario | Test and expected assertion |
|---|---|
| Trigger only on Tier 2 FAIL | `test_second_opinion_runs_only_for_tier2_fail`: one call for failed Tier 2, zero for pass/Tier 1-only/skip. |
| Different lineage | `test_same_lineage_is_rejected_without_call`: no client call and `UNAVAILABLE`. `test_different_provider_model_is_accepted`: one fresh evaluator call. |
| Fresh context | `test_second_opinion_has_fresh_context`: no primary transcript/sandbox/messages shared. |
| Same tools | `test_second_opinion_has_same_tools_and_fresh_context`: tool-name sets equal. |
| Pipeline and legacy | `test_pipeline_and_legacy_failures_get_same_second_opinion_shape`. |
| Persistence | `test_build_verdict_data_persists_second_opinion_shape`: exact three-key object. |
| Outage | `test_second_opinion_outage_does_not_block_persistence`: persistence succeeds with `UNAVAILABLE`. |
| Timeout | `test_second_opinion_timeout_is_unavailable`: timeout result, no leaked child process. |
| Malformed reply | `test_second_opinion_parse_failure_is_unavailable`. |
| NOT_VERIFIABLE | `test_not_verifiable_disagreement_is_advisory_only`. |
| No recursion | `test_second_opinion_cannot_recurse`. |
| Secret safety | `test_second_opinion_errors_are_redacted`. |

Negative controls MUST include: a primary PASS that would fail if the hook were unconditional; a same-provider/same-model pair that would fail if lineage were not enforced; a fake second evaluator that calls the judge hook recursively; and a provider exception that would fail if persistence were coupled to the second pass.

### Requirements

- **R16.** The test suite MUST include every scenario and negative control in this section, with provider calls mocked and call counts asserted.  
  **test:** `pytest -q tests/test_second_opinion.py tests/test_config_schema.py tests/test_persist.py tests/test_judge.py`.  
  **check:** the test file contains named tests for trigger, lineage, fresh context, same tools, persistence, outage, timeout, parse, NOT_VERIFIABLE, recursion, and redaction.
- **R17.** The implementation MUST pass the repository's targeted tests and the full non-LLM suite.  
  **test:** `pytest -q tests/test_second_opinion.py tests/test_config_schema.py tests/test_persist.py tests/test_judge.py` and `pytest -q -m 'not llm'`.  
  **check:** both commands exit 0; no test enables a real external provider by default.

## 8. Hilo Impact

**Depends on:** `engine/judge.py` for trigger placement and verdict extraction; `engine/evaluator.py` for `Verdict`, `VerdictItem`, tool definitions, and independent evaluator construction; `engine/llm.py` for provider/model identity and client creation; `engine/config.py`/`engine/config_schema.py` for raw configuration and validation; `engine/gate.py` for the existing pass predicate; `engine/persist.py` for additive serialization.

**Changes consumed by:** CLI judge, MCP judge/status, history reports, and any caller reading `JudgeResult` or persisted verdict JSON. Existing consumers ignore the additive `second_opinion` key.

**Hilo invariants:** the primary verdict remains authoritative; no new persistence store exists; no provider credential enters a verdict; second opinion is opt-in and bounded; the hook is one-way from Judge to a fresh evaluator and never back into Judge.

### Requirements

- **R18.** The implementation MUST preserve backward compatibility when `second_opinion` is absent: no extra provider call and no `second_opinion` key.  
  **test:** `test_disabled_second_opinion_preserves_historical_shape`.  
  **check:** compare serialized output for an otherwise identical run with the feature absent.

## PANEL VERDICTS (binding amendments)

The following decisions incorporate the write-safety, schema, and consumer panel verdicts. They are normative amendments and override any conflicting wording above.

1. **Canonical versioned verdict envelope (D1; schema-panel finding).** Every newly written verdict envelope, including one that carries `second_opinion`, MUST contain top-level `schema_version: 2` and a non-empty `writer_version` identifying the GitReins writer/build. These fields are part of the canonical persisted verdict schema, not the `second_opinion` object. Readers MUST accept the prior unversioned/legacy shape where compatibility is required, while all new writes use version 2. `test_versioned_verdict_envelope_has_schema_and_writer` MUST cover both the new write and legacy read path.

2. **Distinct names and ordering (D2; schema/consumer-panel finding).** `second_opinion` is RESERVED for this independent Tier 2 re-grader on a final FAIL. The existing `review.second_pass` reviewer is a separate feature; both MAY run in one job. Ordering is mandatory: run `review.second_pass` first, derive the final authoritative Tier 2 result, then run `second_opinion` only on that final result when the trigger below is satisfied. Neither feature may overwrite or be serialized as the other feature. `test_second_pass_precedes_second_opinion_and_shapes_remain_distinct` MUST assert ordering and both serialized namespaces.

3. **Single provider-resolution boundary (D3; schema-panel finding).** The implementation MUST use a provider-resolution factory around the existing `engine/llm.py` `LLMClient`. The factory MUST be the same resolver used by the primary and second-opinion clients; a parallel provider adapter fork is forbidden. V1 accepts only the providers that the existing client supports (`openai` and `anthropic`); `local` is explicitly out of scope and MUST be rejected as unsupported rather than implemented through a shell or local adapter. `base_url` selection and credential resolution MUST be explicit at the resolver boundary, and credentials, authorization headers, and secret-bearing URLs MUST never enter verdict data or diagnostics. `test_disabled_config_constructs_no_second_provider` MUST prove that a disabled/malformed configuration creates no extra provider client.

4. **Exact trigger and actual Tier 2 hook (D4; consumer-panel finding).** The hook MUST require both `(engine.gate.verdict_is_passing(primary, task.criteria) is False)` and an explicit `tier2_actually_ran`/equivalent execution marker. The marker MUST be false for Tier 1-only, skipped, and absent Tier 2 results; it MUST not be inferred from a default or fabricated verdict object. The regression battery MUST cover PASS, Tier-1-only, skipped, absent, and actual Tier-2 FAIL, with zero second-opinion calls for the first four. The second opinion runs after the second pass and only against the final FAIL.

5. **One immutable snapshot and atomic persistence (D5; write-safety-panel finding).** The primary verdict, any second-pass result, the second-opinion result, and the versioned envelope MUST be assembled as one immutable verdict snapshot before persistence. The second opinion is part of that same snapshot and MUST NOT perform a second history write, rewrite, or independent `verdict.json` update. Persistence MUST use unique temporary files, `fsync`, and atomic `os.replace`; a failed second opinion MUST leave the primary snapshot authoritative and persistable.

6. **Lock scopes are mandatory (D5; write-safety-panel finding).** Verdict history writes MUST hold a per-workdir history lock across reconciliation and final replace. Supersede/reconciliation work MUST additionally use a per-job supersede lock so concurrent judged runs cannot both rewrite the same history entries. The lock protocol MUST define acquisition failure/timeout diagnostics and tests MUST use separate processes or equivalent concurrent writers to prove no lost or partially written verdict occurs.

7. **Deterministic timeout/error tests (consumer-panel finding).** Timeout coverage MUST use an injected clock or deterministic fake client/bounded transport signal, not host-load-sensitive wall-clock assertions. Provider outage, timeout, parse, same-lineage, unsupported-provider, and malformed-config tests MUST all prove that the primary result remains unchanged and the single snapshot still reaches persistence.

8. **Consumer-visible compatibility contract (schema/consumer-panel finding).** `build_verdict_data()` and every status/history consumer MUST read the same versioned snapshot and preserve the additive three-key `second_opinion` payload. When the feature is disabled, the provider-resolution factory is not constructed and the historical payload remains unchanged except for the required version-2 envelope fields.
