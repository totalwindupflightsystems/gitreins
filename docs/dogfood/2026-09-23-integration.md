# Dogfood integration report — 2026-09-23 (run 7): the v0.15.0 resolution gate

**Surface under test (never touched by runs 1–6):** `gitreins resolve`, `gitreins
preflight`, MCP `context.resolve` — the Jev resolution gate, the flagship of the
v0.15.0 release. Verified running gitreins 0.15.0 (PATH == repo checkout == PyPI).

## Promise under test

*"A foreman can ask a question of the repo (`gitreins resolve`) and get a calibrated
RESOLVED/REVIEW/UNRESOLVED verdict banded in code within a measured token budget, and
can run `gitreins preflight` before dispatching a board row and get a dispatch decision
(skip-dispatch / dispatch-with-note / dispatch)."* — README v0.15.0 block,
docs/cli-reference.md §15–16, docs/jev-resolution-gate.md.

## Verdict: the gate WORKS — on real premises, with real discrimination

A foreman's morning was simulated on REAL repo facts, on a fresh git repo, and against
every documented failure mode:

| # | Probe | Result |
|---|---|---|
| T1 | preflight of a TRUE premise ("os.kill PID validation fix applied in engine/lsp.py?" — AGENTS.md documents it applied) | RESOLVED p=0.85 → **skip-dispatch**, exit 0, 3.0s — the tool stops a wasted worker |
| T2 | preflight of an OPEN premise ("live_surface() stub-restore bug fixed?" — DF-GITREINS-POC-34, pending) | UNRESOLVED p=0.29, missing_kind=implementation → **dispatch**, exit 0 |
| T3 | resolve, sharply answerable question | REVIEW p=0.60, exit 0 |
| T4 | resolve, unanswerable question (nonexistent component) | UNRESOLVED p=0.05, missing_kind=implementation, exit 1 |
| T5d | transport dead (HTTPS_PROXY=127.0.0.1:9) | **ABSTAIN transport-error**, exit 1, actionable fix string — fail-CLOSED proven |
| T5f | preflight under the same failure | **dispatch**, exit 0, abstain_reason carried — fail-OPEN as documented; the asymmetry is real |
| T5a/b/e | no key / garbage key / empty key | still returned verdicts — the documented .env failover (6 candidates on this host) supplies credentials; NOT a bug |
| T6 | MCP `context.resolve` over raw JSON-RPC stdio | 13-tool surface, REVIEW p=0.58 in 2.5s, protocol clean (initialize → notification → tools/list → tools/call) |
| T7 | resolve `--budget 2000` | budget ENFORCED (tokens_estimated 1963 ≤ 2000, 1-file manifest) — and the verdict got BETTER (RESOLVED 0.92): precision-beats-volume, exactly as the spec predicted |
| T8b | resolve in a code-less repo (surfaces on) | ABSTAIN **empty-bundle**, exit 1, named reason + action |

The budget law held under measurement: every verdict carried `clipped: true` +
`chars_dropped` (~52k chars dropped at default budget — truncation DISCLOSED, never
silent), tokens_estimated matched actual input_tokens within ~7% (12,197 actual vs
13,076 estimated on T1 — conservative, as designed). Cost per call measured
$0.00051–0.00057. Jev model build `typesafe/jev-1.13-20260917` echoed in every verdict.

## The one thing a new user MUST know (Finding 1, P1)

**Every resolution surface ships disabled, and no doc says so.** `surface_enabled()`
(engine/resolution.py:238–260) requires an explicit `resolution.enabled.<surface>: true`
in `.gitreins/config.yaml`; absent/empty/wrong-typed config all mean OFF. Concretely:

- `gitreins preflight "<x>"` on this very repo (a valid key in env, hilo installed)
  returns in **0.12s** with `abstain_reason: "surface-disabled"`.
- The error's own fix hint says *"see docs/jev-resolution-gate.md §9"* — **that section
  does not exist** (the doc ends at §8).
- README quickstart, onboarding, cli-reference §15/16, mcp-api §13: none mention the knob.
- `gitreins init` DOES write the block (all false) into a fresh repo's config — so a new
  user eventually finds it, but only after the dead end; and this repo's own tracked
  `.gitreins/config.yaml` predates 0.15 and has no `resolution:` block at all (the
  project does not run its own flagship).

Working config that made the whole table above run:

```yaml
resolution:
  enabled:
    cli: true
    mcp: true
    predispatch: true
```

(Transiently applied during the run; restored byte-exact afterward — sha256-verified.)

## Finding 2 (P1): resolution verdicts are never persisted

`gitreins report` and `gitreins serve` showed NOTHING from any resolve/preflight run:
no new entry in `.gitreins/history/`, no usage.jsonl line (only `tier2` judge lines),
and `grep persist engine/resolution.py engine/preflight.py` hits only a docstring
(resolution.py:1629). This is the same class as DF-GITREINS-POC-23 (MCP judge verdicts
invisible) and violates the spec's own acceptance criterion (jev-resolution-gate.md §8:
"persisted where `gitreins serve` can show it — the DF-GITREINS-POC-23 lesson"). The
verdict records themselves are excellent (cost, tokens, manifest, model build, attempts)
— they just evaporate.

## Finding 3 (P2): preflight record omits `band`

`gitreins resolve --json` returns the verdict object (verdict/probability/…); `gitreins
preflight --json` returns the dispatch record {band, decision, probability,
missing_kind, question, reason, abstain_reason, verdict_json}. A foreman scripting the
decision gets `decision` — fine — but the record's `verdict_json` is an EMBEDDED JSON
STRING (requiring a second `fromjson`), and scripts keying on `verdict`/`band` only get
`band`. Cosmetic-but-real API friction; document the dual-shape or flatten it.

## What "using it" felt like

Time-to-first-success: ~6 minutes for someone who reads the error string, ~never for
someone who doesn't (nothing documents the enable step). Once enabled, the flow is
genuinely good: 2–3s per question, sub-millidollar, honest manifests, exit codes that
match the docs exactly (0/1 for resolve, 0/2 for preflight — verified including the
argparse exit-2 case), and the fail-closed/fail-open asymmetry — the subtle design
centerpiece — behaves precisely as documented. The discrimination between a true and a
false premise on real board facts (0.85 vs 0.29) is the product working.

## Performance (Step 2b)

hyperfine, this host, gitreins 0.15.0 venv build:

- preflight warm: **2.297s ± 0.058** (10 runs); cold (drop_caches): **2.752s ± 0.067** (5 runs)
- resolve warm: **2.269s ± 0.052** (10 runs)

Both are ~85% network + Jev; nothing a user would feel against a worker dispatch that
costs minutes. **No PERF row filed** — a win nobody can feel is not a finding.

## Install leg

RUN — ephemeral fresh-system battery via `bunker-qa.sh` on bunker-las-02 (agent
dedef735, ttl 4h, spawned → used → destroyed cleanly; 16 evidence rows). Fresh-install
OK: gitreins 0.15.0 installed from source on bare Debian after the harness bootstrapped
venv + zig-cc toolchain + GNU make; the native test suite PASSED, including a run under
a 3G memory cap; upgrade path 0.14.0 → HEAD clean. `act`-based CI failed rc=1 inside
its own docker environment (the harness's designed native-suite fallback carried the
cell). docker-deploy/chaos-shutdown/chaos-corruption were honestly N/A (no compose
file, no db files — this is a CLI library). Evidence:
/tmp/dogfood-gitreins/bunker-qa-evidence.jsonl.
