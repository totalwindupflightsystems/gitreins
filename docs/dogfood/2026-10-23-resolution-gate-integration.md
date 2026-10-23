# GitReins resolution gate + push-check — real-use integration report (2026-10-23)

Run: dogfood tick `gitreins-dogfood-2026-10-23-10-02-55`, target gitreins
(`/home/kara/gitreins`, HEAD 156c36c, v0.17.0). Angle: fix-verification of the
closed 0.15-era resolution-gate rows (DF-GITREINS-POC-35/36/37) in real use, plus
the one surface no prior run touched — `gitreins push-check` (§18, zero mentions
in runs 1–23). Read-only lane: no code changed, rows appended to the board, docs
and this report left behind.

## The promise under test

> Enable one resolution-gate surface by typing `true`, ask the repo a real
> question, and get a persisted calibrated verdict banded in code. Configure a
> pre-push hook, and a push that would publish a credential is refused.

## Setup (as a user, per the docs)

Scratch consumer repo `/home/kara/scratch/gitreins-df-20261023/consumer`
(real app: a log summarizer with a test), gitreins from the repo venv 0.17.0,
hilo 0.4.0 on PATH, OpenRouter key from `~/.hermes/.env` per §15.

```
gitreins install    # pre-commit + pre-push hooks, pinned binary (DF-011)
gitreins init       # writes the complete resolution block, all four surfaces false
# one literal edit in .gitreins/config.yaml: cli: false → cli: true
```

## POC-35/36/37 fix verification — all three FIXED in real use

| Prior row | Prior defect | Today on 0.17.0 |
|---|---|---|
| POC-35 (P1) | surfaces shipped disabled, undocumented; fix hint cited a nonexistent §9 | `init` writes the complete block with all four `false`; enabling `cli` is one literal edit; the disabled ABSTAIN names `docs/jev-resolution-gate.md §9` which EXISTS (verified) — FIXED |
| POC-36 (P1) | resolve/preflight verdicts never persisted | RESOLVED verdict persisted to `.gitreins/history/2026-10-23/f445526b/verdict.json` (kind: resolution) + one usage.jsonl row (941 in / 96 out); `gitreins report` lists it (`─── Resolution gate (1) ───`); ABSTAIN wrote nothing (history dir count unchanged) — FIXED |
| POC-37 (P2) | preflight --json embedded verdict_json as an escaped string | `preflight --json` carries a top-level `verdict` OBJECT — the same dict `resolve --json` prints (verified on the same question) — FIXED |

Working resolve: `gitreins resolve "Does summarize.py handle a log line whose
level is TRACE?"` → RESOLVED (0.97) in 0.6–1.1 s after `hilo init && hilo graph
warm`; REVIEW 0.70 on a question with missing implementation; the discrimination
the 2026-09-23 run measured (real snippet → high, filler → low) still holds at
repo scale (RESOLVED 0.92 on engine/preflight.py's own mapping; UNRESOLVED 0.39
on a Kubernetes question with no such code).

## New findings (E1–E5)

E1 (P2, JEVRES-007): preflight silently demotes a ≥0.85 probability to REVIEW via
the evidence floor and its printed reason misreports the cause. Same question,
seconds apart: resolve → RESOLVED (0.97); preflight → band REVIEW, decision
dispatch-with-note, reason "the evidence partially resolves the row" — on
evidence that was NOT partial (evidence_quality 2 = "the exact code path the
question asks about is present"; the floor requires 3 = "…together with its
test"). Nothing in the consumer output or §16's table names the floor.

E2 (P3, DF-025): a fresh repo's first resolve ABSTAINs `empty-bundle` with a hint
that says "check that hilo is installed" — hilo IS installed; the real fix is
`hilo init && hilo graph warm`. Two trial-and-error rounds; the claimed "exact
path that was searched" appears nowhere in the abstain detail.

E3 (P3, DF-026): push-check publishes a token whose literal is split by
concatenation. Single-commit-range A/B (identical bytes except the split):
`"ghp_" + "<40-char body>"` → PUSH SECRET CHECK: clean, RC=0 — and the push
lands the token on the remote; the joined form → refused. Also seen: the guard's
secrets lane DID catch the concat form (as a sensitive-path/near-miss), so the
gap is push-check's line scanner alone (engine/push_secrets.py).

E4 (P4, DF-027): `guard --full`'s secrets cell reads clean on a tree with a
COMMITTED credential while its console line says "whole tree" — the secrets lane
keeps the staged-diff scope; `--scope working-tree` catches it (3 findings).

E5 (P4, DOC-25): §16's band table has no evidence-floor row, so the E1 demotion
is unexplainable from the doc a CLI user reads.

## The working consumer config (the parts that matter)

```yaml
resolution:
  enabled:
    cli: true            # one literal edit; the other three stay false
  model: typesafe/jev-1.13
  tokens_max: 28000
  bands: {resolved_at: 0.85, review_at: 0.5}
```

## Push-check real use (never exercised before this run)

Clean push through the installed pre-push hook: PASS. A `--no-verify` commit
carrying a fake-formatted key: blocked by the pre-commit hook (secrets FAIL).
With the hook bypassed, the secret commit reached the local remote through the
pre-push hook in its PUBLISHED (concatenated) form — E3. After `git rebase`
removing the secret, push-check ran clean on the rewritten range (correct). The
refusal text names rotation + history rewrite (the right remedy), and the
documented ref-pair semantics hold: swapped positionals → empty range → clean.

## Perf (Step 2b; measured, warm + cold, one measurement per op)

| Operation | Cold | Warm | Verdict |
|---|---|---|---|
| guard (consumer repo) | — | 264.4 ms ± 13.0 (n=15) / 282.6 ± 10.0 (n=10) | fast |
| push-check (clean range) | — | 126.6 ms ± 15.0 (n=15) / 167.5 ± 10.8 (n=20) | fast |
| resolve (consumer, 941-token bundle) | 0.195 s on a cache-free tree (abstains fast) / 0.561 s with cache | 0.57–1.08 s (n=3) | fast |
| resolve (gitreins repo scale, real questions ×5) | — | 2.26–3.23 s | acceptable (network-dominated) |
| preflight (warm, same question) | — | 0.64–0.66 s (n=3) | fast |

Cost per consumer resolve measured in usage.jsonl: 941 in / 96 out tokens,
$0.0000395 — the "~$0.0003 typed decision" promise holds with margin. Cold-graph
degradation is a fast ABSTAIN, not a hang (the UX of that abstain is DF-025).
Nothing slow enough that a user would notice; no profiler run justified; the
numbers are recorded as board row PERF-2.

## Verdict

**SHIPPABLE-leaning.** The exact failures that made 0.15's flagship a
PROMISING-BUT-ROUGH verdict are gone in real use, and the never-tested push
gate honors its contract against the literal form. What remains is one trust
gap (E1 — a demotion whose printed reason is wrong) and two P3 gaps a user
meets in their first hour (E2) or only when concatenating credential strings
(E3). Board: JEVRES-007 (P2), DF-025, DF-026 (P3), DF-027, DOC-25, PERF-2 (P4).

## Install leg (ephemeral bunker)

RUN on bunker-mvp agent df-gitreins-1023 (ttl 2h): fresh Ubuntu 24.04 /
Python 3.12.3. README's literal `pip install gitreins` → PEP 668 refusal
(reproduced, known). Venv route: 0.17.0 installed in ~20 s. install + init +
"Try the hook" commit green (DEGRADED-PASS with the honest gitleaks-missing
warning); pre-push hook ran push-check on a real push. Evidence pulled and
verified; agent destroyed, key removed, `bunker list` shows 0 matching agents.