# Review Profiles, Effort Levels, and Multi-Pass Fix Guidance (GR-148)

Review **profiles** and **effort levels** are separate dimensions:

- A **profile** is a review POLICY: which checks run, the rubric the reviewer
  follows, the severity filter, which passes execute, and what output each
  pass must produce.
- An **effort level** is a BUDGET: max passes, LLM calls, tokens, wall-clock
  time, and tool calls. No profile can run unbounded.

## Built-in profiles

| Profile | Passes | Severity filter | Checks on | Default effort |
|---|---|---|---|---|
| `quick` | findings | critical-only | bugs, security | 1 LLM call, 1024 tokens, 60s, no tools |
| `standard` (default) | findings, estimates | standard | bugs, security, anti_patterns | 2 LLM calls, 2048 tokens, 120s, no tools |
| `deep` | findings, estimates, remediation | all | all five checks | 4 LLM calls, 4096 tokens, 300s, 8 tool calls |

## Pass contracts

- **Pass 1 — findings:** evidence-backed issues with file/line, severity,
  category, concise rationale (description), and `suggestion` (preserved from
  GR-065). Severities: `critical | high | medium | low | trivial | info |
  observation`.
- **Pass 2 — estimates:** per-finding fix estimate on a coarse size scale
  `XS | S | M | L | XL`, a labeled-estimate time range, `confidence`
  (high/medium/low), `assumptions`, `likely_scope`, and `likely_tests`. An
  unknown estimate carries a `reason` string. **All figures are estimates —
  they are never measured durations**, and every rendering says so.
- **Pass 3 — remediation (optional, `deep` only):** an agent-ready brief with
  ordered steps, acceptance checks, and notes. It is a DESCRIPTION only —
  GitReins never modifies code unless an explicit implementation action is
  requested elsewhere.

## Severity compatibility (existing consumers)

Pre-GR-148 consumers know `critical | high | medium | low | info`. The
mapping (nothing maps upward, so no consumer can miss a critical/high/
medium/low finding):

| canonical | legacy |
|---|---|
| critical / high / medium / low | unchanged |
| trivial | info |
| info | info |
| observation | info |

`ReviewIssue.compat_severity()` and the `compat_severity` key in structured
output apply the mapping; the legacy `_run_review` issue strings use it too.

## Effort precedence

```
per-invocation override  >  repo default  >  profile default
(CLI / explicit arg)        (commit_audit.review_effort
                             in .gitreins/config.yaml)     (built-in)
```

Budget keys: `max_passes`, `max_llm_calls`, `max_tokens`, `time_budget_s`,
`max_tool_calls`. A zero cap means the dimension is unused, not exhausted.

When a cap is hit before the profile's passes complete, the result is
returned **partial**: `partial: true` plus a machine-readable
`partial_reason`, with the passes that DID run intact (pass-1 findings are
never dropped to mark a run partial).

## Selection

- **CLI:** `gitreins commit-audit --review-profile deep --review-effort max_llm_calls=4 --review-effort time_budget_s=240 "message"`
- **Repo config** (`.gitreins/config.yaml`):

  ```yaml
  defaults:
    commit_audit:
      review_profile: deep          # quick | standard | deep
      review_effort:                # repo-default budget layer
        max_llm_calls: 6
        time_budget_s: 240
  ```

- **Python:** `CommitAuditor(llm, review_profile="deep",
  review_effort_override={"max_tokens": 8192})` or
  `auditor.review(message, diff, profile="quick",
  effort_override={"max_llm_calls": 1})`.

## Output

`CommitReviewResult` carries `profile`, `effort` (the RESOLVED limits
actually used), `passes_run`, `partial` / `partial_reason`, `estimates`,
and `remediation_brief` alongside all GR-065/GR-066 fields. Structured
output: `CommitReviewResult.to_dict()` (includes `severity_counts` and
per-issue `compat_severity`). Human output:
`CommitReviewResult.render_human()` — severity groups with counts, per-finding
fix-effort estimates, and the remediation brief when present.
