# SPEC-GR223 — `gitreins review explain` and Finding Traceability

## 1. Purpose

Specify the explain CLI that renders the resolved base policy and its effect on a diff, including layers, globs, instructions, effective execution, config hash, gate results, suppressed rules, unavailable passes, and persisted finding trace fields.

**Normative requirements**

- **R1** The command MUST resolve the same protected-base policy and diff inputs as review execution; explain MUST NOT have a second resolver. `test: test_explain_matches_execution_resolution`.
- **R2** Explain MUST print resolved layers, matched globs, appended instructions, effective model/tools, config hash, and gate evaluation for the selected diff. `test: test_explain_required_sections`.
- **R3** `--dry-run` MUST show suppressed rules and unavailable passes without invoking an LLM or review tool. `test: test_dry_run_is_non_executing`.
- **R4** Every persisted finding MUST carry the trace fields named by the design document. `test: test_persisted_finding_trace_fields`.
- **R5** Policy-source failures and gate denials MUST be visible in explain output and MUST return the same non-zero category used by review execution. `test: test_explain_denial_exit_code`.

## 2. Interface

The CLI implementation belongs in `gitreins/cli.py`; rendering is a pure engine function so JSON and text cannot diverge in policy decisions.

```python
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

@dataclass(frozen=True)
class ExplainRequest:
    workdir: str
    base: str
    head: str
    dry_run: bool = False
    output: Literal["text", "json"] = "text"

@dataclass(frozen=True)
class ExplainDocument:
    source_ref: str
    source_commit: str
    head_ref: str
    diff_range: tuple[str, str]
    config_hash: str
    layers: tuple[str, ...]
    matched_overlays: tuple[Mapping[str, object], ...]
    appended_instructions: tuple[str, ...]
    effective_model: str | None
    effective_tools: tuple[str, ...]
    passes: tuple[Mapping[str, object], ...]
    gates: tuple[Mapping[str, object], ...]
    suppressed_rules: tuple[Mapping[str, object], ...]
    unavailable_passes: tuple[Mapping[str, object], ...]
    diagnostics: tuple[Mapping[str, object], ...]
    findings: tuple[Mapping[str, object], ...] = ()


def build_explain_document(
    request: ExplainRequest,
    *,
    source: "PolicySource",
) -> ExplainDocument: ...

def render_explain_text(document: ExplainDocument) -> str: ...
def render_explain_json(document: ExplainDocument) -> str: ...
def cmd_review_explain(args: "argparse.Namespace") -> None: ...
```

The parser MUST expose:

```text
gitreins review explain --base <ref> --head <ref> [--dry-run] [--json]
```

`--base` is required; `--head` defaults to `HEAD` only when omitted by the parser? No: v1 requires both flags to make the diff range explicit. The implementation MUST require both. `--json` emits exactly one JSON document on stdout; diagnostics go inside it, not as stray stdout. Human text goes to stdout and warnings/errors to stderr.

Text output MUST contain these headings in order:

```text
Review contract explain
Policy source
Resolved layers
Changed files and path selection
Matched overlays
Appended instructions
Effective execution
Triggered passes
Suppressed rules
Unavailable passes
Gate evaluation
Config hash
Diagnostics
```

JSON output uses lower-snake-case keys matching `ExplainDocument`; arrays preserve resolver order. The explain command returns `0` for a successfully resolved dry run with no blocking gate, `1` for a resolved diff with a normal blocking gate, and `2` for policy/contract denial, unavailable required passes, or invalid invocation.

## 3. Data Model

The normative explainability YAML is:

```yaml
explainability:
  effective_config: {enabled: true, commands: ["gitreins review explain --base origin/main --head HEAD"]}
  trace_each_finding:
    include: [pass_id, matched_layers, matched_globs, appended_instructions,
              effective_model, effective_tools, config_hash, diff_range, gate_evaluation]
  dry_run: {enabled: true, show_suppressed_rules: true, show_unavailable_passes: true}
```

The exact persisted trace field contract is:

| Field | Type | Meaning |
|---|---|---|
| `pass_id` | non-empty string | Pass that emitted the finding. |
| `matched_layers` | ordered string list | Layers contributing to the effective policy for this finding. |
| `matched_globs` | ordered string list | Overlay/pass globs that matched its file. |
| `appended_instructions` | ordered string list | Instruction fragments actually supplied to the pass. |
| `effective_model` | string | Model selected after layer/profile resolution. |
| `effective_tools` | ordered string list | Tools available to the pass after `tools_add` resolution. |
| `config_hash` | `sha256:<hex>` string | Canonical resolved base-contract hash. |
| `diff_range` | object `{base, head, file, start_line, end_line}` | Exact comparison and source range. |
| `gate_evaluation` | object | Gate ID, pass/fail, blocking, action, reasons, finding IDs. |

A finding without any one of these fields is not persistable under the v1 contract. A finding from a legacy run without review-contract execution retains the legacy item shape and has no synthetic trace.

Dry-run pass entries MUST include:

```json
{
  "id": "security",
  "status": "suppressed|unavailable|available",
  "reasons": ["disabled_by_policy", "no_path_or_signal_trigger"],
  "matched_globs": [],
  "signal_hits": []
}
```

Suppressed rules are rules prevented by explicit policy (disabled pass, excluded path, skip label/title/draft). Unavailable passes are enabled/triggered but cannot execute because a model/tool/capability is unavailable or a required pass failed to initialize. These categories MUST NOT be merged.

## 4. Wiring

1. Add a `review` parser under the existing root parser in `gitreins/cli.py`, then add the `explain` child parser with required `--base`, required `--head`, optional `--dry-run`, and `--json`.
2. `cmd_review_explain` obtains the repository workdir, resolves the protected base through SPEC-GR219, computes the same changed files/lines as the judge/pre-commit path, and calls `build_explain_document`.
3. `build_explain_document` calls SPEC-GR219's loader/resolver, SPEC-GR220's matcher, SPEC-GR221's deterministic signal detector, and SPEC-GR222's gate evaluator. It MUST NOT duplicate any of those decisions.
4. With `--dry-run`, do not construct an LLM client, invoke pass tools, mutate `.gitreins/usage.jsonl`, write history, or write labels. The command may read repository state and render the document. It must use immutable base/head commit IDs and a changed-file/diff digest so explain cannot race a moving worktree.
5. With non-dry-run explain, the command still explains the selected diff; it may evaluate configured deterministic gates but does not persist a review verdict unless an explicit review execution command is used. The command never changes source files or refs.
6. `engine.persist.build_verdict_data` receives `FindingTrace` from SPEC-GR222 and writes it under each persisted item/finding. `gitreins report` and `serve` read the additive fields without requiring them on old records.
7. The command's exit status is derived from the same `VerdictGateResult` mapping as SPEC-GR222. Human wording is not parsed for status.

## 5. Error Catalog

| Code | Condition | Behavior |
|---|---|---|
| `REVIEW_EXPLAIN_DISABLED` | Contract disables explainability | Exit 2 with a diagnostic; do not silently print partial policy. |
| `REVIEW_EXPLAIN_MISSING_BASE` / `REVIEW_EXPLAIN_MISSING_HEAD` | Required flag absent | Argparse usage error, exit 2. |
| `REVIEW_EXPLAIN_REF_INVALID` | Git ref cannot resolve | Exit 2; include ref and git error in JSON/text diagnostics. |
| `REVIEW_EXPLAIN_POLICY_DENIED` | Loader/schema/lock/conflict denial | Exit 2; print diagnostics and no effective policy claimed. |
| `REVIEW_EXPLAIN_GLOB_FAILURE` | Matcher rejects a pattern | Exit 2; include exact field and pattern. |
| `REVIEW_EXPLAIN_GATE_FAILURE` | Resolved gate blocks | Exit 1; include all gate evaluations and blocking finding IDs. |
| `REVIEW_EXPLAIN_OUTPUT_FAILURE` | JSON/text serialization fails | Exit 2; write no partial document. |
| `REVIEW_EXPLAIN_TRACE_MISSING` | Persisting a finding without all required trace fields | Persistence fails closed; never write an apparently complete finding. |

## 6. Edge Cases

- A diff with no changed files still prints the resolved policy, empty path selection, suppressed/unavailable pass lists, and a passing gate evaluation when policy loading succeeded.
- `--dry-run` with a triggered pass whose model is absent prints it under `unavailable_passes`; it does not call the model and does not change the exit code unless the pass is required by a gate, in which case exit is `2`.
- A path excluded by include/exclude appears in changed-file selection with `eligible=false`; it must not be mislabeled as a matched overlay.
- A PR-side `.gitreins/review.yaml` is never read for policy. Explain's `source_commit` must equal the protected base commit and must differ from the head commit in the self-weakening fixture.
- Multiple matching overlays show all matched globs and the final effective scalar profile/model, not only the last overlay.
- A 1,000+ file diff uses the same bounded ordering and rendering as execution; JSON must remain valid and contain counts plus entries, never truncated invalid JSON.
- Secrets, API keys, full environment values, and model endpoint URLs MUST NOT appear in text, JSON, config hashes, diagnostics, or finding traces.
- Existing verdicts with no trace render as `trace: unavailable (legacy verdict)` only in report/viewer surfaces; explain never fabricates one.

## 7. Testing

1. Run `gitreins review explain --help`; assert the command, required flags, `--dry-run`, and `--json` are present.
2. Build a temporary base/head repository with the normative API overlay and assert text contains all required headings and exact overlay/profile/tool/hash values.
3. Run `--json` and parse stdout as one JSON object; assert no non-JSON stdout prefix/suffix and all required top-level arrays exist.
4. Compare explain's resolved policy/hash/pass triggers with a direct judge/pre-commit resolution on the same base/head; assert byte-equivalent canonical policy data.
5. Run dry-run with a disabled pass, excluded file, missing model, and missing tool; assert suppressed and unavailable entries are distinct, no LLM/tool call occurred, and no usage/history file changed.
6. Run a blocking finding fixture and assert exit `1`, gate evaluation in output, and blocking ID. Run malformed policy/locked-field/unknown-glob fixtures and assert exit `2` with diagnostics.
7. Persist a finding through the judge path, read `verdict.json`, and assert every required trace field, exact config hash, base source commit, diff range, and gate evaluation.
8. Run the PR-self-weakening fixture and assert explain reads base policy despite a weaker head policy.
9. Test no-change, 1,000+ file, Unicode, excluded-path, and multiple-overlay output cases.
10. Assert secrets/URLs are absent from serialized output with a canary config containing secret-like values.
11. Verify foreman closeout wiring: `gitreins task complete` invokes review explain with caller-provided base/head from the task worktree context, and closeout evidence contains the explain JSON plus exit code. Run CLI focused tests, `ruff check`, and the repository test command.

## 8. Hilo Impact

This spec depends on all preceding review-contract specs and existing CLI/parser, persistence, report, and serve surfaces. It is the user-facing proof that the resolver is shared rather than duplicated. Hilo must connect `cmd_review_explain` to the same loader, matcher, detector, and gate nodes used by judge/pre-commit, and connect `FindingTrace` to `engine.persist.build_verdict_data` and readers. No Hilo storage format changes are required.

## PANEL VERDICTS (binding amendments)

These decisions override conflicting text above and are the contract for GR-223.

1. **Foreman closeout wiring (D8).** `gitreins task complete` MUST invoke review explain with caller-provided `--base` and `--head` resolved from the task's worktree context. The closeout evidence MUST contain the explain JSON and exit code. If either ref cannot be resolved, closeout fails closed; explain MUST never silently substitute `HEAD`.
2. **Immutable explain inputs (panel A).** Explain requests carry immutable base/head commit IDs and a changed-file/diff digest. The document must identify the exact policy source commit and config hash used; it cannot race a moving worktree or claim equivalence with another execution.
3. **Dry-run safety (panel A/C).** Dry-run uses only resolver, matcher, deterministic detector, and pure gate evaluation. It MUST NOT construct LLM/tool clients or mutate usage, history, labels, refs, or source files. Assert unchanged artifacts before/after.
4. **Versioned envelope and digest (D1/D7).** Contract-era explain/closeout verdict evidence carries `contract_schema_version`, `verdict_schema_version: 2`, `writer_version`, and the gate digest (`source_commit`, worktree fingerprint, policy hash, run id). Legacy records remain readable without synthetic trace/equivalence claims.
5. **Shared precedence/order (D2/D3).** Explain calls the same precedence and processing order as execution: locked organization gates > pass gates > overlay gates > defaults; citation check > coverage downgrade > standards filter > gate/outcome recomputation; lower layers only add restrictions and `deny_and_report` wins.
6. **Wall-clock discipline (D9/panel C).** Explain tests assert exact headings, JSON shape, refs, hashes, match sets, unchanged artifacts, and exit categories. Timeout tests use an injectable clock/fake scheduler; no tight elapsed-duration assertion is allowed.
