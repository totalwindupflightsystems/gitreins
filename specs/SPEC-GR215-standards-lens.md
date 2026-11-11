# SPEC-GR215 — Standards Lens

## 1. Purpose

Prevent unsupported convention/style findings from entering verdict history by requiring a repository path that states the rule, dropping uncited convention findings at persistence, and honoring the repository's effective lint-ignore configuration.

This specification governs verdict criterion items and any structured finding that is copied into the durable verdict record. A rule citation is always a repository path, never a URL.

## 2. Interface

The implementation MUST extend the existing verdict item shape with these exact fields:

```python
from dataclasses import dataclass, field
from typing import Literal

FindingKind = Literal["correctness", "security", "convention", "other"]

@dataclass
class VerdictItem:
    criterion: str
    status: str
    detail: str
    resolution_probability: float | None = None
    cited_path: str | None = None
    derived: bool = False
    not_verifiable_reason: str | None = None
    evidence_files: list[str] = field(default_factory=list)
    finding_kind: FindingKind = "other"
    rule_id: str | None = None
    rule_citation: str | None = None
```

The standards filtering API MUST be:

```python
@dataclass(frozen=True)
class LintWaiver:
    source_path: str
    rule_id: str
    path_patterns: tuple[str, ...] = ()


def load_lint_waivers(workdir: str) -> tuple[LintWaiver, ...]: ...

def is_waived(
    finding: VerdictItem, *, path: str, waivers: tuple[LintWaiver, ...]
) -> bool: ...

def filter_standards_findings(
    workdir: str,
    items: list[VerdictItem],
    *,
    changed_paths: tuple[str, ...] = (),
) -> list[VerdictItem]: ...
```

`filter_standards_findings()` is a pure selection operation over the supplied item list apart from loading the repository's read-only config. It MUST be called by `engine.persist.build_verdict_data()` immediately before `_verdict_item_dict()` serializes items.

- **R1** — Only an item with `finding_kind == "convention"` is subject to the rule-citation requirement; correctness, security, and other findings retain their historical behavior.  
  `test:` `tests/test_standards_lens.py::test_only_convention_findings_require_rule_citation`.
- **R2** — A convention item MUST carry a non-empty `rule_citation` that is a repository-relative path to a regular file under `workdir`; URLs, absolute paths, `..` escapes, and empty strings are invalid.  
  `test:` `tests/test_standards_lens.py::test_rule_citation_accepts_repo_path_and_rejects_url_or_escape`.
- **R3** — A convention item without a valid citation MUST be removed by `filter_standards_findings()` and MUST not appear in `verdict.json`, `summary.md`, `judge_result_to_dict()`, or pipeline `items`.  
  `test:` `tests/test_standards_lens.py::test_uncited_convention_finding_is_dropped_at_persistence`.
- **R4** — `rule_citation` MUST contain the path only; line anchors and URL schemes MUST be rejected rather than normalized into a different identifier.  
  `check:` `engine/persist.py` never writes a citation that begins with `http:`, `https:`, `file:`, `/`, or `../`.
- **R5** — `rule_id` MUST be retained for a convention item and is the exact key used to match a lint waiver; matching by free-form detail text is forbidden.  
  `test:` `tests/test_standards_lens.py::test_waiver_matching_uses_rule_id_not_detail_text`.
- **R6** — A valid waiver MUST suppress the convention finding even when its `rule_citation` is absent; a waiver MUST never suppress a correctness or security finding.  
  `test:` `tests/test_standards_lens.py::test_lint_ignore_waiver_suppresses_only_matching_convention_finding`.
- **R7** — A finding with a citation to a real rule file MUST survive filtering when no matching waiver applies.  
  `test:` `tests/test_standards_lens.py::test_cited_nonwaived_convention_finding_survives`.

## 3. Data Model

The evaluator JSON item contract adds these optional members:

```json
{
  "criterion": "Use the repository's configured import ordering.",
  "status": "FAIL",
  "detail": "engine/foo.py:12 violates rule I001.",
  "finding_kind": "convention",
  "rule_id": "I001",
  "rule_citation": "pyproject.toml",
  "evidence_files": ["engine/foo.py"]
}
```

Persistence serializes `finding_kind`, `rule_id`, and `rule_citation` only when their values are non-default/non-empty. Existing items without these members remain readable as `finding_kind="other"`. A convention item with no citation is not serialized at all.

The supported effective lint-ignore sources and exact keys are:

| File | Section/key | Waiver interpretation |
|---|---|---|
| `pyproject.toml` | `[tool.ruff.lint].ignore` | Global rule ids |
| `pyproject.toml` | `[tool.ruff.lint.per-file-ignores]` | Rule ids keyed by fnmatch path |
| `setup.cfg`, `.flake8`, `tox.ini` | `[flake8].ignore`, `[flake8].extend-ignore` | Global rule ids |
| `setup.cfg`, `.flake8`, `tox.ini` | `[flake8].per-file-ignores` | Rule ids keyed by fnmatch path |
| `setup.cfg`, `tox.ini` | `[pycodestyle].ignore` | Global rule ids |

Values may be comma-, whitespace-, or newline-separated strings, or TOML arrays. The source path recorded in `LintWaiver.source_path` is repo-relative and is also a valid `rule_citation` target.

- **R8** — The parser MUST preserve `finding_kind`, `rule_id`, and `rule_citation` through live verdict, pipeline step, status dict, and persisted record paths.  
  `test:` `tests/test_standards_lens.py::test_standards_fields_survive_all_verdict_serializers`.
- **R9** — Persistence MUST omit all three optional fields for a default `other` item with no rule id/citation, preserving the historical item shape.  
  `test:` `tests/test_standards_lens.py::test_nonstandards_item_shape_remains_backward_compatible`.
- **R10** — Waiver parsing MUST read the effective files listed above in deterministic path order and merge all matching entries; it MUST not use a network or execute configuration code.  
  `check:` `load_lint_waivers()` uses `tomllib`, `configparser`, and the repository's YAML parser only.
- **R11** — A malformed or unreadable lint-ignore file MUST be ignored for waiver purposes and MUST NOT cause an uncited convention finding to survive.  
  `test:` `tests/test_standards_lens.py::test_malformed_lint_ignore_fails_closed_for_uncited_finding`.

## 4. Wiring

1. `engine/evaluator.py` updates the JSON-only prompt to require `finding_kind`, `rule_id` for convention findings, and `rule_citation` when the claim is not waived. `_try_parse_verdict()` preserves the fields and uses `other` for old responses.
2. `engine/pipeline.py::_verdict_item_data()` carries the fields into `StepResult.data["items"]`; it MUST not drop them before persistence has applied the waiver/citation policy.
3. `engine.judge.judge_result_to_dict()` exposes only findings that survive the same standards filter used by persistence. It MUST call the shared filter rather than implement a second rule.
4. `engine.persist.build_verdict_data()` loads waivers, filters items, serializes the survivors, and builds `summary` from the filtered verdict. The filter runs before the final persisted `items` list is constructed and after coverage downgrades from SPEC-GR214 are applied.
5. Existing `pyproject.toml`, `setup.cfg`, `.flake8`, and `tox.ini` files are ordinary rule sources. No new config key is added to `.gitreins/config.yaml`.
6. The standards lens applies to the persisted `review.findings` block only when a review finding explicitly carries `category="convention"`, `rule_id`, and `rule_citation`; untyped legacy review findings remain unchanged and are not reclassified by prose heuristics.

- **R12** — CLI, MCP, pipeline, and direct persistence callers MUST converge on one filter implementation and one waiver read.  
  `test:` `tests/test_standards_lens.py::test_cli_pipeline_and_direct_persistence_share_standards_filter`.
- **R13** — A dropped convention finding MUST not be resurrected by `judge_result_to_dict()` or by `_find_pipeline_verdict()`.  
  `test:` `tests/test_standards_lens.py::test_status_and_pipeline_views_do_not_resurrect_dropped_finding`.
- **R14** — A dropped FAIL finding MUST never turn a persisted verdict green: `passed` remains false unless an independent surviving failure also determines the outcome exactly as before.  
  `test:` `tests/test_standards_lens.py::test_dropping_uncited_fail_finding_cannot_create_pass`.
- **R15** — The coverage and GR-178 downgrade from SPEC-GR214 MUST run before standards filtering, so a dropped-file criterion cannot be made PASS by removing its convention item.  
  `check:` `build_verdict_data()` order is coverage normalization/downgrade, waiver/citation filtering, then final outcome and serialization.

## 5. Error Catalog

| Condition | Required behavior | Observable result |
|---|---|---|
| Convention item has no citation | Drop at persistence | No item in any durable/output serializer |
| Citation is a URL or path escape | Drop at persistence | No normalized substitute is written |
| Citation points to missing/non-file path | Drop at persistence | No unsupported rule claim survives |
| Matching lint-ignore waiver exists | Drop as waived | No finding and no blocking outcome from that finding |
| Waiver has wrong rule id | Do not waive | Citation is still required |
| Malformed config | Ignore malformed waiver source | Uncited item remains dropped |
| Non-convention item lacks citation | Preserve | Historical behavior unchanged |
| Filter receives non-list items | Skip malformed item | Persistence remains non-fatal |

- **R16** — Standards-filter errors MUST fail closed for convention evidence and MUST never cause a persistence exception to escape `persist_evaluation()`.  
  `test:` `tests/test_standards_lens.py::test_filter_errors_fail_closed_and_persistence_is_nonfatal`.

## 6. Edge Cases

- **R17** — An empty finding list MUST remain empty and MUST not cause a synthetic convention finding.  
  `test:` `tests/test_standards_lens.py::test_empty_findings_remain_empty`.
- **R18** — A huge finding list MUST preserve non-convention findings and apply the same citation/waiver decision independently to every convention item; filtering MUST not stop after the first invalid entry.  
  `test:` `tests/test_standards_lens.py::test_huge_finding_list_filters_each_item_deterministically`.
- **R19** — A rule citation to `pyproject.toml` MUST be accepted when that file contains the stated rule, while a citation to an unrelated existing file MUST be rejected by the citation validator.  
  `test:` `tests/test_standards_lens.py::test_citation_path_must_be_a_rule_stating_file`.
- **R20** — A per-file waiver MUST match only paths selected by its fnmatch pattern; a global waiver MUST match every path for that rule id.  
  `test:` `tests/test_standards_lens.py::test_global_and_per_file_waiver_scope`.
- **R21** — `E501` in `pyproject.toml` per-file ignores and `B001` in `setup.cfg` per-file ignores MUST be recognized using the repository's actual config syntax.  
  `test:` `tests/test_standards_lens.py::test_repo_lint_ignore_config_syntax_is_honored`.
- **R22** — A citation path MUST be evaluated relative to the judged `workdir`, never relative to the process current directory.  
  `test:` `tests/test_standards_lens.py::test_rule_citation_resolves_against_workdir`.
- **R23** — A convention finding whose rule is waived MUST be dropped before the citation requirement is evaluated; a waiver is not a citation and MUST not be persisted as one.  
  `check:` `filter_standards_findings()` applies waiver suppression without manufacturing `rule_citation`.
- **R24** — A dropped convention finding MUST not change the task criterion count, but a surviving dropped-file `NOT_VERIFIABLE` item from SPEC-GR214 MUST continue to route to GR-178 human review.  
  `test:` `tests/test_standards_lens.py::test_standards_filter_does_not_bypass_coverage_review`.

## 7. Testing

The implementation MUST add `tests/test_standards_lens.py` with these exact scenarios:

- `test_only_convention_findings_require_rule_citation` — category boundary.
- `test_rule_citation_accepts_repo_path_and_rejects_url_or_escape` — path safety.
- `test_uncited_convention_finding_is_dropped_at_persistence` — required poisoned-finding negative control.
- `test_cited_nonwaived_convention_finding_survives` — positive citation.
- `test_lint_ignore_waiver_suppresses_only_matching_convention_finding` — waiver isolation.
- `test_waiver_matching_uses_rule_id_not_detail_text` — no prose matching.
- `test_repo_lint_ignore_config_syntax_is_honored` — actual `pyproject.toml`/`setup.cfg` forms.
- `test_malformed_lint_ignore_fails_closed_for_uncited_finding` — malformed config.
- `test_standards_fields_survive_all_verdict_serializers` — live/pipeline/status/persisted parity.
- `test_dropping_uncited_fail_finding_cannot_create_pass` — fail-closed outcome.
- `test_standards_filter_does_not_bypass_coverage_review` — GR-178 integration.

The required negative control is a poisoned model response containing a convention FAIL with no `rule_citation`, an invented URL citation, and a detail string that names a real rule. All three MUST be absent from persisted `items`; the non-convention control item MUST remain.

- **R25** — The targeted suite MUST verify real file readback from `verdict.json` and `summary.md`, not only in-memory lists.  
  `check:` `pytest -q tests/test_standards_lens.py tests/test_persist.py tests/test_judge.py` reports all targeted GR-215 tests passing.

## 8. Hilo Impact

`engine/evaluator.py` is the producer of finding kind, rule id, and citation; `engine/pipeline.py` is a transport-only seam; the shared standards filter owns config interpretation; `engine.judge` and `engine.persist` are consumers. `engine/config_schema.py` is unchanged because the effective lint-ignore files are tool configuration, not GitReins settings. SPEC-GR213's judge-derived failure modes are independent of convention findings. SPEC-GR214's coverage downgrade runs first and remains authoritative for `NOT_VERIFIABLE`; GR-215 MUST not remove or weaken that path.
