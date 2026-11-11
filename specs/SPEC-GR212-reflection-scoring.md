# SPEC-GR212 — Reflection Scoring

## 1. Purpose
Add an independent second reflection call that scores every candidate code-review finding from 0–10 under deterministic category caps and drops findings below the configured threshold before persistence.

This specification extends the existing `FindingReflector`/`SecondPassReviewer` path in `engine/review_profiles.py`; it does not replace the commit-audit `ReviewIssue` score, `review_score_threshold`, or `review_score_offset` path in `engine/commit_audit.py`.

## 2. Interface

The worker modifies `engine/review_profiles.py` with these exact signatures.

```python
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

REFLECTION_SCORE_MIN: int = 0
REFLECTION_SCORE_MAX: int = 10
DEFAULT_FINDING_THRESHOLD: int = 5

@dataclass(frozen=True)
class ReflectionFindingScore:
    index: int
    score: int
    raw_score: int
    cap: int
    category: str
    reason: str = ""
    score_unassigned: bool = False

    def to_dict(self) -> dict[str, Any]: ...

@dataclass
class ReflectionScoringResult:
    kept: list[SecondPassFinding] = field(default_factory=list)
    scores: list[ReflectionFindingScore] = field(default_factory=list)
    dropped: list[DroppedFinding] = field(default_factory=list)
    threshold: int = DEFAULT_FINDING_THRESHOLD
    error: str | None = None

    def to_dict(self) -> dict[str, Any] | None: ...

class ReflectionScorer:
    def __init__(
        self,
        llm: Any,
        workdir: str,
        threshold: int = DEFAULT_FINDING_THRESHOLD,
    ) -> None: ...

    def score(
        self,
        findings: Sequence[SecondPassFinding],
        diff_text: str,
    ) -> ReflectionScoringResult: ...


def reflection_category_cap(category: str) -> int: ...

def clamp_reflection_score(raw_score: Any, category: str) -> tuple[int | None, str | None]: ...

def reflection_score_from_dict(data: Any) -> tuple[ReflectionFindingScore | None, str | None]: ...
```

The existing `FindingReflector` remains available with its exact current signature:

```python
class FindingReflector:
    def __init__(self, llm: Any, workdir: str, threshold: float = DEFAULT_REFLECTION_THRESHOLD) -> None: ...
    def reflect(self, findings: list[SecondPassFinding], diff_text: str | None = None) -> ReflectionResult: ...
```

The implementation replaces its score policy with `ReflectionScorer` or delegates to it, while preserving `reflect(...)` as a compatibility adapter returning `ReflectionResult`. The existing `SecondPassReviewer` signature remains exact:

```python
class SecondPassReviewer:
    def __init__(self, llm: Any, workdir: str, max_findings: int = MAX_SECOND_PASS_FINDINGS) -> None: ...
    def review(self, diff_text: str | None = None) -> SecondPassResult: ...
```

R1. `ReflectionScoringResult` MUST score each candidate by index and MUST preserve input ordering in `scores`, `kept`, and `dropped`.

`test:` provide three findings in order and assert score entries have indices `[0, 1, 2]`, with kept and dropped findings retaining their original relative order.

R2. A reflection score MUST be an integer from 0 through 10 inclusive after parsing and cap enforcement; booleans, floats, numeric strings, missing values, and non-finite values MUST NOT become scores.

`test:` pass each invalid value to `clamp_reflection_score` or `reflection_score_from_dict` and assert a validation failure, then pass 0 and 10 and assert acceptance.

R3. `reflection_category_cap("verify-only")` MUST return `7`; `reflection_category_cap("error-handling")` MUST return `8`.

`test:` assert the exact two cap values.

R4. `reflection_category_cap` MUST return `0` for `"docstring"`, `"type-hint"`, `"exception-narrowing"`, and `"question-a-declaration"`, including case-insensitive and underscore/hyphen-equivalent spellings.

`test:` assert all eight spellings (`docstring`, `DOCSTRING`, `type_hint`, `exception_narrowing`, `question-a-declaration`, etc.) return zero.

R5. An unknown category MUST have cap `10`, not a silent zero or an inherited cap from another category.

`test:` assert `reflection_category_cap("security") == 10`.

R6. `clamp_reflection_score` MUST return `(min(raw_score, cap), None)` for a valid integer score above the category cap and MUST return `(0, reason)` for a valid score when the category cap is zero; no category cap may increase a score.

`test:` assert `(10, "verify-only")` becomes `7` and `(9, "docstring")` becomes `0` with a reason naming the hard cap.

R7. The reflection system prompt MUST instruct the scorer to score each candidate 0–10, return one entry per index, and never add, remove, or rewrite findings.

`check:` assert the prompt contains `0-10`, `index`, and a prohibition against adding findings.

R8. The scorer MUST send the generator's candidate findings and the diff in a fresh message list to a separate `llm.chat(...)` call; it MUST NOT receive, append to, or reuse the generator's conversation messages.

`test:` use an LLM spy that records object identities and messages, run `SecondPassReviewer.review` followed by `ReflectionScorer.score`, and assert the second call has exactly one system and one user message, contains no generator transcript/tool calls, and has a distinct list object.

R9. `ReflectionScorer.score` MUST apply caps and threshold in Python after parsing; the model's declared score MUST never bypass a cap or threshold.

`test:` return score 10 for `verify-only` and threshold 7, assert the score is stored as 7 and the finding is kept; return score 6 for `verify-only`, assert it is dropped.

R10. A finding MUST be kept only when its capped score is greater than or equal to `threshold`; below-threshold findings MUST be dropped and named in `DroppedFinding.reason`.

`test:` with threshold 5, assert score 4 drops and score 5 keeps.

R11. The default threshold MUST be `5`, and a configured threshold MUST be an integer from 0 through 10; invalid threshold configuration MUST fall back to 5 and record a warning without widening the filter.

`test:` construct `ReflectionScorer` with `True`, `-1`, `11`, `"five"`, and `None`, assert `threshold == 5`; construct with 0 and 10 and assert those values are retained.

R12. A reflection LLM exception, timeout, unavailable response, or unparseable response MUST default every candidate to BELOW threshold, set `score_unassigned: true` for every dropped score record, set `error`, and return without raising.

`test:` use an LLM that raises `RuntimeError("boom")`, assert `kept == []`, `len(dropped) == len(findings)`, `error` contains `boom`, and every `ReflectionFindingScore.score_unassigned is True`.

R13. A missing, malformed, duplicate, or out-of-range score entry MUST default only its indexed candidate to BELOW threshold with `score_unassigned: true`; valid entries for other indices MUST still be applied.

`test:` return one valid score, one missing score, and one duplicate index, then assert only the valid candidate can be kept and all unresolved candidates carry `score_unassigned`.

R14. The scorer MUST never invoke another reviewer, inspect task acceptance criteria, or score a generated finding not present in its input sequence.

`check:` assert `ReflectionScorer` has no criteria parameter and the prompt includes only the supplied findings and diff.

R15. The scorer MUST retain the exact candidate finding object data in each `DroppedFinding.finding`; it MUST not paraphrase a finding while recording a drop.

`test:` drop a finding with multiline `detail` and assert the dropped record's `finding.detail` is byte-identical.

R16. The score result MUST expose `raw_score`, capped `score`, `cap`, `category`, `reason`, and `score_unassigned` for each candidate, so a consumer can distinguish a cap from a failure.

`test:` serialize a capped score and assert all six keys and their exact values are present.

## 3. Data Model

### 3.1 Candidate input

The candidate input is the existing `SecondPassFinding` JSON shape, extended by GR-211's `category` field:

```json
{
  "severity": "high",
  "file": "engine/pipeline.py",
  "line": 42,
  "detection": "error path is swallowed",
  "detail": "The exception branch returns success.",
  "category": "error-handling"
}
```

### 3.2 Reflection request

The user message sent to the independent scorer is exactly this structure in text form, with JSON values escaped by `json.dumps`:

```text
## DIFF UNDER REVIEW
<diff_text>

## CANDIDATE FINDINGS TO SCORE
[{"index":0,"severity":"high","file":"...","line":42,"detection":"...","detail":"...","category":"..."}]

Score each candidate per your instructions and output the JSON scores.
```

The system prompt requires this response object:

```json
{
  "scores": [
    {
      "index": 0,
      "score": 8,
      "reason": "The cited branch is present in the diff."
    }
  ],
  "summary": "One sentence."
}
```

### 3.3 Persisted review block

`SecondPassResult.to_dict()` retains its current keys and adds `reflection` only when scoring dropped findings or failed. The `reflection` object is:

```json
{
  "threshold": 5,
  "scores": [
    {
      "index": 0,
      "score": 7,
      "raw_score": 10,
      "cap": 7,
      "category": "verify-only",
      "reason": "category cap applied",
      "score_unassigned": false
    }
  ],
  "dropped": 1,
  "dropped_findings": [
    {
      "index": 1,
      "severity": "low",
      "file": "engine/a.py",
      "line": 9,
      "detection": "x",
      "reason": "score 4 below threshold 5"
    }
  ],
  "score_unassigned": false,
  "error": null
}
```

`error` is omitted when null for compatibility. `score_unassigned` at the reflection-block level is `true` if and only if any candidate has an unassigned score. A failed reflection MUST include all candidate drops and `error`.

R17. The request JSON MUST carry exactly one score candidate per input finding index and MUST include category for every candidate, defaulting old findings to `unknown`.

`test:` inspect the scorer's user prompt after a three-finding call and assert the index set is exactly `{0, 1, 2}` and no extra candidate exists.

R18. The persisted reflection block MUST be absent when no findings were supplied and MUST be present on any drop or reflection failure.

`test:` assert `ReflectionScoringResult.to_dict() is None` for an empty input and is non-null for a below-threshold score and an LLM failure.

R19. A reflection failure MUST persist `score_unassigned: true` and a non-empty `error`; it MUST never serialize as a clean empty `scores` result.

`test:` serialize the failure result and assert the exact failure markers.

R20. Existing pre-GR-212 reflection records using `correctness` and `evidence` fields MUST remain readable by `FindingReflector.reflect`; the adapter maps `max(correctness, evidence) * 10` to an integer score only for legacy reads, while all new calls use the single 0–10 `score` field.

`test:` feed the existing correctness/evidence fixture to `FindingReflector` and assert it remains parseable; feed a new score fixture and assert the new path is selected.

## 4. Wiring

1. `engine/review_profiles.py` owns `ReflectionScorer`, category-cap constants, score parsing, score result serialization, and the compatibility `FindingReflector` adapter.
2. `engine/judge.py` keeps `_run_second_pass(result: JudgeResult, skip_tier2: bool = False) -> None`. It constructs `SecondPassReviewer(self.llm, self.scan_root, max_findings=int(max_findings))`, calls `review()`, then constructs `ReflectionScorer(self.llm, self.scan_root, threshold=review_cfg.get("reflection_threshold", 5))` with the same injected LLM object but a fresh request context, and calls `score(review.findings, collect_review_diff(self.scan_root))`.
3. `review.reflection` defaults to enabled for the GR-212 path. `review.reflection: false` disables the call and leaves the existing review shape unchanged. `review.reflection_threshold` defaults to 5.
4. The scorer's output replaces `review.findings` with `scoring.kept`; `review.reflection` receives `scoring.to_dict()` when non-null; then `result.second_pass_review = review.to_dict()`.
5. `engine/persist.py` continues to serialize `result.second_pass_review` under the existing top-level `review` key through `build_verdict_data(...)`; no new persistence writer is introduced.
6. `engine/pipeline.py` and `engine/commit_audit.py` are not used as the reflection scoring implementation. The existing commit-audit `CommitAuditor.review(...) -> CommitReviewResult` path retains its own `ReviewIssue.score` 1–10 contract.

R21. The generator call and reflection call MUST be separate invocations of `llm.chat`; reusing the generator's `messages` list or adding reflection messages to it violates the independent-context requirement.

`test:` assert the spy's two calls have disjoint message-list identities and the reflection messages contain neither the generator's system prompt nor tool-call transcript.

R22. The reflection call MUST execute after `SecondPassReviewer.review()` has parsed and capped candidate findings and before `result.second_pass_review` is assigned.

`check:` inspect `_run_second_pass` call order or use an event-recording LLM and assert events are `generator_call`, `reflection_call`, `persisted_review`.

R23. `persist_evaluation(workdir: str, task: Task, result: JudgeResult, *, extra: dict[Any, Any] | None = None, collect_evidence: Callable[[str], dict[Any, Any]] | None = None) -> str` MUST remain the only evaluation persistence entry point and MUST receive the reflection block through `JudgeResult.second_pass_review`.

`check:` assert no reflection code writes `.gitreins/history/` or calls `VerdictPersister.persist` directly.

## 5. Error Catalog

| Error | Trigger | Caller-visible result |
|---|---|---|
| `reflection_threshold_invalid` | bool, non-integer, or outside 0–10 | Threshold 5; warning; review continues |
| `reflection_llm_failure` | `llm.chat` raises or times out | All candidates dropped; `score_unassigned: true`; `error` text; no raise |
| `reflection_reply_unparseable` | no JSON object or no `scores` list | All candidates dropped; unassigned markers; no raise |
| `reflection_score_invalid` | missing, bool, float, string, NaN, infinity, or outside 0–10 | That candidate dropped; unassigned marker; valid siblings continue |
| `reflection_index_invalid` | non-integer, negative, duplicate, or out-of-range index | That candidate is unassigned/dropped; valid siblings continue |
| `reflection_category_unknown` | category absent in legacy input | Category `unknown`; cap 10; no error |
| `reflection_empty_input` | no findings | No LLM call; empty result; reflection block absent |

R24. Every catalogued error MUST be represented in a deterministic test and MUST leave the judge able to return a `JudgeResult`; only the candidate(s) affected by a malformed score are dropped, except a call-wide failure which drops all candidates.

`test:` parameterize the error table and assert no case raises from `Judge._run_second_pass`.

R25. Reflection failure MUST default BELOW threshold even when the configured threshold is 0; failure is represented by `score_unassigned`, not by a synthetic passing score.

`test:` use threshold 0 with a raising LLM and assert every candidate is dropped.

## 6. Edge Cases

R26. Empty findings MUST cause zero reflection LLM calls and produce no reflection block.

`test:` use an LLM spy and assert `calls == []`.

R27. A threshold of 0 MUST keep every successfully scored candidate, including score 0; an unassigned candidate still drops under R25.

`test:` score one candidate 0 with threshold 0 and assert it is kept; then fail the LLM and assert it is dropped.

R28. A threshold of 10 MUST keep only successfully scored candidates whose capped score is 10; `verify-only` and `error-handling` scores cannot pass threshold 10 because their caps are 7 and 8.

`test:` score three categories at 10 with threshold 10 and assert only the uncapped category survives.

R29. Category matching MUST be case-insensitive and treat `_`, `-`, and spaces as equivalent; the canonical stored category is lower-case with hyphens.

`test:` assert `reflection_category_cap("Error Handling") == reflection_category_cap("error_handling") == 8`.

R30. A category hard-capped at zero MUST drop at every threshold greater than zero and MUST still drop on unassigned failure at threshold zero.

`test:` exercise `docstring` at thresholds 0 and 1 with a valid score and a failed call.

R31. More than 10 candidate findings MUST be rejected by `SecondPassReviewer`'s existing `max_findings` cap before reflection; reflection MUST never score beyond the generator's supplied list.

`test:` provide 13 generator findings with `max_findings=10`, assert the scorer receives 10, and assert no score index is 10 or greater.

R32. A huge diff MUST be passed through the same bounded context policy as the existing reflection path; the scorer MUST use `collect_review_diff` and the established diff truncation limit, not silently request unbounded input.

`check:` assert the user prompt's diff portion is at most 15,000 characters and uses `collect_review_diff` for the default path.

R33. Generator and scorer failures MUST not mutate the caller's original findings list or message list.

`test:` retain copies before `score` and assert equality after both success and failure.

R34. Duplicate candidate indices in the model reply MUST not make a candidate pass by last-write-wins ambiguity; the duplicate candidate is unassigned and dropped.

`test:` return duplicate index 0 entries and assert finding 0 is dropped with `score_unassigned: true`.

R35. Reflection output MUST preserve exact finding detail and category text in kept and dropped objects; only score metadata is transformed.

`test:` use Unicode, multiline detail and a non-ASCII filename and assert exact equality in both result lists.

## 7. Testing

The implementation test module is `tests/test_gr212_reflection_scoring.py`. It uses a recording fake implementing the real `LLMClient.chat` seam and invokes the real `SecondPassReviewer`, `ReflectionScorer`, `FindingReflector`, and `Judge._run_second_pass` wiring.

| Test | Expected outcome |
|---|---|
| `test_verify_only_cap_is_seven` | R3/R6 pass |
| `test_error_handling_cap_is_eight` | R3/R6 pass |
| `test_hard_zero_categories` | R4 passes for all four categories |
| `test_unknown_category_cap_is_ten` | R5 passes |
| `test_score_is_integer_and_bounded` | R2 passes |
| `test_threshold_is_inclusive` | R10 passes at 4/5 |
| `test_independent_context_has_no_generator_transcript` | R8/R21 pass |
| `test_reflection_failure_drops_all_and_marks_unassigned` | R12/R19/R25 pass |
| `test_malformed_one_does_not_hide_valid_sibling` | R13 passes |
| `test_duplicate_index_is_not_last_write_wins` | R34 passes |
| `test_empty_findings_skips_llm` | R26 passes |
| `test_threshold_zero_keeps_valid_zero_but_not_failure` | R27 passes |
| `test_hard_caps_cannot_reach_threshold_ten` | R28/R30 pass |
| `test_diff_context_is_bounded` | R32 passes |
| `test_legacy_reflection_shape_remains_readable` | R20 passes |
| `test_judge_wires_generator_then_reflection_then_result` | R22 passes |

R36. The suite MUST include a falsifier that supplies a reflection reply score of 10 for `verify-only` and asserts the persisted score is 7; an implementation that trusts the model score without a Python cap fails.

`test:` run `test_verify_only_model_ten_is_capped_to_seven` against the real scorer and assert `score == 7` and `raw_score == 10`.

R37. The suite MUST include a negative control that passes the same generator conversation object into the scorer and asserts the independent-context test fails; the production implementation MUST pass the independent test.

`test:` invoke a deliberately broken scorer that appends to the generator messages and assert `test_independent_context_has_no_generator_transcript` raises an assertion.

R38. The suite MUST run the existing review-profile tests unchanged in addition to the new GR-212 module.

`check:` run `pytest -q tests/test_review_profiles.py tests/test_gr212_reflection_scoring.py` and require exit code 0.

## 8. Hilo Impact

- **Depends on:** `engine/review_profiles.py` `SecondPassFinding`, `SecondPassReviewer`, `FindingReflector`, `collect_review_diff`, `_extract_json_object`, and the existing `SecondPassResult`/`ReflectionResult` serialization contracts; `engine/judge.py` `_run_second_pass`; `engine/persist.py` `build_verdict_data` and `persist_evaluation`; `engine/llm.py` `LLMClient.chat`.
- **Consumed by:** GR-211 persistent finding state, which receives only reflection-kept findings; verdict consumers reading `review.reflection`; human review interfaces reading score reasons and `score_unassigned`.
- **Unaffected:** `engine/pipeline.py` stage execution and `engine/commit_audit.py` CVE-style `ReviewIssue` scoring.

R39. Hilo edges MUST be `SecondPassReviewer -> ReflectionScorer -> JudgeResult.second_pass_review -> build_verdict_data -> persist_evaluation`, with `ReviewStateStore` downstream of the reflection-kept findings; no edge may route reflection scoring through `CommitAuditor`.

`check:` inspect imports/call sites and assert the edge chain and absence of a `CommitAuditor` import in `engine/review_profiles.py`.
