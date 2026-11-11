# SPEC-GR221 — Typed Triggered Passes and Deterministic Diff Signals

## 1. Purpose

Define typed review passes that run only when their path or deterministic diff-signal triggers match, with explicit execution budgets, tools, gates, output, and exact v1 signal detectors.

**Normative requirements**

- **R1** Every pass MUST have a unique `id`, `enabled`, typed trigger, typed execution, and typed output contract. `test: test_pass_schema_requires_all_contract_sections`.
- **R2** A pass trigger is OR across `paths` and `diff_signals` within `trigger.any`; a pass with no trigger is always eligible within its scope. `test: test_trigger_or_semantics`.
- **R3** Diff-signal detection MUST be deterministic regex/AST scanning; no LLM, model, network, or tool execution may decide whether a signal fired. `test: test_diff_signals_are_deterministic`.
- **R4** v1 MUST implement exact detectors for `authentication_call`, `deserialization`, `unbounded_query`, and `public_signature_change`. `test: test_required_signal_fixture_matrix`.
- **R5** A pass that is unavailable or exceeds budget MUST be recorded as unavailable/partial and MUST NOT be represented as completed. `test: test_unavailable_and_budget_outcomes`.

## 2. Interface

Implementation belongs in `engine/review_passes.py`; the pass runner integrates with `engine/pipeline.py` and `engine/judge.py`.

```python
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

SignalName = Literal[
    "authentication_call", "deserialization", "unbounded_query", "public_signature_change",
    "authorization_check", "secret_access", "shell_execution", "schema_change",
    "new_loop_over_remote_data", "n_plus_one_candidate", "lock_scope_change",
]

@dataclass(frozen=True)
class TriggerSpec:
    paths: tuple[str, ...] = ()
    diff_signals: tuple[SignalName, ...] = ()

@dataclass(frozen=True)
class ExecutionSpec:
    model: str
    tools: tuple[str, ...] = ()
    budget: int = 0
    timeout_seconds: int = 0

@dataclass(frozen=True)
class OutputSpec:
    finding_types: tuple[str, ...] = ()
    blocking_severities: tuple[str, ...] = ()
    labels_add: tuple[str, ...] = ()

@dataclass(frozen=True)
class PassConfig:
    id: str
    enabled: bool
    scope_include: tuple[str, ...]
    trigger: TriggerSpec | None
    instructions: tuple[str, ...]
    execution: ExecutionSpec
    gates: Mapping[str, object]
    output: OutputSpec

@dataclass(frozen=True)
class DiffSignalHit:
    name: SignalName
    path: str
    line: int
    end_line: int
    detector: str
    evidence: str

@dataclass(frozen=True)
class DiffSignals:
    hits: tuple[DiffSignalHit, ...]

class DiffSignalDetector(Protocol):
    def scan(
        self,
        *,
        base_tree: str,
        head_tree: str,
        changed_files: Sequence[str],
        changed_lines: Mapping[str, set[int]],
    ) -> DiffSignals: ...

@dataclass(frozen=True)
class ResolvedPass:
    config: PassConfig
    triggered: bool
    trigger_reasons: tuple[str, ...]
    signal_hits: tuple[DiffSignalHit, ...]
    status: Literal["available", "unavailable", "suppressed", "partial"]
    unavailable_reason: str | None = None

def detect_diff_signals(
    base_tree: str,
    head_tree: str,
    changed_files: Sequence[str],
    changed_lines: Mapping[str, set[int]],
) -> DiffSignals: ...

def resolve_passes(
    passes: Sequence[PassConfig],
    *,
    eligible_files: Sequence[str],
    changed_files: Sequence[str],
    changed_lines: Mapping[str, set[int]],
    signals: DiffSignals,
) -> tuple[ResolvedPass, ...]: ...

def run_pass(
    resolved: ResolvedPass,
    *,
    diff: str,
    policy: "ResolvedReviewPolicy",
) -> "PassResult": ...
```

`budget` is a maximum number of model-output tokens for the pass; `timeout_seconds` is wall-clock seconds. Both MUST be positive for an enabled triggered pass. The execution adapter MUST pass only the declared tools and MUST use a process-group timeout. The result includes actual consumption and an explicit stop reason.

## 3. Data Model

The normative pass examples are:

```yaml
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
        diff_signals: [authentication_call, authorization_check, secret_access, deserialization, shell_execution]
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
```

The four mandatory signal detectors are exact and deterministic:

### `authentication_call`

For every changed Python file, parse the changed file with `ast`. Emit a hit on the `lineno` of a `Call` whose callable is:

- a bare name in `{authenticate, auth, login, sign_in, signin, verify_token, check_password}`;
- an attribute whose final name is one of `{authenticate, login, sign_in, signin, verify_token, check_password}`; or
- a call whose attribute chain contains an attribute exactly named `authenticate` or `verify_token`.

For all file types, also apply this exact case-sensitive regex to added diff lines only: `\b(?:authenticate|auth|login|sign[_-]?in|signin|verify[_-]?token|check[_-]?password)\s*\(`. A line is reported once per signal, with detector `auth-call-regex` when AST parsing is unavailable. Deletions do not trigger the signal.

### `deserialization`

For Python AST calls, emit a hit for:

- `pickle.load`, `pickle.loads`, `cPickle.load`, `cPickle.loads`;
- `yaml.load`, `yaml.unsafe_load`, `yaml.full_load`;
- `marshal.load`, `marshal.loads`; or
- `shelve.open`.

The AST detector does not infer safety from arguments; the call itself is the signal. For added diff lines in any language, also apply this exact regex: `\b(?:pickle|cPickle|marshal)\s*\.\s*(?:load|loads)\s*\(|\byaml\s*\.\s*(?:load|unsafe_load|full_load)\s*\(`. A hit records the changed line and exact matched text.

### `unbounded_query`

For Python AST, emit a hit when an added or modified `Call` has an attribute in `{all, fetchall, fetch_all}` and its receiver is not a literal list/tuple and the call has no preceding bound method in the same expression chain from `{limit, slice, paginate, fetchmany, fetch_many}`. Also emit for a call to `Query`/`query`/`select` whose chain has no `limit`/`paginate` before terminal `{all, fetchall, fetch_all}`.

For added diff lines in any language, apply these exact regexes (one hit per line per regex):

- SQL terminal without a limit: `\b(?:SELECT|select)\b(?![^\n;]*\b(?:LIMIT|limit|TOP|top)\b)[^\n;]*\b(?:execute|query|raw|sql)\s*\(`
- ORM terminal: `\.(?:all|fetchall|fetch_all)\s*\(\s*\)`

The detector never flags a query solely because it contains `SELECT`; it requires one of the two exact patterns or the AST terminal rule.

### `public_signature_change`

For changed Python files, parse both base and head ASTs. For every top-level or class-member definition whose name does not start with `_`, compare:

- `FunctionDef`/`AsyncFunctionDef`: positional-only count, positional-or-keyword names/count, vararg presence, keyword-only names/default presence, kwarg presence, and return annotation source; and
- `ClassDef`: base-name tuple and public method signature map.

Emit a hit on the first changed line of the definition when any compared field differs, or when a new public definition is added/removed. For diff lines in Go/TypeScript/JavaScript, apply these exact added-line regexes:

- Go: `^\+\s*func\s+(?:\([^)]*\)\s*)?([A-Z][A-Za-z0-9_]*)\s*\(([^)]*)\)`
- TypeScript/JavaScript: `^\+\s*(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(([^)]*)\)|^\+\s*(?:export\s+)?(?:class|interface|type)\s+([A-Z][\w$]*)`

A regex hit is a `public_signature_change` only when the same declaration's normalized name/signature differs from base or is absent in base; an added public declaration with no base counterpart is a hit. Formatting-only changes are normalized away. The detector does not treat a private name as public.

All hit ordering is `(path, line, signal name, detector, evidence)`. Signal enums not implemented by v1 MUST be rejected rather than silently treated as always false. The design YAML may list them; their implementation is a separate required pass task only when the schema marks them `implemented: true`.

## 4. Wiring

1. SPEC-GR219 resolves the merged pass list and path scope, then `detect_diff_signals` runs once per review before pass resolution.
2. SPEC-GR220 supplies eligible paths and overlay-triggered context. A path match alone can trigger a pass; a signal match alone can trigger a pass; either is sufficient under `trigger.any`.
3. `engine/pipeline.py:Pipeline.run` receives one step per resolved available pass. A suppressed pass is represented in dry-run/explain data and is not executed.
4. `engine/judge.py:Judge.evaluate_task` collects `PassResult` findings and sends them to SPEC-GR222's gate evaluator. The final `passed` value is the conjunction of all blocking gate results and contract-load success.
5. Existing `review_profiles.py` remains the source for named profile defaults and effort compatibility. A pass's explicit `execution` values override the profile only where the contract specifies them; no implicit unbounded defaults are allowed.
6. Tool lookup is capability-based. A missing declared tool creates `status=unavailable` with a reason and gate-visible result; it cannot become an empty successful pass.

## 5. Error Catalog

| Code | Trigger | Behavior |
|---|---|---|
| `REVIEW_PASS_ID_INVALID` | Empty, duplicate, or unsafe ID | Deny contract. |
| `REVIEW_PASS_TRIGGER_INVALID` | Wrong trigger shape, empty `paths`/`diff_signals` item, or unknown signal | Deny contract. |
| `REVIEW_SIGNAL_UNSUPPORTED` | Signal is named but no deterministic detector exists | Deny contract; never silently skip. |
| `REVIEW_EXECUTION_INVALID` | Missing model, non-positive budget/timeout, wrong type, or duplicate tools | Deny contract. |
| `REVIEW_TOOL_UNAVAILABLE` | Declared tool cannot be resolved | Mark pass unavailable, include reason, and let its gate decide; no completed result. |
| `REVIEW_PASS_TIMEOUT` | Timeout expires | Mark pass partial, record configured/actual time, and evaluate as non-completed. |
| `REVIEW_PASS_BUDGET_EXHAUSTED` | Output budget is consumed | Mark pass partial with `budget_exhausted`; do not label it complete. |
| `REVIEW_AST_PARSE_FAILURE` | Changed source cannot parse | For a required detector, emit a deterministic parse diagnostic and use regex fallback where defined; do not infer no signal. |
| `REVIEW_PASS_OUTPUT_INVALID` | Pass result violates output schema | Mark pass failed/unavailable according to gate policy and persist validation diagnostics. |

## 6. Edge Cases

- A disabled pass is `suppressed` with reason `disabled_by_policy`; it is not an unavailable pass and cannot satisfy `require_passes`.
- A pass with no `trigger` is eligible when at least one eligible file is in its scope. A pass with an empty `trigger.any` is invalid, not always eligible.
- Path and signal triggers are OR; multiple hits do not run a pass multiple times. The pass has one run and retains all trigger reasons/hits.
- A signal on a deleted file is detected from the base/head diff only when the added/modified side or signature comparison requires it; a deletion alone cannot produce an added-line regex hit but can produce `public_signature_change` for a removed public declaration.
- Generated/fixture files excluded by SPEC-GR220 do not create path triggers or signal hits unless another eligible file triggers a pass whose scope explicitly includes them.
- A 1,000+ file diff is scanned with bounded AST parsing and exact regexes; one parse failure is recorded per file, not one traceback per detector.
- `budget=0` and `timeout_seconds=0` are invalid, not “unlimited.” Unlimited execution is not a v1 state.
- Tool lists preserve declaration order and reject duplicates; the effective tool trace is exact.

## 7. Testing

1. Parse the normative correctness/security pass examples and assert every typed field and default.
2. Assert missing IDs, duplicate IDs, missing execution, zero budgets, unknown signals, duplicate tools, and empty trigger lists produce exact errors.
3. Build fixtures for each mandatory detector with positive, negative, comment/string, deletion, and formatting-only cases.
4. Run the exact signal matrix: each required pattern fires; a near miss (`authenticate_user` without the exact allowed AST/regex form, bounded `.limit(10).all()`, private `_func`, or `yaml.safe_load`) does not fire unless it matches the stated exact detector.
5. Compare base/head Python ASTs for added, removed, parameter-count, keyword-only, annotation, and private-definition changes; assert public signature hits and formatting-only non-hits.
6. Assert trigger OR semantics with path-only, signal-only, both, and neither fixtures.
7. Assert one pass run for multiple hits and stable trigger-reason ordering.
8. Simulate missing tools, timeout, budget exhaustion, malformed pass output, and parse failure; assert status/reason and gate-visible non-completion.
9. Assert deterministic output by running detection five times and comparing serialized `DiffSignals` bytes.
10. Assert a 1,200-file diff completes within the test's bounded fixture budget and emits no duplicate hits for identical lines.
11. Run focused tests, `ruff check`, and the repository test command.

## 8. Hilo Impact

This spec depends on SPEC-GR219's pass schema and SPEC-GR220's normalized paths. It feeds SPEC-GR222 with typed findings/pass status and SPEC-GR223 with trigger reasons, signal evidence, effective execution, and unavailable-pass traces. Hilo should link each detector to the exact pipeline/judge execution path; an isolated detector module without a runner edge is an inert feature.
