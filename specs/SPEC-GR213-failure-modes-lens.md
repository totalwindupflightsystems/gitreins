# SPEC-GR213 — Failure-Modes Lens

## 1. Purpose

Make every persisted Tier 2 verdict expose a bounded, judge-derived list of concrete residual failure modes without allowing task authors to manufacture review criteria or allowing an unsupported detector claim to read as coverage.

This specification is the design authority for the `failure_modes` extension. It applies to the live `Verdict` object, the pipeline step payload, `judge_result_to_dict`, and the record emitted by `engine.persist.build_verdict_data`.

## 2. Interface

The implementation MUST keep these exact Python interfaces:

```python
from collections.abc import Iterable
from dataclasses import dataclass, field

@dataclass
class FailureMode:
    what: str = ""
    where: str = ""
    trigger: str = ""
    detected_by: str = ""
    covered_in_this_pr: bool = False

@dataclass
class Verdict:
    verdict: str
    items: list[VerdictItem] = field(default_factory=list)
    summary: str = ""
    prescreen: dict[str, Any] | None = None
    failure_modes: list[FailureMode] = field(default_factory=list)

MAX_FAILURE_MODES: int = 3

def failure_mode_test_refs(detected_by: str) -> list[str]: ...
def detected_by_is_backed(detected_by: str, run_record: Iterable[str]) -> bool: ...
def failure_mode_summary_flags(
    verdict: Verdict, run_record: Iterable[str]
) -> list[str]: ...
```

`FailureMode` is the only in-memory representation. No second failure-mode class may be introduced in `engine/pipeline.py`, `engine/judge.py`, or `engine/persist.py`.

- **R1** — `Verdict.failure_modes` MUST default to a new empty list and MUST NOT share mutable state between verdict instances.  
  `test:` `tests/test_judge.py::test_failure_modes_default_is_empty_and_not_shared`.
- **R2** — The parser MUST accept zero through three object entries and MUST ignore non-object entries.  
  `test:` `tests/test_judge.py::test_failure_modes_parser_accepts_empty_and_ignores_non_objects`.
- **R3** — Entries after the first three MUST be discarded deterministically, without changing `verdict`, `items`, or `summary` solely because the bound was exceeded.  
  `test:` `tests/test_judge.py::test_failure_modes_parser_caps_at_three`.
- **R4** — `failure_modes` MUST be read from the evaluator's JSON response, never from `Task.criteria`, task metadata, or a caller-supplied `extra` dictionary.  
  `check:` `engine/evaluator.py` parser tests show criteria containing a forged `failure_modes` object do not populate `Verdict.failure_modes`.
- **R5** — Each retained entry MUST contain non-empty `what`, `where`, `trigger`, and `detected_by`; `detected_by` MUST name a concrete test node/path or an alert identifier. Invalid entries MUST be dropped before the verdict is returned.  
  `test:` `tests/test_judge.py::test_failure_mode_without_concrete_detector_is_dropped`.
- **R6** — A test detector is concrete only when `failure_mode_test_refs()` extracts a pytest-style node id, a test-file path, or a `test_*` identifier; an alert detector is concrete only when it uses the literal `alert:` prefix followed by a non-empty alert name.  
  `test:` `tests/test_judge.py::test_failure_mode_detector_requires_test_or_alert_identifier`.
- **R7** — `covered_in_this_pr` MUST be computed by the judge from the actual diff and recorded test/alert evidence; the evaluator MUST ignore a model claim of `true` when the required evidence is absent.  
  `test:` `tests/test_judge.py::test_failure_mode_coverage_is_judge_derived_not_model_derived`.
- **R8** — A test named by `detected_by` MUST be present in the evaluator tool record before it can support `covered_in_this_pr=True`; an unbacked test citation MUST produce a summary flag naming the uncovered failure path.  
  `test:` `tests/test_judge.py::test_unbacked_failure_mode_detector_degrades_summary`.
- **R9** — An alert detector MUST be preserved as an identifier, but it MUST set `covered_in_this_pr=False` unless the run record contains an explicit `alert:<name>` result.  
  `test:` `tests/test_judge.py::test_alert_detector_requires_recorded_alert_for_coverage`.
- **R10** — `failure_mode_summary_flags()` MUST return an empty list for an empty failure-mode block and MUST leave the original summary byte-for-byte unchanged in that case.  
  `test:` `tests/test_judge.py::test_empty_failure_modes_are_unpenalised`.

## 3. Data Model

The live evaluator response accepts this exact additive JSON member:

```json
{
  "verdict": "COMPLETE",
  "items": [],
  "summary": "All requested criteria were checked.",
  "failure_modes": [
    {
      "what": "A missing plugin directory raises during startup.",
      "where": "engine/plugins.py:load_plugins",
      "trigger": "A clean worktree has no plugins directory.",
      "detected_by": "tests/test_plugins.py::test_missing_plugin_directory",
      "covered_in_this_pr": true
    }
  ]
}
```

The persisted `verdict.json` uses the same five keys and types. `failure_modes` is omitted, rather than written as `[]`, when the retained list is empty; this preserves the historical shape for verdicts with no residual failure mode. The field is present when at least one valid entry survives parsing.

- **R11** — `what`, `where`, `trigger`, and `detected_by` MUST serialize as strings and `covered_in_this_pr` MUST serialize as a JSON boolean.  
  `test:` `tests/test_judge.py::test_failure_modes_persist_with_exact_five_fields_and_types`.
- **R12** — Persistence MUST cap the serialized list at three even when a duck-typed verdict object bypasses the evaluator parser.  
  `test:` `tests/test_persist.py::test_persist_caps_duck_typed_failure_modes`.
- **R13** — `judge_result_to_dict()` MUST expose the same five-field entries only when the list is non-empty; it MUST not expose an author-controlled replacement block.  
  `test:` `tests/test_judge.py::test_status_dict_exposes_only_judge_failure_modes`.
- **R14** — A persisted record written before this field existed MUST remain readable as if it had an empty failure-mode list.  
  `test:` `tests/test_persist.py::test_legacy_verdict_without_failure_modes_remains_readable`.

## 4. Wiring

1. `engine/evaluator.py` extends the JSON-only system prompt with the failure-mode object contract, parses the bounded list in `_try_parse_verdict`, validates detector syntax, and applies `failure_mode_summary_flags()` against the evaluator's `_tool_record`.
2. `engine/pipeline.py::_run_ai_eval()` carries the parsed list through `StepResult.data["failure_modes"]` using the same five-field serializer used for `items`.
3. `engine/judge.py::JudgeResult.summary` renders a non-empty block after the GR-178 human-review block and before the final overall status. `judge_result_to_dict()` exposes the additive machine-readable block.
4. `engine/persist.py::build_verdict_data()` is the persistence authority. It extracts the pipeline verdict when `result.verdict` is absent, serializes only validated entries, and writes the block at the verdict top level.
5. No CLI flag, config key, MCP route, or task criterion is added. Existing CLI/MCP callers reach this behavior through the existing judge and shared persistence paths.

- **R15** — The pipeline, judge status, and persistence paths MUST all expose identical five-field values for one run.  
  `test:` `tests/test_persist.py::test_failure_modes_are_identical_across_pipeline_status_and_persisted_record`.
- **R16** — A Tier 1-only verdict MUST not invent failure modes and MUST omit the persisted block.  
  `test:` `tests/test_persist.py::test_tier1_only_verdict_has_no_failure_modes`.
- **R17** — The evaluator MUST not claim that a test ran unless a corresponding `run_command` tool record is present.  
  `check:` `engine/evaluator.py` and `tests/test_judge.py` contain a negative control with a fabricated test citation and no command record.

## 5. Error Catalog

| Condition | Required behavior | Observable result |
|---|---|---|
| `failure_modes` is absent | Treat it as an empty list | Historical verdict behavior; no added summary text |
| `failure_modes` is not a list | Treat it as empty and retain the rest of the verdict | No parser exception |
| Entry is not an object | Drop the entry | Remaining entries survive |
| Required text is empty or non-concrete detector | Drop the entry | No invalid entry reaches `Verdict` or persistence |
| More than three entries | Keep first three in response order | No error; later entries are absent |
| Test citation has no tool record | Keep the valid entry, force `covered_in_this_pr=False`, flag summary | Explicit `uncovered failure path` text |
| Persistence receives a malformed duck-typed entry | Skip that entry | Persistence remains non-fatal and never writes malformed fields |

- **R18** — None of these conditions may turn a parseable verdict into a raised exception or a false `COMPLETE`.  
  `test:` `tests/test_judge.py::test_failure_mode_malformed_inputs_fail_closed_without_parser_exception`.

## 6. Edge Cases

- **R19** — An empty diff MAY produce `failure_modes=[]`, but it MUST not produce a synthetic failure mode merely to fill the block.  
  `test:` `tests/test_judge.py::test_empty_diff_does_not_pad_failure_modes`.
- **R20** — A huge failure-mode list MUST be bounded before persistence and MUST preserve the first three entries in model order.  
  `test:` `tests/test_judge.py::test_huge_failure_mode_list_is_bounded`.
- **R21** — A detector that names a test and an alert MUST be considered backed only when both identifiers are present in the run record; one missing detector makes `covered_in_this_pr=False`.  
  `test:` `tests/test_judge.py::test_mixed_detector_requires_all_detector_evidence`.
- **R22** — `covered_in_this_pr=False` is an honest result, not a parser failure; the entry remains visible so the reviewer can see the residual risk.  
  `check:` `tests/test_judge.py::test_uncovered_failure_mode_remains_visible`.
- **R23** — A model-supplied failure mode that is also present in task criteria MUST be treated as the independent judge lens, not as a second criterion and not as a criterion-count increment.  
  `test:` `tests/test_judge.py::test_failure_mode_does_not_change_criterion_coverage`.

## 7. Testing

The implementation MUST add or update tests with these exact scenarios:

- `tests/test_judge.py::test_failure_modes_default_is_empty_and_not_shared` — dataclass default isolation.
- `tests/test_judge.py::test_failure_modes_parser_caps_at_three` — four entries retain three.
- `tests/test_judge.py::test_failure_mode_without_concrete_detector_is_dropped` — poisoned empty/prose detector is rejected.
- `tests/test_judge.py::test_failure_mode_coverage_is_judge_derived_not_model_derived` — forged `true` is not trusted.
- `tests/test_judge.py::test_unbacked_failure_mode_detector_degrades_summary` — absent command evidence is named.
- `tests/test_judge.py::test_empty_failure_modes_are_unpenalised` — no summary drift for `[]` or omission.
- `tests/test_persist.py::test_failure_modes_are_identical_across_pipeline_status_and_persisted_record` — end-to-end shape parity.
- `tests/test_persist.py::test_tier1_only_verdict_has_no_failure_modes` — negative control for no Tier 2.
- `tests/test_persist.py::test_legacy_verdict_without_failure_modes_remains_readable` — backward read.

The negative controls MUST include a poisoned convention finding without a citation (owned by SPEC-GR215) and a failure-mode test citation that was never run. The latter MUST be visibly flagged rather than silently accepted.

- **R24** — The targeted suite MUST exercise parser, live object, pipeline serialization, status serialization, and `build_verdict_data()` persistence.  
  `check:` `pytest -q tests/test_judge.py tests/test_pipeline.py tests/test_persist.py` reports all targeted GR-213 tests passing.

## 8. Hilo Impact

The producer is `engine/evaluator.py`; the transport seam is `engine/pipeline.py`; the presentation seam is `engine/judge.py`; and the durable schema authority is `engine/persist.py`. Consumers of `judge_result_to_dict()` and history readers receive the additive block. No task-manager, guard, config-schema, CLI, or MCP contract changes are required. GR-214 consumes the same verdict object but owns coverage downgrades; GR-215 consumes the same persistence boundary but owns convention filtering. A worker MUST implement those cross-spec seams without duplicating serializers.
