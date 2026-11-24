# GitReins dogfood — 2026-11-24 (tick `gitreins-dogfood-2026-11-24-19-47-57`, first delivery)

Verdict: **SHIPPABLE** for the tier-1 surfaces at 0.22.0; tier-2 works end-to-end but its
runtime UX (silent multi-minute complete) is the polish blocker. Verdict rows filed:
`DF-032` (init guard-WARN mismatch), `DF-033` (`commit -m` refuses), `DF-034` (silent task complete).

## Promise

"GitReins lives inside your git repository as a quality harness … MCP tools for task
lifecycle, an agentic evaluator, git hooks that ensure nothing bypasses the quality
gates" (README @ 0.22.0). Null hypothesis: a consumer can `pip install gitreins`, `gitreins
install && gitreins init`, then guard commits and judge task completeness — all inside
their own repo, in minutes, on the documented commands alone.

## Consumer run (scratch repo /tmp/dogfood-20261124-g1/app, gitreins 0.22.0)

Tiny Python app + a vendored nested git repo tracked as ordinary content in the outer
index — set up to exercise 0.22.0's headline feature (`fingerprint.allow_nested_repositories`).

Milestones (all fresh, `time`d):

| step | result | time |
|---|---|---|
| `pipx install ~/gitreins --force` (local build of tree @ `3de89c4f`) | 0.22.0 | 4.6s |
| `gitreins install` | baseline config + pre-commit/pre-push hooks; `.gitignore` +12 entries | 0.35s |
| `gitreins init` | smart: python detected, pytest test_command, static analysis on | 0.43s |
| first guarded commit | DEGRADED PASS (allow_skips: first commit fresh repo) | ~0.5s |
| `gitreins config-validate` after opt-in key | OK | <0.2s |
| `gitreins fingerprint` default | refusal `unavailable (dirty=0, state=nested-repository)` rc=1 — exactly the documented default | 0.46s |
| `fingerprint.allow_nested_repositories: true` → same command | `state=authoritative`, digest | 0.39s |

### The 0.22.0 nested-repo promise, verified by decisive arms

- Arm A: index tracks `vend/tool/ver.txt` (100644) as ordinary content, `vend/tool/.git`
  present → digest `850d5f…`, dirty 1.
- Arm C: change the vendored file's content → digest `723133…`, dirty 2 — content under the
  nested subtree IS picked up as ordinary content.
- Arm B (decisive): move `vend/tool/.git` OUT of the workspace entirely, byte-identical
  tree otherwise → digest `723133…` — **bit-identical to Arm C's with-.git digest**. The
  nested `.git` directory contributes nothing, as the release note claims.
- Default-config refusal before opt-in reproduced verbatim (`state=nested-repository`, rc=1).

### Tier-2 judge contract, end-to-end on 0.22.0

- Key contract from `docs/model-keys.md` read as written: `GITREINS_LLM_API_KEY` + optional
  `GITREINS_LLM_BASE_URL`/`GITREINS_LLM_MODEL`. Driven with a local keyless OpenAI-compatible
  lane (LM Studio `qwen3.8-27b`) plus `GITREINS_MAX_TIME=12m`/`GITREINS_MAX_ITERATIONS=25`.
- `gitreins task complete DF-USER-99` → 66.43s total: tier1 PASS → tier2 FAIL/INCOMPLETE
  (the 27B lane returned an empty response on the coverage criterion — a weak judge, a
  correctly staged verdict) → `fix_verification` stage PASS (the GR-235 stage exists and ran)
  → overall FAIL, verdict saved id `25878013`, history dir `c5bc5b37` written, task held
  `in_progress`. The full contract behaved: staged verdicts, no silent pass, task not closed.
- `jev pre-screen unavailable (empty-bundle)` warns and runs tier-2 at full authority — named
  degradation, not a silent skip.

### What failed the promise (files: DF-032/033/034)

1. `gitreins init` writes no `hook_timeout`/`test_timeout`; the guard then warns about the
   300s≤1200s defaults mismatch on EVERY run. 5 occurrences in the first 8 minutes of a
   fresh repo. `config-validate` says OK.
2. `gitreins commit -m 'msg'` → rc=2 `unrecognized arguments: -m` + full usage dump; the
   message is positional. No hint. 3/3 arms.
3. `gitreins task complete` prints nothing between tier-1 and the verdict — no job id for
   `gitreins judge --status`, no phase/heartbeat lines. A first attempt was killed at 180s
   because silence is indistinguishable from a hang; the 66s second run proved the leg works.

## Performance (Step 2b) — honest non-finding

Consumer repo, warm (n≥3, med): `--version` 0.309s (import floor), `fingerprint --json`
0.509s, `guard` 0.772s, mis-invoked `commit` 0.281–0.307s. Cold (fresh venv, first run):
`--version` 0.484s, `fingerprint` 0.315s. Everything a user feels on this repo is
sub-second; nothing justifies a flamegraph. Historical hot spots (PERF-3 family,
22s→1.15–2.1s commit gate) already carry their own rows (`PERF-3-R2-R2` pending).
No PERF row filed this fire — a win nobody can feel is not a finding.

## Install leg (ephemeral bunker) — install-PROVEN, battery PARTIAL

`bunker-qa.sh launch /home/kara/gitreins --keep` → agent `6b506a4b` @ `bunker-mvp` (ttl 4h),
20 collected rows (5 chaos cells NOT-COLLECTED), evidence committed under
`.coding-hermes/evidence/qa-20261124T200710Z/`:

- `fresh-install` OK — pip installs gitreins 0.22.0 from the synced clone
- `lint` OK — `ruff check .` rc=0
- `tests` OK — `3932 passed, 41 skipped` in 173.70s (3973 collected = README's number)
- `upgrade` OK — pip 0.21.0 → HEAD clean
- `ci-act` FAIL — `No space left on device` (host disk contention, **not a product claim**)
- chaos cells NOT-COLLECTED (harness leg)

## What a new user needs that the docs don't front-load

- `gitreins commit MESSAGE` is positional (no `-m`).
- tier-2 needs `GITREINS_LLM_API_KEY` before the first `task complete`; without it the run
  grinds silently (see DF-034).
- a fresh `init` repo will warn `hook_timeout=300s <= test_timeout=1200s` on every guard run
  until you hand-write the pair (see DF-032).

Left behind: this report, diagnostics.md §, skills/gitreins-usage §, dogfood-log entry,
rows DF-032/033/034, evidence dir. Unverified: `gitreins serve` (viewer) and the MCP
surface were not re-driven this fire (MCP was walked 2026-10-15; 0.22.0 claims no MCP delta).
`resolve`/Jev resolution gate not re-measured (previous fire's 3.26s warm median still
describes unchanged code paths).
