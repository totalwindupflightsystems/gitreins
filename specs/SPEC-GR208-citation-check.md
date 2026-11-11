# SPEC-GR208 — Deterministic Citation Check

## 1. Purpose

Add a zero-LLM post-pass that validates every `file:line` citation emitted in Tier 2 verdict items and failure modes against the changed files and working tree, downgrading any item with an unverified citation to `NOT_VERIFIABLE` with reason `citation_unverified` before the verdict is persisted.

### Requirements

- **R1.** The citation pass MUST run deterministically after Tier 2 produces a verdict and before `persist.py` writes `verdict.json`.  
  **test:** `test_citation_pass_runs_between_tier2_and_persist` records `tier2`, `citation_check`, `build_verdict_data`, and `persist` in that order.  
  **check:** the pass has no LLM client parameter and is called before `build_verdict_data()`/`VerdictPersister.persist()`.
- **R2.** The pass MUST make zero LLM calls, network calls, shell commands, or evaluator tool calls.  
  **test:** `test_citation_check_is_zero_llm_and_zero_shell` patches `LLMClient`, `subprocess`, and evaluator tools and asserts no calls.  
  **check:** the implementation uses only in-process diff/file reads and `os.path`/`pathlib` operations.
- **R3.** Every citation in every item detail and every failure-mode string MUST be extracted and checked; one invalid citation MUST downgrade only the containing item or failure-mode-derived item, not unrelated criteria.  
  **test:** `test_every_item_and_failure_mode_citation_is_checked_independently`.  
  **check:** extraction walks `Verdict.items` and all five `FailureMode` fields.
- **R4.** An unverified citation MUST set the containing verdict item status to `NOT_VERIFIABLE` and `not_verifiable_reason` to exactly `citation_unverified`, reusing the existing GR-178 status and persistence path.  
  **test:** `test_unverified_citation_downgrades_item_with_gr178_reason`.  
  **check:** the implementation imports/uses `STATUS_NOT_VERIFIABLE` and does not invent a second status vocabulary.
- **R5.** A valid citation MUST leave the item status, detail, reason, failure-mode fields, and top-level verdict unchanged.  
  **test:** `test_verified_citation_is_byte_stable`.  
  **check:** the pass mutates only items with a failed citation verification.

## 2. Interface

All signatures below are normative and use the repository's Python 3 typing style.

```python
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

@dataclass(frozen=True)
class Citation:
    path: str
    start_line: int
    end_line: int
    raw: str

@dataclass(frozen=True)
class CitationCheck:
    citation: Citation
    verified: bool
    reason: str = ""

# engine/citation_check.py
def extract_citations(text: str) -> list[Citation]: ...

def check_citation(
    citation: Citation,
    *,
    workdir: str,
    scan_root: str,
    diff_paths: frozenset[str],
) -> CitationCheck: ...

def verify_verdict_citations(
    verdict: Verdict,
    *,
    workdir: str,
    scan_root: str,
    diff_text: str,
    diff_paths: frozenset[str] | None = None,
) -> Verdict: ...

# engine/judge.py
class Judge:
    def _run_citation_check(
        self,
        verdict: Verdict,
        *,
        diff_text: str,
    ) -> Verdict: ...
```

`extract_citations()` MUST accept citations in these forms: `path/to/file.py:17`, `path/to/file.py:17-21`, and backtick-wrapped equivalents. It MUST not treat a Windows drive prefix, URL port, decimal number, or prose `foo:bar` as a citation. `start_line <= end_line` is required; a single line sets both values.

`check_citation()` MUST resolve the path relative to `scan_root` after rejecting absolute paths and normalized paths that escape `scan_root`. `workdir` is the control root for the Git diff; `scan_root` is the source tree the primary judge graded. A citation is verified only when its normalized path is present in `diff_paths` or the file exists inside `scan_root`, and the requested line range is within the file's actual UTF-8 line count. For a deleted path present in the diff but absent from the working tree, the citation is unverified.

`verify_verdict_citations()` MUST parse `diff_paths` from `diff_text` when the optional set is `None`, call `extract_citations()` on every item `detail` and each failure-mode field (`what`, `where`, `trigger`, `detected_by`), and return the same `Verdict` object after deterministic in-place field updates. It MUST not change `Verdict.verdict` or `Verdict.summary` directly; existing gate/persistence logic observes the downgraded item and applies GR-178.

### Requirements

- **R6.** The extraction grammar MUST support single-line and inclusive ranges and reject non-citations, absolute paths, path traversal, URLs, and malformed ranges.  
  **test:** `test_extract_citations_accepts_single_and_range_and_rejects_non_citations`.  
  **check:** the parser returns exact `Citation(path, start_line, end_line, raw)` values and never guesses a line for malformed text.
- **R7.** Path resolution MUST be confined to `scan_root`, with diff membership and working-tree existence both enforced as specified.  
  **test:** `test_citation_path_traversal_and_deleted_file_are_unverified`.  
  **check:** `Path.resolve()` is checked with `os.path.commonpath`/`Path.is_relative_to`; no `../` citation can read outside the scan root.
- **R8.** Line-range plausibility MUST use the actual UTF-8 file line count and MUST reject line zero, negative lines, reversed ranges, and an end line beyond EOF.  
  **test:** `test_line_range_must_be_plausible`.  
  **check:** the file is read with `encoding="utf-8"`, errors are not replaced, and decode/read errors produce an unverified check.

## 3. Data Model

### 3.1 Citation grammar

The canonical grammar is:

```text
(?<![A-Za-z0-9_./-])(?P<path>(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+\.[A-Za-z0-9_+-]+):(?P<start>[1-9][0-9]*)(?:-(?P<end>[1-9][0-9]*))?(?![0-9])
```

The implementation MUST additionally handle a backtick directly around the complete citation. Paths use POSIX separators as emitted by GitReins; `\` is rejected. The parser MUST preserve the exact matched `raw` text while normalizing no identifier supplied to the caller.

### 3.2 Verdict mutation

The existing dataclasses are the source of truth:

```python
@dataclass
class VerdictItem:
    criterion: str
    status: str
    detail: str
    resolution_probability: float | None = None
    cited_path: str | None = None
    derived: bool = False
    not_verifiable_reason: str | None = None

@dataclass
class FailureMode:
    what: str = ""
    where: str = ""
    trigger: str = ""
    detected_by: str = ""
    covered_in_this_pr: bool = False
```

When any citation extracted from an item's `detail` fails, mutate only that `VerdictItem`:

```python
item.status = STATUS_NOT_VERIFIABLE
item.not_verifiable_reason = "citation_unverified"
```

Do not overwrite a pre-existing non-empty `not_verifiable_reason` when the item was already `NOT_VERIFIABLE`; the pass still records the failed citation in the diagnostic log but preserves the first GR-178 reason. If an item is already `FAIL`, it is still downgraded to `NOT_VERIFIABLE` because the citation evidence is not trustworthy enough to retain the failure as verified.

Failure modes do not have a status field. For a failure mode with an unverified citation, retain its five persisted fields and attach a deterministic diagnostic entry to a new additive `citation_check` record on the verdict:

```python
verdict.citation_check = {
    "checked": 4,
    "unverified": [
        {"scope": "failure_mode[0].where", "citation": "src/deleted.py:9", "reason": "citation_unverified"}
    ]
}
```

The `citation_check` field MUST be added to `Verdict` as `dict[str, Any] | None = None`; it is omitted from persisted JSON when no citations were found and included when citations were checked. Item downgrades are represented by the existing `items` serialization, including `not_verifiable_reason`. Failure-mode diagnostics are serialized additively by `build_verdict_data()` under top-level `citation_check` and never alter the existing `failure_modes` schema.

### 3.3 Persisted JSON

A verdict with one invalid item citation and one invalid failure-mode citation MUST persist as:

```json
{
  "items": [
    {
      "criterion": "criterion text",
      "status": "NOT_VERIFIABLE",
      "detail": "implemented in src/widget.py:999",
      "not_verifiable_reason": "citation_unverified"
    }
  ],
  "failure_modes": [
    {"what": "bad path src/deleted.py:9", "where": "src/deleted.py:9", "trigger": "", "detected_by": "pytest", "covered_in_this_pr": false}
  ],
  "citation_check": {
    "checked": 2,
    "unverified": [
      {"scope": "item[0].detail", "citation": "src/widget.py:999", "reason": "citation_unverified"},
      {"scope": "failure_mode[0].where", "citation": "src/deleted.py:9", "reason": "citation_unverified"}
    ]
  }
}
```

The example is illustrative of the exact key names and nesting; `checked` equals the number of extracted citations, and `unverified` preserves deterministic verdict traversal order. No raw file contents, command output, or secrets are persisted.

### Requirements

- **R9.** The data model MUST preserve existing item and failure-mode schemas while adding only the additive `Verdict.citation_check` record.  
  **test:** `test_citation_check_persistence_is_additive`.  
  **check:** old verdicts without citations serialize byte-identically and `failure_modes` retains all five existing fields.
- **R10.** Citation diagnostics MUST count every extracted citation and preserve stable traversal order.  
  **test:** `test_citation_check_counts_and_orders_diagnostics`.  
  **check:** traversal is items in list order, then failure modes in list order, then fields in `what`, `where`, `trigger`, `detected_by` order.
- **R11.** GR-178 reasons MUST survive persistence exactly as `not_verifiable_reason: "citation_unverified"`.  
  **test:** `test_citation_unverified_reason_round_trips_through_persist`.  
  **check:** `_verdict_item_dict()` continues to serialize `not_verifiable_reason` without renaming or filtering it.

## 4. Wiring

1. The existing Tier 2 evaluator returns a `Verdict` through either the legacy path (`JudgeResult.tier2`/`verdict`) or the configured pipeline path (`_find_pipeline_verdict(result.pipeline_result)`).
2. Immediately after Tier 2 extraction, `Judge.evaluate_task()` calls `_run_citation_check()` with `diff_text` read through the existing read-only `read_diff` boundary against `self.workdir`; it passes `self.scan_root` for source-file resolution.
3. `_run_citation_check()` calls `verify_verdict_citations()` with the primary `Verdict`; it does not construct `LLMClient`, `AgenticEvaluator`, or a subprocess.
4. The same object is reattached to the result: `result.tier2`, `result.verdict`, and the pipeline step's structured `data.items` MUST reflect the downgraded item. The pipeline path MUST update the `VerdictItem` object used by `_find_pipeline_verdict` and the serialized pipeline stage data in one authoritative conversion function, not by parsing rendered summary text.
5. Existing `gate.check_coverage`, `verdict_is_passing`, and `merge_recommendation` run after the mutation or are explicitly re-run once, so a downgraded item cannot remain a pass. A `NOT_VERIFIABLE` item follows the existing GR-178 `needs_review` path.
6. `persist.build_verdict_data()` copies `Verdict.citation_check` to top-level `citation_check` when non-empty and keeps the existing item/failure-mode serializers unchanged except for reading the already-mutated item status/reason.
7. `VerdictPersister.persist()` writes the fully checked payload; no citation check runs after the file write.
8. If no Tier 2 verdict exists, no citation pass runs. Tier 1-only and `skip_tier2=True` shapes remain unchanged.

### Requirements

- **R12.** The citation pass MUST operate on both legacy and pipeline Tier 2 paths and must update the exact object used for persistence.  
  **test:** `test_legacy_and_pipeline_verdicts_receive_identical_citation_downgrade`.  
  **check:** the hook has one shared verifier and no path that checks only rendered output.
- **R13.** A citation failure MUST flow through `engine.gate`'s existing NOT_VERIFIABLE/GR-178 behavior and MUST not be treated as a new hard FAIL or as PASS.  
  **test:** `test_citation_downgrade_uses_existing_gr178_merge_recommendation`.  
  **check:** `merge_recommendation()` returns `needs_review` for the mutated verdict.
- **R14.** The pass MUST not change Tier 1, primary model provenance, second-opinion data, or unrelated verdict fields.  
  **test:** `test_citation_pass_preserves_unrelated_result_fields`.  
  **check:** the mutation scope is limited to item status/reason and additive citation diagnostics.

## 5. Error Catalog

| Code | Condition | Required behavior |
|---|---|---|
| `GR208_NO_VERDICT` | Tier 1-only, skipped, or no Tier 2 verdict | No-op; persist historical shape. |
| `GR208_NO_CITATIONS` | No recognized citations | No-op; omit `citation_check`. |
| `GR208_MALFORMED` | Citation-like text has malformed path/range | Extract no false citation; if the text is a recognizable malformed citation, record it as unverified and downgrade its containing item. |
| `GR208_OUTSIDE_ROOT` | Absolute or traversal path | Mark citation unverified; never read outside `scan_root`. |
| `GR208_DELETED_FILE` | Path in diff but absent from working tree | Mark unverified; deleted content is not available for line verification. |
| `GR208_DECODE` | File cannot be decoded as UTF-8 or cannot be read | Mark unverified; do not substitute replacement characters. |
| `GR208_LINE_RANGE` | Zero, negative, reversed, or past-EOF range | Mark unverified. |
| `GR208_DIFF_PARSE` | Diff cannot be parsed | Use the explicit `diff_paths` if supplied; otherwise treat citations as eligible only when the file exists inside `scan_root`, and never crash the judge. |
| `GR208_PERSIST` | Citation diagnostics cannot be copied | Preserve item downgrade and let the existing persistence error policy handle the write; never restore PASS. |

### Requirements

- **R15.** Every read/parse failure MUST fail closed for the affected citation and MUST not crash or block persistence of the primary verdict.  
  **test:** `test_citation_read_and_diff_errors_are_unverified_but_persistable`.  
  **check:** exceptions are converted to `CitationCheck(verified=False, reason="citation_unverified")` at the citation boundary.
- **R16.** The pass MUST never read outside `scan_root`, execute content, or interpret a citation as a command.  
  **test:** `test_citation_check_never_executes_or_escapes_root`.  
  **check:** all file access is read-only and path containment is checked before opening.

## 6. Edge Cases

- A citation to a file outside the diff is valid when the file exists within `scan_root`; the diff is evidence scope, not an exclusive allowlist. A missing file is unverified even if its path appears in the diff.
- A citation to a deleted file is unverified because the requested line cannot be checked in the working tree.
- An untracked file is valid if it exists under `scan_root`; it need not appear in `diff_paths`.
- A file with a final newline and a citation to its last content line is valid; a citation to an extra empty line after the final newline is invalid because the line range must identify content within the actual `splitlines()` count. An empty file has zero valid lines.
- A citation range is inclusive; `file.py:4-4` is the same as `file.py:4`.
- Multiple citations in one detail are all checked; one failure downgrades the item once, while diagnostics retain every failed citation.
- A pre-existing `NOT_VERIFIABLE` item keeps its first non-empty `not_verifiable_reason`; citation diagnostics still identify the unverified citation.
- A citation in `FailureMode.detected_by` is checked exactly like one in `where`; a failure mode has no status to downgrade, so its existing five fields remain unchanged.
- URLs (`https://host/file.py:443`), Windows drive paths, decimal values, and `name:line` prose without a file extension are not extracted.
- If both judges are present, citation checking runs after the primary/second-opinion comparison but checks only the authoritative primary verdict items and failure modes; it does not inspect second-opinion prose because that prose is not persisted.
- Concurrent judge runs use local immutable citation snapshots and do not share mutable counters.

### Requirements

- **R17.** Deleted files, outside-diff files, untracked files, multiple citations, and pre-existing GR-178 items MUST have explicit deterministic behavior as above.  
  **test:** `test_citation_edge_cases_deleted_outside_diff_untracked_multiple_and_existing_not_verifiable`.  
  **check:** the test asserts each case independently and checks diagnostics order.
- **R18.** The citation pass MUST remain zero-LLM even when the verdict contains second-opinion metadata or failure-mode citations.  
  **test:** `test_second_opinion_metadata_does_not_trigger_citation_llm_call`.  
  **check:** `verify_verdict_citations` accepts only the verdict and filesystem/diff inputs.

## 7. Testing

| Scenario | Test and expected assertion |
|---|---|
| Grammar | `test_extract_citations_accepts_single_and_range_and_rejects_non_citations`: exact parsed paths and lines. |
| Existing file and plausible line | `test_verified_citation_is_byte_stable`: PASS remains PASS. |
| Out-of-range | `test_line_range_must_be_plausible`: line 0, reversed, and past EOF become unverified. |
| Traversal/absolute | `test_citation_path_traversal_and_deleted_file_are_unverified`: no outside read. |
| Diff membership | `test_citation_path_resolution_uses_diff_and_working_tree`: changed, untracked, and outside-diff existing files follow §6. |
| Item downgrade | `test_unverified_citation_downgrades_item_with_gr178_reason`: exact status/reason. |
| Failure modes | `test_every_item_and_failure_mode_citation_is_checked_independently`: diagnostics for all fields. |
| Persistence | `test_citation_unverified_reason_round_trips_through_persist`: exact persisted reason. |
| Additive shape | `test_citation_check_persistence_is_additive`: no-citation historical shape unchanged. |
| Order/count | `test_citation_check_counts_and_orders_diagnostics`: stable count and order. |
| Wiring | `test_citation_pass_runs_between_tier2_and_persist`: event order. |
| Legacy/pipeline | `test_legacy_and_pipeline_verdicts_receive_identical_citation_downgrade`. |
| Gate | `test_citation_downgrade_uses_existing_gr178_merge_recommendation`: `needs_review`. |
| Errors | `test_citation_read_and_diff_errors_are_unverified_but_persistable`. |
| Security | `test_citation_check_never_executes_or_escapes_root`: no shell, no root escape. |
| Second opinion | `test_second_opinion_metadata_does_not_trigger_citation_llm_call`. |

Negative controls MUST include: a valid citation that would fail if the checker used existence-only logic; an invalid past-EOF citation that would pass if line bounds were skipped; a deleted-file citation that would pass if diff membership alone were trusted; a `../` citation that would pass if containment were omitted; and a primary PASS with no citations that would fail if the post-pass changed unrelated verdicts.

### Requirements

- **R19.** The complete test list and all negative controls MUST be implemented with no real LLM/provider/network dependency.  
  **test:** `pytest -q tests/test_citation_check.py tests/test_judge.py tests/test_persist.py tests/test_evaluator.py`.  
  **check:** the test suite contains named tests for grammar, file/range validation, deletion/traversal, item and failure-mode traversal, persistence, wiring, gate behavior, error safety, and zero-LLM behavior.
- **R20.** The targeted suite and full non-LLM suite MUST pass.  
  **test:** `pytest -q tests/test_citation_check.py tests/test_judge.py tests/test_persist.py tests/test_evaluator.py` and `pytest -q -m 'not llm'`.  
  **check:** both commands exit 0 and the tests do not contact an external service.

## 8. Hilo Impact

**Depends on:** `engine/evaluator.py` for `Verdict`, `VerdictItem`, `FailureMode`, and GR-179 parsing; `engine/gate.py` for `STATUS_NOT_VERIFIABLE`, `check_coverage`, and `merge_recommendation`; `engine/judge.py` for the post-Tier-2 hook and both legacy/pipeline extraction paths; `engine/persist.py` for additive `citation_check` serialization; Git's read-only diff path and the configured `scan_root`.

**Changes consumed by:** CLI judge, MCP judge/status, history reports, merge recommendation consumers, and any caller reading persisted `items`. Existing consumers see the established `NOT_VERIFIABLE`/GR-178 semantics; new consumers may inspect `citation_check` diagnostics.

**Hilo invariants:** deterministic and local-only; no new provider, network, shell, or persistence store; primary verdict remains authoritative; citation evidence is checked before persistence; failure-mode schema remains five fields; an absent citation block preserves historical JSON shape.

### Requirements

- **R21.** The implementation MUST preserve historical behavior when no citations are present: no LLM call, no `citation_check` key, no item mutation, and no changed summary.  
  **test:** `test_no_citations_preserve_historical_shape`.  
  **check:** compare serialized verdict data before and after enabling the checker on a citation-free verdict.
