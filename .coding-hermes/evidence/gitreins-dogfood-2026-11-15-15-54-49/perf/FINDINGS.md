# Perf leg — gitreins-dogfood-2026-11-15-15-54-49 (fire 3)

Both earlier fires of this tick recorded timings that did not survive into any artifact
(the first fire's `FINDINGS.md` marks `5,547 ms` / `3,217 ms` / `17,367 ms` as shell-measured).
This leg re-measures the headline operation **with retained in-band artifacts**, so the numbers
can be checked instead of believed. No code was changed to produce this.

## What was measured

Headline promise operation: `gitreins resolve "<premise question>" --json` — trace seeds with
Hilo, assemble a token-bundled evidence set, ask Jev for a calibrated probability.
Question used verbatim from the first fire, so the arms are comparable:

    Does this repository implement the pre-dispatch premise check in a module named engine/preflight.py?

## Protocol

* Binary: `…/dogfood-gitreins-dogfood-2026-11-15-15-54-49/repo/.venv/bin/gitreins --version`
  (consumer install built by the first fire; see `../env.txt`).
* Tree: `…/clone-fresh`, HEAD `60eb8fac`. Code-equivalent to main `f2704a28`:
  `git diff --name-only 60eb8fac..f2704a28` touches **only `.coding-hermes/`** (board + evidence),
  zero `engine/`, `gitreins/`, `tests/` files.
* Wall time via `/usr/bin/time -f '%e'`, one sample per invocation, `/proc/loadavg` taken before
  each sample. Output retained: `floor.txt`, `hilo.txt`, `resolve_warm.txt`, `resolve_warm_{1,2,3}.json`.

## Numbers

| arm | n | wall (s) | median | load1 at sample |
|---|---|---|---|---|
| binary floor (`--version`) | 3 | 0.19, 0.19, 0.22 | **0.19** | 24.4 |
| local half (`hilo graph search preflight`) | 3 | 0.06, 0.07, 0.05 | **0.06** | ~22 |
| headline, warm index (`resolve --json`) | 3 | 3.26, 2.69, 3.40 | **3.26** | 22.2–22.5 |

Warm headline reproduces the first fire's `3,217 ms` at median 3,260 ms — a **43 ms difference
under 5× the host load** (load1 22–25 here vs 4.5 at fire start). That insensitivity is the finding
about the cost, and it is not a defect: of the 3.26 s, **0.25 s is local** (0.19 startup + 0.06 index
query) and ~3.01 s is the remote Jev call plus bundle assembly. The operation is network-latency
bound, not host bound and not hot-path bound.

## Verdict: no `PERF-<n>` row

Nothing here is slow enough that a user would notice, and no local code path is worth a
flamegraph: there is no local hot path to profile — the wall time is a remote inference call that
no amount of local optimisation moves. Consistent with the first fire's verdict, now with artifacts.

## Cold arm: UNVERIFIED this fire (not a silent pass)

The state-fresh clone that a cold arm needs could not be created: the setup command
(`git clone --no-hardlinks` into a new scratch root, after `rm -rf` of that root) was **blocked by
the consent gate** — no answer within 5 minutes. It was not retried and no alternative was used to
reach the same outcome. Therefore:

* the earlier fire's **5,547 ms cold** figure and its **17,367 ms `hilo graph warm`** figure remain
  shell-measured, unretained, and **UNVERIFIED**; this record does not re-assert them;
* the *mechanism* they demonstrated (tracked `edges.jsonl` making a fresh clone's index stale) is
  independent of the timing and was already verified by inspection.

## Reproducibility observation (not a finding, cited to `GR-229`)

Three runs of the same question returned byte-comparable substantive results —
`RESOLVED / 0.96 / evidence_quality 2 / manifest 5 / seeds 12 / cost_usd 0.00051849 / input_tokens
12345 / output_tokens 96` — with distinct `request_id`s. Total spend for the leg ≈ **$0.0016**.

The same three calls left `.gitreins/usage.jsonl` rows of shape
`{"ts":…, "tokens_in":12345, "tokens_out":96, "cache_read":0, "cache_write":0, "step":"resolution"}`
— **no `model`, no `provider`, no `cost_usd`** — while the verdict object itself carries
`model: typesafe/jev-1.13-20260917` and `cost_usd: 0.00051849`. That is the mechanism behind the
already-filed `GR-229` (`/api/stats` attributes usage to the config default model and drops the
recorded cost): the ledger never persists the attribution, so no reader can recover it. No new row —
it sharpens `GR-229`, which stays pending.
