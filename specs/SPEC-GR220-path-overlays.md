# SPEC-GR220 — Path Overlays and Path Selection

## 1. Purpose

Define deterministic glob matching for changed files and path overlays that independently append review instructions, alter profile/tools, add gates, and add labels without conflating review inclusion with overlay behavior.

**Normative requirements**

- **R1** Matching MUST run against normalized repository-relative changed paths, not absolute filesystem paths. `test: test_matcher_normalizes_and_rejects_unsafe_paths`.
- **R2** `paths.include` and `paths.exclude` are selection filters; overlays are policy mutations and MUST remain separate data structures. `test: test_exclusion_does_not_drop_overlay_configuration`.
- **R3** An overlay matches when its `match.any` glob matches at least one eligible changed file. `test: test_overlay_any_match`.
- **R4** A matched overlay applies `instructions.append`, `review.profile`, `review.tools_add`, `gates`, and `labels` independently. `test: test_overlay_operations_are_independent`.
- **R5** Overlay processing MUST be deterministic and MUST report matched overlay IDs and globs. `test: test_overlay_trace_order`.

## 2. Interface

This module is `engine/review_paths.py` and uses the contract value types from SPEC-GR219.

```python
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Literal

@dataclass(frozen=True)
class GlobPattern:
    source: str
    normalized: str

    @classmethod
    def compile(cls, pattern: str) -> "GlobPattern": ...
    def matches(self, path: str) -> bool: ...

@dataclass(frozen=True)
class PathMatcher:
    patterns: tuple[GlobPattern, ...]

    @classmethod
    def from_patterns(cls, patterns: Sequence[str]) -> "PathMatcher": ...
    def match(self, path: str) -> bool: ...
    def matched_patterns(self, paths: Iterable[str]) -> tuple[str, ...]: ...

@dataclass(frozen=True)
class PathOverlayConfig:
    id: str
    match_any: tuple[GlobPattern, ...]
    instructions_append: tuple[str, ...] = ()
    profile: str | None = None
    tools_add: tuple[str, ...] = ()
    gates: Mapping[str, object] = dataclasses.field(default_factory=dict)
    labels_add: tuple[str, ...] = ()
    action: Literal["overlay", "exclude_from_review"] = "overlay"
    reason: str = ""

@dataclass(frozen=True)
class OverlayMatch:
    overlay_id: str
    matched_files: tuple[str, ...]
    matched_globs: tuple[str, ...]
    state: Literal["configured", "candidate_match", "eligible_match", "suppressed"]
    excluded: bool

@dataclass(frozen=True)
class PathSelection:
    include: PathMatcher
    exclude: PathMatcher

    def eligible(self, changed_files: Sequence[str]) -> tuple[str, ...]: ...

@dataclass(frozen=True)
class OverlayResolution:
    eligible_files: tuple[str, ...]
    matches: tuple[OverlayMatch, ...]
    instructions: tuple[str, ...]
    profile: str | None
    tools: tuple[str, ...]
    gates: Mapping[str, object]
    labels_add: tuple[str, ...]
    diagnostics: tuple["ReviewDiagnostic", ...]

def resolve_path_overlays(
    selection: PathSelection,
    overlays: Sequence[PathOverlayConfig],
    changed_files: Sequence[str],
    *,
    base_profile: str | None = None,
    base_tools: Sequence[str] = (),
    base_gates: Mapping[str, object] | None = None,
) -> OverlayResolution: ...
```

`PathMatcher` MUST use one glob implementation for all fields. The implementation MUST support `*`, `?`, `**`, and character classes `[abc]`; matching is case-sensitive, slash-separated, and rooted at the repository path. A pattern not beginning with `/` is repository-relative. A leading `/` is normalized away only once; `..`, empty NUL-containing patterns, and malformed character classes raise `REVIEW_GLOB_PARSE_FAILURE`.

`PathSelection.eligible` applies include then exclude. If include is omitted, it is `("**",)`; if exclude is omitted, it is empty. Overlay resolution uses a second exclusion phase: first select candidates with include/exclude, then apply matching `exclude_from_review` overlays and remove those files from `eligible_files`. An excluded file is not reviewed and cannot trigger a pass by path, but its path remains available to deterministic diff-signal detection only when a pass is triggered by another eligible file and the pass's scope explicitly includes it.

## 3. Data Model

The normative path portion is:

```yaml
paths:
  include: ["src/**", "services/**", "migrations/**"]
  exclude: ["**/fixtures/**", "**/snapshots/**"]
  overlays:
    - id: api-boundary
      match: {any: ["api/**", "routes/**", "**/*controller*"]}
      instructions:
        append: |
          Treat this as an API boundary. Check authentication, authorization,
          input validation, output filtering, backward compatibility, and
          error disclosure. Require concrete evidence for every blocking finding.
      review: {profile: strict, tools_add: [api_contracts]}
      gates: {minimum_severity: high, minimum_confidence: 0.90}
      labels: {add: [gitreins:scope/api]}
    - id: tests
      match: {any: ["**/*_test.go", "**/*.spec.ts", "**/*.test.ts"]}
      instructions:
        append: |
          Check edge cases, negative paths, determinism, isolation, and whether
          the test exercises production wiring rather than a mock-only path.
      gates:
        require: [test_coverage_delta, changed_behavior_has_test]
    - id: generated
      match: {any: ["**/generated/**", "**/*.gen.*"]}
      action: exclude_from_review
      reason: "generated source"
```

Overlay fields map exactly as follows:

| YAML field | Type | Effect |
|---|---|---|
| `id` | non-empty string | Unique trace key; duplicate IDs deny the contract. |
| `match.any` | non-empty list of glob strings | OR match over eligible changed files. |
| `instructions.append` | string or list of strings | Converted to ordered instruction fragments; blank fragments are invalid. |
| `review.profile` | string or absent | Scalar override in overlay order. |
| `review.tools_add` | list of strings | Ordered set union with effective tools. |
| `gates` | mapping | Deep-merged through the contract's gate merge rules. |
| `labels.add` | list of strings | Ordered set union; output lifecycle is handled by GR222. |
| `action` | `overlay` or `exclude_from_review` | Exclusion affects selection, not the existence of this overlay match record; the match record state remains explainable. |
| `reason` | string | Required for `exclude_from_review`; included in diagnostics. |

The output is an ordered trace. For each match, `matched_files` is sorted lexicographically, and `matched_globs` follows the overlay's declared pattern order. Overlay application order is declaration order after the layer merge; a later scalar profile replaces an earlier profile, while tools and labels union.

## 4. Wiring

1. SPEC-GR219's resolver constructs `PathSelection` from `paths.include` and `paths.exclude`, then calls `resolve_path_overlays` with the changed-file set.
2. SPEC-GR221 pass selection receives `eligible_files`, overlay matches, and the overlay-mutated pass/profile/tool context. A path overlay does not directly run a pass.
3. SPEC-GR222 receives overlay gates as one source in the composable gate list; a path gate is evaluated against findings from the pass(es) that cover the matched file.
4. The pre-commit hook supplies staged paths. `gitreins review explain` supplies `--base`/`--head` changed paths. Both call the same resolver.
5. The result is attached to `ResolvedReviewPolicy` and persisted in the verdict trace when a finding is emitted.

## 5. Error Catalog

| Code | Condition | Behavior |
|---|---|---|
| `REVIEW_GLOB_PARSE_FAILURE` | Invalid glob syntax | Deny-and-report with field path and source pattern. |
| `REVIEW_PATH_UNSAFE` | Absolute path, `..`, NUL, backslash, or non-normalized changed path | Deny-and-report; do not partially match. |
| `REVIEW_OVERLAY_ID_EMPTY` | Empty/whitespace overlay ID | Deny-and-report. |
| `REVIEW_OVERLAY_ID_DUPLICATE` | Same ID appears twice in the resolved list | Deny-and-report; no last-writer behavior. |
| `REVIEW_OVERLAY_MATCH_EMPTY` | `match.any` omitted or empty | Deny-and-report. |
| `REVIEW_OVERLAY_REASON_MISSING` | Exclusion action has no non-empty reason | Deny-and-report. |
| `REVIEW_OVERLAY_UNKNOWN_ACTION` | Any action other than the two v1 values | Deny-and-report. |
| `REVIEW_PROFILE_UNKNOWN` | Overlay selects a profile absent from resolved profiles or built-ins | Deny-and-report; do not fall back to standard. |
| `REVIEW_OVERLAY_CONFLICT` | Incompatible overlay gate shapes | Deny-and-report with both overlay IDs and field. |

A changed set with no eligible files produces an empty review scope and a successful resolution with `no_eligible_files=True`; it is not a contract parse error. A malformed overlay is different and denies the run.

## 6. Edge Cases

- `**` matches zero or more path components; `src/**` matches `src/a.py` and `src/pkg/a.py`, but not `services/a.py`.
- `**/*controller*` matches any filename containing `controller`, including a nested filename; matching never inspects file contents.
- Base include/exclude selects candidates before overlay matching. `exclude_from_review` is a second exclusion phase: it removes matching files from `eligible_files` after candidate selection and contributes an explicit exclusion match and reason to the trace.
- If one file matches several overlays, every overlay applies. Instructions append in overlay order; tools and labels de-duplicate preserving first occurrence; scalar profile is last matching overlay's value.
- Overlay and file state is exactly `configured`, `candidate_match`, `eligible_match`, or `suppressed`; explain and persistence use these names. An overlay matching only pre-excluded files is `candidate_match`/`suppressed` as applicable and cannot mutate policy.
- A 1,000+ file diff uses compiled patterns and a single file traversal; matching output is bounded by the number of input files and configured patterns, not repeated glob compilation.
- Unicode paths are matched as Unicode POSIX strings after NFC normalization. A normalization collision is a denial, because two source paths would otherwise share one trace key.
- `paths.include` intersected across layers can become empty. That is valid and yields no eligible files; it is not silently widened to `**`.

## 7. Testing

1. Match `api/auth/login.py` against the normative API overlay and assert the exact overlay ID, file, glob, instruction, profile `strict`, tool `api_contracts`, gate values, and label.
2. Verify OR semantics: one matching glob is sufficient; zero matching globs does not match.
3. Verify `include` then `exclude` with source, fixture, snapshot, and generated paths.
4. Verify a generated exclusion leaves the overlay configuration present in explain output but removes its file from `eligible_files`.
5. Verify two overlays on one file: instruction append order, profile last-wins, tool/label first-seen union, and deep-merged gates.
6. Verify an overlay that matches only an excluded path does not trigger a pass or alter the effective profile.
7. Verify invalid glob patterns (`[`, `**[`, NUL, `../x`, absolute `/tmp/x`) return exact error codes.
8. Verify duplicate IDs, missing exclusion reason, empty `match.any`, and unknown profile all deny.
9. Verify path normalization rejects `a//b`, `./a`, `a/../b`, backslashes, and non-string paths before matching.
10. Verify 1,200 changed files produce stable ordered matches and no duplicate trace entries.
11. Verify the matcher is pure by calling it twice with the same tuples and asserting byte-identical serialized output.
12. Run `pytest` for matcher/overlay tests and `ruff check` on the implementation.

## 8. Hilo Impact

This spec depends on SPEC-GR219's typed path and diagnostic objects and feeds SPEC-GR221's pass trigger scope, SPEC-GR222's path-rule gates, and SPEC-GR223's matched-glob explain trace. It has no direct dependency on `engine/review_profiles.py` beyond profile-name validation; profile resolution remains a scalar policy operation, not a matcher concern. Hilo should connect glob compilation to all review entry points so an unused alternate matcher is detectable.

## PANEL VERDICTS (binding amendments)

These decisions override conflicting text above and are the contract for GR-220.

1. **Overlay state machine (D5).** Every overlay/file record uses exactly one state: `configured`, `candidate_match`, `eligible_match`, or `suppressed`. Explain output and persisted traces MUST use these exact names; `matched` is not a fourth synonym.
2. **Two-phase exclusion (D5).** Base include/exclude selects candidates first. `exclude_from_review` is a second exclusion phase after selection and MUST remove matching files from `eligible_files`; it does not merely annotate them. Correct any body or test that says otherwise.
3. **Gate precedence (D2).** Overlay/repository gates are below pass-level gates and locked organization gates, and above defaults. They may add restrictions only; they may not lower thresholds or weaken actions. `deny_and_report` always wins.
4. **Versioned envelope (D1).** Contract-era persisted records carry `contract_schema_version`, `verdict_schema_version: 2`, and `writer_version`; legacy records are readable but never equivalent.
5. **Write-safety (panel A).** Resolve overlays from one immutable policy/diff snapshot, reject normalization collisions before matching, and serialize persistence/reconciliation under a store-wide lock or atomic transaction. A dry-run or match result is not a successful verdict until its evidence write is complete.
6. **Wall-clock discipline (D9/panel C).** Tests assert exact eligible/match sets, state transitions, and bounded record counts. Use an injectable clock/fake scheduler for timeout paths; do not assert tight elapsed durations.
