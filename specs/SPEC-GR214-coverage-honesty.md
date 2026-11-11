# SPEC-GR214 — Coverage Honesty

## 1. Purpose

Make every persisted verdict state exactly which repository files were fully seen, partially skimmed, or dropped, and route any criterion that depends on a dropped file through the existing GR-178 `NOT_VERIFIABLE` path instead of allowing a partial read to produce a PASS.

This specification is the design authority for the mandatory top-level `coverage` block. The canonical verdict schema remains assembled by `engine.persist.build_verdict_data()` and the live verdict schema remains in `engine.judge`/`engine.evaluator`.

## 2. Interface

The implementation MUST provide these exact interfaces in a new pure module `engine/coverage.py`:

```python
from typing import TypedDict
from collections.abc import Iterable

class CoverageBlock(TypedDict):
    files_seen: list[str]
    files_skimmed: list[str]
    files_dropped: list[str]
    reason: str

def normalize_coverage_block(
    raw: object, *, workdir: str
) -> CoverageBlock: ...

def downgrade_for_dropped_files(
    verdict: Verdict, coverage: CoverageBlock
) -> Verdict: ...
```

The implementation MUST add these exact fields/methods to the live result path:

```python
class AgenticEvaluator:
    def coverage_snapshot(self) -> CoverageBlock: ...

class JudgeResult:
    coverage: CoverageBlock | None
```

`VerdictItem` MUST gain these additive fields:

```python
evidence_files: list[str] = field(default_factory=list)
```

`engine.persist.build_verdict_data()` MUST retain its exact existing signature:

```python
def build_verdict_data(
    workdir: str, task: Task, result: JudgeResult
) -> dict[Any, Any]: ...
```

- **R1** — `CoverageBlock` MUST contain exactly `files_seen`, `files_skimmed`, `files_dropped`, and `reason`; the three file members MUST be lists of repo-relative POSIX paths and `reason` MUST be a non-empty string.  
  `test:` `tests/test_coverage.py::test_coverage_block_has_exact_keys_and_types`.
- **R2** — `normalize_coverage_block()` MUST deduplicate and lexically sort every file list after normalizing separators and removing a leading `./`.  
  `test:` `tests/test_coverage.py::test_coverage_paths_are_normalized_deduplicated_and_sorted`.
- **R3** — A path outside `workdir`, an absolute path, a path containing `..`, or a URL MUST be rejected from all three lists and named in `reason`.  
  `test:` `tests/test_coverage.py::test_coverage_rejects_escape_paths_and_urls`.
- **R4** — `AgenticEvaluator.coverage_snapshot()` MUST use the evaluator's actual read/skim/drop bookkeeping, not model prose or task criteria.  
  `check:` `engine/evaluator.py` records every accepted `read_file`, bounded/range read, scope refusal, size refusal, timeout, and tool error at the tool boundary.
- **R5** — Every `JudgeResult` produced by legacy, pipeline, skipped-Tier-2, and error paths MUST carry a coverage block before persistence.  
  `test:` `tests/test_judge.py::test_every_judge_result_path_has_coverage`.

## 3. Data Model

Every new verdict record MUST contain this top-level member, including Tier 1-only and failed persistence-builder inputs:

```json
"coverage": {
  "files_seen": ["engine/judge.py"],
  "files_skimmed": ["README.md"],
  "files_dropped": ["tests/test_judge.py"],
  "reason": "tests/test_judge.py was outside changed-file scope"
}
```

File-state semantics are exact:

- `files_seen`: the evaluator observed the complete file content, or a complete authoritative diff for the file, and could use it as evidence.
- `files_skimmed`: the evaluator observed metadata, a bounded head/tail, a range, or diff hunks without complete file content.
- `files_dropped`: a relevant file was not observed because scope, size, binary handling, missing path, time budget, command refusal, or tool failure prevented evidence collection.
- A path MUST appear in only one list. If states disagree, `files_dropped` wins, then `files_skimmed`, then `files_seen`.
- `reason` MUST be one concise human-readable explanation. Implementations MUST use `complete`, `tier2_not_run`, `scope_restricted`, `budget_exhausted`, `read_refused`, `tool_error`, or `mixed:` followed by the concrete causes.

Each verdict item MUST serialize its evidence file set when non-empty:

```json
{
  "criterion": "The judge is wired from the CLI entry point.",
  "status": "PASS",
  "detail": "gitreins/cli.py:123 calls Judge.evaluate_task",
  "evidence_files": ["gitreins/cli.py", "engine/judge.py"]
}
```

- **R6** — `coverage` MUST be present in `build_verdict_data()` output for an ordinary pass, fail, Tier 1-only result, empty diff, and persistence-builder fake.  
  `test:` `tests/test_persist.py::test_coverage_is_mandatory_on_every_verdict_shape`.
- **R7** — A Tier 1-only or no-Tier-2 result MUST use empty file lists and `reason="tier2_not_run"`; it MUST not claim that the repository was seen.  
  `test:` `tests/test_persist.py::test_tier1_only_coverage_is_explicitly_not_graded`.
- **R8** — A criterion item MUST carry `evidence_files` as a list after parsing; missing or malformed input MUST normalize to an empty list.  
  `test:` `tests/test_judge.py::test_verdict_item_evidence_files_parse_and_normalize`.
- **R9** — `downgrade_for_dropped_files()` MUST change a PASS item to `NOT_VERIFIABLE` when its `evidence_files` intersects `coverage.files_dropped`, set `not_verifiable_reason`, and force the verdict outcome to `INCOMPLETE`.  
  `test:` `tests/test_coverage.py::test_pass_spanning_dropped_file_uses_gr178_not_verifiable`.
- **R10** — When an item has no `evidence_files` and `files_dropped` is non-empty, the item MUST be downgraded to `NOT_VERIFIABLE` because its dependency set is unknown; this is the fail-closed rule against unreported file dependence.  
  `test:` `tests/test_coverage.py::test_missing_evidence_file_set_is_not_a_pass_under_partial_coverage`.
- **R11** — The downgrade reason MUST name the dropped paths and the settling action: rerun with those paths in scope or perform the named human check.  
  `test:` `tests/test_coverage.py::test_not_verifiable_reason_names_dropped_paths_and_settling_action`.
- **R12** — The persisted `merge_recommendation` MUST be `needs_review` whenever coverage creates a `NOT_VERIFIABLE` item, using the existing GR-178 `engine.gate.merge_recommendation()` authority.  
  `test:` `tests/test_persist.py::test_coverage_downgrade_persists_gr178_needs_review`.

## 4. Wiring

1. `engine/evaluator.py` initializes coverage sets per evaluation, updates them in the real tool execution/read paths, parses `evidence_files` into each `VerdictItem`, and returns `coverage_snapshot()` with the verdict.
2. `engine/pipeline.py::_run_ai_eval()` places the snapshot in `StepResult.data["coverage"]`; the stage fold may mirror it, but the top-level record is canonical.
3. `engine/judge.py` assigns the snapshot to `JudgeResult.coverage` for legacy and pipeline paths. Error and skipped paths construct the explicit `tier2_not_run` block.
4. `engine.persist.build_verdict_data()` normalizes the block, extracts the pipeline verdict when necessary, calls `downgrade_for_dropped_files()`, serializes the downgraded items, recomputes the persisted `passed`, `merge_recommendation`, `summary`, and `ungraded_criteria` from that local verdict, and writes `coverage` at top level.
5. The downgrade MUST use the existing GR-178 vocabulary and rendering path: `STATUS_NOT_VERIFIABLE`, `not_verifiable_reason`, `Coverage.not_verifiable`, `ungraded_note` where applicable, and `merge_recommendation`. No parallel “coverage uncertain” status is permitted.

- **R13** — The live pipeline and legacy paths MUST produce the same top-level coverage shape for equivalent observations.  
  `test:` `tests/test_pipeline.py::test_pipeline_and_legacy_coverage_shapes_match`.
- **R14** — Persistence MUST not mutate the caller's live `JudgeResult` or `Verdict`; the GR-178 downgrade is applied to a deep/local copy used for the record.  
  `test:` `tests/test_persist.py::test_persistence_coverage_downgrade_does_not_mutate_live_result`.
- **R15** — A persisted record with a downgraded item MUST never retain `passed=true` or `verdict="COMPLETE"`.  
  `test:` `tests/test_persist.py::test_coverage_downgrade_cannot_leave_persisted_green_verdict`.
- **R16** — No caller may infer coverage from `passed`, `items` count, `stages.tier2.passed`, or the summary; readers MUST use the top-level block.  
  `check:` `engine/persist.py` writes the block unconditionally and tests assert all four keys at the document root.

## 5. Error Catalog

| Condition | Required behavior | Observable result |
|---|---|---|
| No Tier 2 run | Emit empty lists and `tier2_not_run` | Mandatory block, no false “seen” claim |
| Empty diff | Emit the actual observed lists; do not synthesize files | `reason="complete"` only when evidence is complete |
| Scope excludes a required file | Put it in `files_dropped` | PASS becomes `NOT_VERIFIABLE` |
| Whole-file read exceeds `max_read_bytes` | Put the file in `files_dropped`; a range read belongs in `files_skimmed` | Reason names refusal |
| Range/head-tail read | Put the file in `files_skimmed` | It is not full evidence |
| Tool timeout/refusal/error | Put the affected file in `files_dropped` | Reason names tool failure |
| Invalid coverage payload | Normalize to empty lists and `tool_error` | Persistence remains non-fatal and honest |
| Dropped path disagrees with item evidence | Dropped state wins | GR-178 downgrade |

- **R17** — Coverage collection failures MUST never erase the verdict or cause persistence to omit the block; they MUST fail closed to `tier2_not_run` or `tool_error`.  
  `test:` `tests/test_coverage.py::test_coverage_collection_failure_still_emits_block`.

## 6. Edge Cases

- **R18** — An empty `files_dropped` list MUST not downgrade a PASS, and a complete criterion MUST remain PASS.  
  `test:` `tests/test_coverage.py::test_complete_coverage_preserves_pass`.
- **R19** — A huge file list MUST be handled without order-dependent behavior: normalize, deduplicate, sort, and persist all valid paths; no silent truncation is allowed.  
  `test:` `tests/test_coverage.py::test_huge_file_list_round_trips_without_silent_truncation`.
- **R20** — A dropped file listed in `files_seen` or `files_skimmed` MUST still win as dropped, and the reason MUST retain the concrete drop cause.  
  `test:` `tests/test_coverage.py::test_dropped_state_wins_disagreement`.
- **R21** — A criterion spanning two files is downgraded when either evidence file is dropped; a criterion naming only seen files remains eligible for PASS.  
  `test:` `tests/test_coverage.py::test_any_dropped_dependency_downgrades_multi_file_criterion`.
- **R22** — A malformed or missing `evidence_files` member MUST never be interpreted as proof that the criterion has no file dependency when any file was dropped.  
  `check:` `downgrade_for_dropped_files()` applies R10 before returning the local verdict.
- **R23** — Old verdicts without `coverage` remain readable; readers treat them as `reason="legacy_record_without_coverage"` and never upgrade them to a claimed full pass.  
  `test:` `tests/test_persist.py::test_legacy_record_without_coverage_is_not_upgraded`.

## 7. Testing

The implementation MUST add these exact scenarios:

- `tests/test_coverage.py::test_coverage_block_has_exact_keys_and_types`.
- `tests/test_coverage.py::test_coverage_paths_are_normalized_deduplicated_and_sorted`.
- `tests/test_coverage.py::test_coverage_rejects_escape_paths_and_urls`.
- `tests/test_coverage.py::test_pass_spanning_dropped_file_uses_gr178_not_verifiable`.
- `tests/test_coverage.py::test_missing_evidence_file_set_is_not_a_pass_under_partial_coverage`.
- `tests/test_coverage.py::test_huge_file_list_round_trips_without_silent_truncation`.
- `tests/test_coverage.py::test_dropped_state_wins_disagreement`.
- `tests/test_persist.py::test_coverage_is_mandatory_on_every_verdict_shape`.
- `tests/test_persist.py::test_coverage_downgrade_persists_gr178_needs_review`.
- `tests/test_persist.py::test_coverage_downgrade_cannot_leave_persisted_green_verdict`.
- `tests/test_pipeline.py::test_pipeline_and_legacy_coverage_shapes_match`.

The negative control MUST construct a PASS criterion whose `evidence_files` includes a dropped file and assert `NOT_VERIFIABLE`, `verdict="INCOMPLETE"`, `passed=false`, a settling reason, and `merge_recommendation="needs_review"`. A second negative control MUST assert that a dropped file cannot be hidden by listing it in `files_seen`.

- **R24** — The targeted suite MUST cover direct builder calls, pipeline results, legacy results, skipped Tier 2, malformed inputs, and persisted JSON readback.  
  `check:` `pytest -q tests/test_coverage.py tests/test_judge.py tests/test_pipeline.py tests/test_persist.py` reports all targeted GR-214 tests passing.

## 8. Hilo Impact

`engine/evaluator.py` is the observation producer; `engine/pipeline.py` transports the snapshot; `engine/judge.py` attaches it to `JudgeResult`; `engine/coverage.py` owns pure normalization and GR-178 downgrade logic; and `engine/persist.py` is the durable schema authority. `engine.gate` remains the sole authority for `NOT_VERIFIABLE` semantics and merge recommendation. GR-213 consumes the same result without changing coverage semantics. GR-215 filtering runs before final item serialization and MUST preserve this coverage downgrade ordering.
