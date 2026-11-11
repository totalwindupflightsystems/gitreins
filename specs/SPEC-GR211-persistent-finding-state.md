# SPEC-GR211 — Persistent Finding State

## 1. Purpose
Persist second-pass review findings across runs so unchanged findings retain identity and human dispositions without allowing paraphrase, stale state, or carried findings to bypass the review cap.

This specification is the design authority for the state store used by `engine/review_profiles.py` `SecondPassFinding` values. It does not alter verdict-history storage under `.gitreins/history/`; its only state file is `.gitreins/review-state.json`.

## 2. Interface

The worker adds `engine/review_state.py` and extends the existing review-profile data shape. The following signatures are exact.

```python
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

ReviewStateStatus = Literal["ACTIVE", "RESOLVED", "DISMISSED"]
HumanDecision = Literal["dismiss"]

@dataclass(frozen=True)
class HumanReviewDecision:
    fingerprint: str
    decision: HumanDecision
    reviewer: str
    reason: str = ""

@dataclass(frozen=True)
class FindingStateRecord:
    fingerprint: str
    status: ReviewStateStatus
    file: str
    header: str
    body: str
    echo: str
    severity: str
    category: str
    first_seen_run: int
    last_seen_run: int
    dismissed_by: str | None = None
    dismissed_reason: str | None = None

@dataclass(frozen=True)
class ReviewState:
    version: int
    run: int
    findings: tuple[FindingStateRecord, ...]

@dataclass(frozen=True)
class FindingStateResult:
    state: ReviewState
    emitted: tuple[dict[str, Any], ...]
    carried: tuple[str, ...]
    state_error: str | None = None

class ReviewStateError(RuntimeError):
    """A review-state read, validation, lock, or atomic-write failure."""


def normalize_header(header: str) -> str: ...

def finding_fingerprint(file: str, header: str, body: str) -> str: ...

def finding_parts(finding: Mapping[str, Any]) -> tuple[str, str, str]: ...

def finding_echo(finding: Mapping[str, Any]) -> str: ...

def review_state_path(workdir: str) -> str: ...

class ReviewStateStore:
    def __init__(self, workdir: str = ".") -> None: ...
    def load(self) -> ReviewState: ...
    def reconcile_and_save(
        self,
        findings: Sequence[Mapping[str, Any]],
        human_decisions: Sequence[HumanReviewDecision] = (),
        max_findings: int = 10,
    ) -> FindingStateResult: ...


def reconcile_review_findings(
    workdir: str,
    findings: Sequence[Mapping[str, Any]],
    *,
    human_decisions: Sequence[HumanReviewDecision] = (),
    max_findings: int = 10,
) -> FindingStateResult: ...
```

The existing `engine.review_profiles.py` shape is extended exactly as follows; all existing fields retain their meaning and order, and `category` is appended with a default so existing positional construction remains valid.

```python
@dataclass(frozen=True)
class SecondPassFinding:
    severity: str
    file: str
    line: int
    detection: str
    detail: str = ""
    category: str = "unknown"
```

`SecondPassFinding.to_dict()` emits `severity`, `file`, `line`, `detection`, `detail`, and `category`. `second_pass_finding_from_dict(data: Any) -> tuple[SecondPassFinding | None, str | None]` accepts a missing `category` as `"unknown"` for old review records.

### 2.1 Fingerprint algorithm

R1. `finding_fingerprint` MUST return the lowercase hexadecimal SHA-256 digest of the UTF-8 bytes of `file + normalize_header(header) + body`, with no implicit separator, JSON encoding, or line number.

`test:` call `finding_fingerprint("engine/a.py", " High   Error ", "body")` and compare it with `hashlib.sha256("engine/a.pyhigh errorbody".encode("utf-8")).hexdigest()`.

R2. `normalize_header` MUST strip leading/trailing whitespace, collapse every run of Unicode whitespace to one ASCII space, and apply `casefold()`.

`test:` assert `normalize_header("  HIGH\tError  ") == "high error"`.

R3. `finding_parts` MUST derive `file` from the repo-relative `file`, `header` as `severity + "|" + category + "|" + detection`, and `body` as `detail` without paraphrasing or whitespace normalization.

`test:` pass `{"file":"a.py","severity":"HIGH","category":"verify-only","detection":"rule","detail":"Exact detail"}` and assert the returned tuple is `("a.py", "HIGH|verify-only|rule", "Exact detail")`.

R4. The fingerprint MUST omit `line`, so a finding whose file, normalized header, and body are unchanged retains its identity after a line move.

`test:` compute two fingerprints with identical file/header/body and different source line values supplied outside the function and assert equality.

R5. A paraphrased body MUST produce a different fingerprint even when file, category, detection, and severity are unchanged.

`test:` the falsifier passes `"returns None on failure"` and `"returns no value when the operation fails"` as bodies and asserts the two digests differ; an implementation hashing only file/header fails this test.

### 2.2 State operations

R6. `ReviewStateStore` MUST use `os.path.join(workdir, ".gitreins", "review-state.json")` as its only state path.

`check:` inspect the implementation and assert no path under `.gitreins/history/`, `refs/`, or the repository root is used for finding state.

R7. A missing state file MUST load as `ReviewState(version=1, run=0, findings=())` without raising.

`test:` construct a store in a directory containing `.gitreins/` but no state file and assert the exact empty state.

R8. `reconcile_and_save` MUST increment `run` by one, apply human decisions, reconcile current findings, and atomically save one version-1 state document while holding an exclusive lock for the entire read/reconcile/write transaction.

`test:` run two concurrent reconciliations against one fixture and assert the final JSON has one valid document and a run number equal to the number of successful transactions, with no lost decision or partial JSON.

R9. A newly observed fingerprint MUST receive `ACTIVE`, `first_seen_run == last_seen_run == state.run`, and an `echo` equal to the exact current finding echo.

`test:` reconcile one new finding and assert its record has those values and appears in `emitted`.

R10. An existing `ACTIVE` fingerprint present in the current findings MUST remain `ACTIVE`, update `last_seen_run`, and replace its stored `echo` with the current exact echo.

`test:` reconcile the same fingerprint with changed detail and assert the record is active and its echo equals the new detail exactly.

R11. An existing `ACTIVE` fingerprint absent from the current findings MUST become `RESOLVED` and MUST NOT be emitted or carried.

`test:` reconcile a prior active record with an empty current list and assert status `RESOLVED` and an empty `emitted` list.

R12. An existing `RESOLVED` fingerprint that reappears MUST become `ACTIVE`, preserve `first_seen_run`, update `last_seen_run`, and use the new echo.

`test:` reconcile active → absent → present and assert the final record is active with its original first-seen run.

R13. A `dismiss` human decision MUST set the matching fingerprint to `DISMISSED`, record `reviewer` and `reason`, and prevent the finding from being emitted.

`test:` reconcile a generated finding with `HumanReviewDecision(fp, "dismiss", "alice", "accepted risk")` and assert status, dismissal metadata, and zero emission.

R14. A `DISMISSED` fingerprint MUST remain `DISMISSED` when the generator emits it again, unless a separate explicit state-reset API exists; this specification defines no reset API.

`test:` reconcile a dismissed fingerprint with a later generated copy and assert it remains dismissed and absent from `emitted`.

R15. A human decision for an unknown fingerprint MUST return `state_error == "unknown fingerprint: <fingerprint>"` and MUST NOT create a synthetic finding record.

`test:` submit a decision for `"0" * 64` to an empty store and assert the exact error and unchanged findings tuple.

R16. `finding_echo` MUST serialize the finding as the exact displayed finding text, including original header/body text; state reconciliation MUST never regenerate an active carried echo from a score, summary, or paraphrase.

`test:` store an active finding, change only the generator's unrelated metadata, omit it from the next run, and assert the carried emitted object contains the byte-identical stored `echo`.

R17. Carried findings MUST be active records absent from the current generated set; resolved and dismissed records MUST never be counted as carried.

`test:` reconcile a state containing one record of each status with an empty generated set and assert `carried` contains only the active fingerprint.

R18. `max_findings` MUST be a positive integer and MUST cap the emitted union of current active findings and carried active findings after deduplication; every carried finding consumes one slot.

`test:` with `max_findings=2`, one new finding, and two carried active findings, assert `len(emitted) == 2` and that the deterministic selection order is current findings first, then carried findings by fingerprint.

R19. State records omitted from emission because the cap is full MUST remain in the state file with their reconciled status.

`test:` repeat R18 and assert all three active records remain in `state.findings` even though only two are emitted.

R20. `reconcile_review_findings` MUST pass mappings produced by `SecondPassFinding.to_dict()` unchanged into the store and MUST return the same `FindingStateResult` contract as `ReviewStateStore.reconcile_and_save`.

`test:` monkeypatch `ReviewStateStore.reconcile_and_save`, call the helper, and assert the received mappings, decisions, and cap are exact.

## 3. Data Model

### 3.1 `review-state.json`

The complete file is one UTF-8 JSON object. No JSONL append format is permitted.

```json
{
  "version": 1,
  "run": 17,
  "findings": [
    {
      "fingerprint": "64 lowercase hex characters",
      "status": "ACTIVE",
      "file": "engine/example.py",
      "header": "HIGH|error-handling|missing timeout check",
      "body": "The exact generator detail text.",
      "echo": "[HIGH][error-handling] engine/example.py:42 — missing timeout check\nThe exact generator detail text.",
      "severity": "high",
      "category": "error-handling",
      "first_seen_run": 12,
      "last_seen_run": 17,
      "dismissed_by": null,
      "dismissed_reason": null
    }
  ]
}
```

R21. The persisted top-level keys MUST be exactly `version`, `run`, and `findings`; `version` MUST equal `1`, `run` MUST be a non-negative integer, and `findings` MUST be a list.

`test:` load a saved file with `json.load` and assert the top-level key set and primitive types exactly.

R22. Every finding record MUST contain exactly the keys shown in the schema, with `status` restricted to `ACTIVE`, `RESOLVED`, or `DISMISSED`, a 64-character lowercase hexadecimal fingerprint, and non-negative integer run fields.

`test:` feed malformed records for each invalid status, digest length, and negative run into the validator and assert `ReviewStateError` names the first invalid field.

R23. `fingerprint` MUST equal the digest recomputed from the record's `file`, `header`, and `body` using the algorithm in §2.1; the stored header is the pre-normalized header used for the human echo, not a second identity source.

`test:` mutate `body` in a saved record without mutating `fingerprint`, load the file, and assert validation rejects it.

R24. `dismissed_by` and `dismissed_reason` MUST be non-null only for `DISMISSED` records; active and resolved records MUST serialize both as JSON null.

`test:` validate one record of each status and assert the null/non-null rule.

R25. The verdict review block MUST add a `review_state` object with `emitted`, `carried`, and optional `state_error`; it MUST NOT copy the entire state file into the verdict.

`check:` inspect `build_verdict_data` output and assert `review_state` contains only the bounded emission receipt, not all historical records.

## 4. Wiring

1. `engine/review_profiles.py` remains the source of `SecondPassFinding` validation and serialization. The parser accepts the new `category` field and defaults old records to `unknown`.
2. `Judge._run_second_pass(result: JudgeResult, skip_tier2: bool = False) -> None` keeps its existing signature. After `SecondPassReviewer.review()` and before assigning `result.second_pass_review`, it calls `reconcile_review_findings(self.workdir, [f.to_dict() for f in review.findings], human_decisions=..., max_findings=max_findings)` and replaces `review.findings` with the emitted findings.
3. Human decisions arrive through the judge invocation's review input as `review.human_decisions`, each validated into `HumanReviewDecision`; absent input means `()`.
4. `engine/persist.py` `build_verdict_data(workdir: str, task: Task, result: JudgeResult) -> dict[Any, Any]` copies the bounded `review_state` receipt from `result.second_pass_review` into the persisted `review` block. `persist_evaluation(...)` remains the single verdict writer.
5. The state store writes before `VerdictPersister.persist(...)`; state-write failure does not turn a completed review into a passed review and is exposed as `state_error` in the review receipt.

R26. The judge MUST reconcile state after reflection filtering and before verdict serialization, so only findings that survived GR-212 are fingerprinted, carried, capped, and persisted as emitted findings.

`test:` provide one generator finding that reflection drops and assert it appears in neither `review.findings` nor the state receipt.

R27. The pipeline's existing `StepResult` and `Pipeline.run(task: dict[str, Any], trigger: str = "pre-eval") -> dict[str, Any]` contracts MUST remain unchanged by GR-211.

`check:` run the existing pipeline tests and assert no `StepResult` key or `Pipeline.run` parameter changes.

## 5. Error Catalog

| Error condition | Internal handling | Caller-visible result |
|---|---|---|
| Missing state file | Treat as empty version-1 state | No error; `state_error == null` |
| Invalid JSON | Preserve the corrupt file, skip write, continue with empty in-memory state | `state_error == "corrupt review state: <reason>"` |
| Schema mismatch | Preserve the file, skip write, continue with empty in-memory state | `state_error == "invalid review state: <field>"` |
| Invalid generated finding | Drop only that finding | Existing parser drop reason; state receipt remains valid |
| Unknown human fingerprint | Do not create a record | `state_error == "unknown fingerprint: <fingerprint>"` |
| Non-positive/non-integer cap | Reject the reconciliation call | `ReviewStateError("max_findings must be a positive integer")`; judge records `state_error` |
| Lock acquisition failure | Do not write state | `state_error == "review state lock failed: <reason>"` |
| Atomic replace failure | Leave the prior state file untouched | `state_error == "review state write failed: <reason>"` |
| Missing `.gitreins` directory | Create it with mode inherited from the process umask | No error |

R28. No state error MUST raise through `Judge.evaluate_task` or `persist_evaluation`; the review result MUST remain inspectable and the error MUST be present in its review-state receipt.

`test:` make the state path unwritable, run a judge with one finding, and assert the judge returns a result with `review_state.state_error` rather than raising.

R29. A corrupt state file MUST never be overwritten during the same reconciliation attempt.

`test:` write invalid JSON, run reconciliation, and assert the original file bytes are unchanged.

## 6. Edge Cases

R30. An empty generated finding list MUST still resolve absent active findings, preserve dismissed records, and write the incremented run state.

`test:` reconcile an empty list against active, resolved, and dismissed fixtures and assert only active records transition to resolved.

R31. Duplicate generated findings with the same fingerprint MUST produce one state record and one emitted finding, retaining the first occurrence's ordering and exact echo.

`test:` submit two identical mappings and assert one record and one emission.

R32. A generator finding with the same fingerprint but changed severity or category MUST be treated according to the fingerprint contract: if those header components change, it is a new fingerprint and the prior record is resolved when absent.

`test:` change only `category` and assert the old and new fingerprints differ and the state contains the old resolved plus new active records.

R33. A finding whose body contains newlines, Unicode, or leading spaces MUST preserve those bytes in `body` and `echo` while hashing UTF-8 bytes exactly.

`test:` round-trip a Unicode multiline body and assert byte equality after JSON load and digest recomputation.

R34. A state file larger than 10,000 records MUST be loaded and reconciled without an output-size expansion beyond the bounded `emitted` list; the full state remains available for audit.

`check:` use a 10,001-record fixture and assert `len(result.emitted) <= max_findings` and no history verdict contains the full state list.

R35. Concurrent runs MUST serialize through the state lock and MUST never use last-writer-wins replacement of a decision already committed by a completed transaction.

`test:` race one generated reconciliation against one dismissal transaction and assert the final status reflects the transaction that acquired the lock second while preserving both run increments.

## 7. Testing

The implementation test module is `tests/test_gr211_persistent_finding_state.py`. Each test name below is normative.

| Test | Expected outcome |
|---|---|
| `test_fingerprint_exact_sha256_payload` | R1 exact digest passes |
| `test_header_normalization` | R2 normalization passes |
| `test_line_move_does_not_change_fingerprint` | R4 passes |
| `test_paraphrase_churn_falsifier` | R5 detects body paraphrase churn |
| `test_missing_state_is_empty` | R7 passes |
| `test_atomic_concurrent_reconciliation` | R8 and R35 pass |
| `test_new_finding_is_active` | R9 passes |
| `test_absent_active_becomes_resolved` | R11 passes |
| `test_dismissal_is_human_only` | R13 and R14 pass |
| `test_unknown_dismissal_is_rejected` | R15 passes |
| `test_active_carried_echo_is_verbatim` | R16 passes |
| `test_carried_findings_consume_cap` | R18 and R19 pass |
| `test_corrupt_state_is_preserved` | R29 passes |
| `test_duplicate_fingerprints_deduplicate` | R31 passes |
| `test_large_state_has_bounded_emission` | R34 passes |

R36. The test suite MUST include a negative control that removes `body` from the digest payload and asserts `test_paraphrase_churn_falsifier` fails.

`test:` run the negative-control helper against the intentionally broken digest function and assert a failing assertion is raised.

R37. The state tests MUST exercise the real `ReviewStateStore` path and real JSON file, using an LLM stub only at the existing `SecondPassReviewer` seam.

`check:` reject tests that replace `ReviewStateStore` with a mock for all assertions or seed `review-state.json` without invoking reconciliation.

## 8. Hilo Impact

- **Depends on:** `engine/review_profiles.py` for `SecondPassFinding`, `SecondPassReviewer`, severity validation, and the existing review block; `engine/judge.py` for second-pass orchestration; `engine/persist.py` for verdict payload assembly; Python `hashlib`, `json`, `os`, and `fcntl` for identity, serialization, path creation, and locking.
- **Consumed by:** future human-review UI/CLI surfaces through `HumanReviewDecision`; persisted verdict consumers through the bounded `review_state` receipt; report consumers that already read `review`.
- **Must not change:** `VerdictPersister`, `.gitreins/history/`, pipeline stage schemas, or the existing `SecondPassReviewer` independent-context contract.

R38. Hilo dependency edges MUST name `engine/review_state.py -> engine/review_profiles.py`, `engine/judge.py -> engine/review_state.py`, and `engine/persist.py -> JudgeResult.second_pass_review`; no dependency may point from review state into verdict-history persistence.

`check:` inspect imports and assert the three required edges and the absence of a `VerdictPersister` import in `engine/review_state.py`.

## PANEL VERDICTS (binding amendments)

The amendments below are binding. Where an amendment differs from the body above, this section controls.

1. **Quoted challenged requirement:** “`finding_fingerprint` MUST return the lowercase hexadecimal SHA-256 digest of the UTF-8 bytes of `file + normalize_header(header) + body`” (R1), and “`finding_parts` MUST derive ... `body` as `detail`” (R3).
   **Decision:** The fingerprint hashes the canonical identity body `severity|category|detection`, with `category` normalized exactly as defined by GR-212. `line` and display `detail` are not identity inputs. The `echo` carries the original display header and detail; a detail-only change updates the echo without changing the fingerprint. This resolves Seat B’s identity/display and schema-collision finding and Owner Decision D4.

2. **Quoted challenged requirement:** “`reconcile_and_save` MUST ... atomically save one version-1 state document while holding an exclusive lock for the entire read/reconcile/write transaction” (R8), and “Concurrent runs MUST serialize through the state lock” (R35).
   **Decision:** Every transaction uses `.gitreins/review-state.lock` and a POSIX `flock` exclusive lock, held across read, reconciliation, and atomic replacement. Acquisition times out after 30 seconds; timeout and stale-lock diagnostics identify the lock path, PID, process start time when available, and holder metadata. Separate processes must exercise the timeout and serialization paths. This resolves Seat A’s lock-operational-definition finding and Owner Decision D2.

3. **Quoted challenged requirement:** “Invalid JSON ... Preserve the corrupt file, skip write, continue with empty in-memory state” and “Schema mismatch ... Preserve the file, skip write, continue with empty in-memory state” (the §5 error catalog).
   **Decision:** Missing and corrupt state are distinct. MISSING means no state file exists: load empty state and proceed. CORRUPT means invalid JSON or schema: quarantine the original bytes to `.gitreins/review-state.corrupt-<ts>`, write a recovery-required marker, expose the corruption error, and do not silently proceed or reconcile as empty. This resolves Seat A’s corruption-recovery finding and Owner Decision D3.

4. **Quoted challenged requirement:** “The persisted top-level keys MUST be exactly `version`, `run`, and `findings`; `version` MUST equal `1`” (R21), together with the requirement that the bounded `review_state` receipt be copied into the verdict (R25).
   **Decision:** `review-state.json` retains its integer `version` field, but every persisted `verdict.json` from this suite forward MUST independently contain integer `schema_version: 2` and `writer_version: gitreins.__version__`. The review-state `version` does not provide verdict provenance and MUST NOT be used as its substitute. This resolves Seat B’s verdict-schema/provenance finding and Owner Decision D1.

5. **Quoted challenged requirement:** “The verdict review block MUST add a `review_state` object with `emitted`, `carried`, and optional `state_error`; it MUST NOT copy the entire state file into the verdict” (R25).
   **Decision:** The persisted review-state receipt is serialized through a dedicated bounded serializer and contains only `emitted`, `carried`, and `state_error` (with the documented omission of a null optional error); it never embeds historical records, lock metadata, quarantine bytes, or the full state document. This resolves Seat B’s persistence-compatibility finding.

6. **Quoted challenged requirement:** “The judge MUST reconcile state after reflection filtering” (R26), and “An existing `ACTIVE` fingerprint absent from the current findings MUST become `RESOLVED`” (R11).
   **Decision:** Reconciliation may resolve active findings only after a completed reflection/second pass supplies the authoritative candidate set. A failed, unavailable, absent, or `score_unassigned` reflection result MUST leave active findings unresolved for reconciliation purposes, while persisting the failure receipt; it MUST NOT resolve them merely because the candidate list is empty. This resolves Seat A’s durable-failure-marker finding and Owner Decision D6.

7. **Quoted challenged requirement:** “Human decisions arrive through the judge invocation's review input as `review.human_decisions`” (wiring item 3), while the Hilo section says human decisions are consumed by “future human-review UI/CLI surfaces.”
   **Decision:** Human dismissal is not a future-only contract. The implementation MUST provide a concrete `gitreins review dismiss <fingerprint>` operation with reviewer and reason fields, validate it into `HumanReviewDecision`, and route it through the same locked state transaction; equivalent MCP exposure is optional but, if present, MUST use the same operation. This resolves Seat C’s no-live-consumer finding.

8. **Quoted challenged requirement:** “The state tests MUST exercise the real `ReviewStateStore` path and real JSON file” (R37), and “Concurrent runs MUST serialize through the state lock” (R35).
   **Decision:** Acceptance tests MUST invoke the concrete dismissal producer and the real store in separate processes, verify byte-level quarantine/recovery behavior, and assert that a completed second pass—not a mocked or absent result—drives resolution. Tests MUST assert outcomes and persisted markers rather than relying on host-load-sensitive wall-clock timing. This resolves Seat A and Seat C’s consumer-realism/test-harness findings.
