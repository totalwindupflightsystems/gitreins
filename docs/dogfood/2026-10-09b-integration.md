# Dogfood run 18 — Resolution gate (`resolve` / `preflight`) + commit-audit + push-check

**Date:** 2026-10-09 · **Verdict:** 🟡 PROMISING-BUT-ROUGH (for the surfaces tested)
**Untouched surface claim:** runs 1–17 never drove `resolve`, `preflight`, `commit-audit`
or `push-check`. (The sibling run the same day covered disposable verification + install.)

## Promise under test

An orchestrator facing a board row it may not need to dispatch can resolve the row's
premise against the repo's code with `gitreins resolve` (Hilo seed trace + measured-token
evidence bundle + calibrated Jev probability, fail-closed), map it to a dispatch decision
with `gitreins preflight` (fail-open, exit 0 for every verdict), and validate commit
messages with `gitreins commit-audit` — per the documented exit codes and JSON contracts.

## What worked (contracts exact)

| Probe | Expected | Got |
|---|---|---|
| `resolve` default-state, surface off | ABSTAIN `surface-disabled`, exit 1 | ✅ exact (on a fresh clone; this checkout has cli+mcp already enabled) |
| `resolve` warm, real question | typed verdict + manifest + model echo | ✅ REVIEW 0.63, 2.4–2.7 s |
| `resolve --json` | full verdict object, clip disclosure, seeds, dependency paths, cost | ✅ incl. honest "5 manifest files fell outside the clipped state" disclosure |
| fail-closed, all 3 key env vars unset + no `~/.hermes/.env` | ABSTAIN `no-credentials`, exit 1 | ✅ exact |
| key failover (invalid key first) | 401 rejected, next candidate serves | ✅ `candidate 1/7: rejected (401) → candidate 2/7: ok` |
| `preflight` fail-open, gate disabled | ABSTAIN → decision `dispatch`, exit 0 | ✅ exact |
| `preflight --json` with `predispatch: true` | machine record with `verdict` as OBJECT | ✅ (nested dict, not escaped string) |
| persistence (spec §8) | one `kind: resolution` record per call in `.gitreins/history`, `usage.jsonl` `step: resolution` line, `report` lists them in their own section, never in the judge rollup | ✅ all four (two calls → two records + two usage lines) |
| `push-check HEAD github/main` (clean range) | clean, exit 0 | ✅ 86–96 ms |
| push-check error contract | refused with named error when ref pair invalid | ✅ `PUSH REFUSED` on ambiguous arg (fail-closed exit 1) |
| `commit-audit` un-armed | named skip, exit 0 | ✅ exactly the documented line |

## Findings (filed as board rows DF-GITREINS-POC-84..87)

1. **POC-84 (P1) preflight calibration inversion.** Two obviously-false premises
   ("Does the repo implement its guard engine in Go?", "…a Rust implementation of the
   guard engine?") scored RESOLVED 0.87 / 0.89 → decision **skip-dispatch**, while a true
   control (Gitleaks secrets lane blocks API keys) scored REVIEW 0.63 → dispatch-with-note.
   A skip-dispatch on a false premise silently kills a needed dispatch — the exact failure
   class the gate exists to prevent. Lexical overlap ("guard engine") appears to masquerade
   as evidence in the Hilo bundle.
2. **POC-85 (P2) commit-audit false-negative.** `feat: rewrite guard engine in Rust…`
   over a one-line docs diff passed ("looks good", exit 0, block mode); the identical
   message class over a one-file planted diff was correctly BLOCKED by both profiles.
   LLM-nondeterministic with no deterministic floor.
3. **POC-86 (P2) hilo prerequisite invisible.** Fresh PyPI-path install (13 s) then first
   `resolve` → ABSTAIN `empty-bundle`, hint says "check that hilo is installed" — but
   nothing in onboarding/README/pyproject mentions the external hilo binary
   (engine/resolution.py:498 searches PATH and ~/.cargo/bin/hilo).
4. **POC-87 (P3) ssh clone fails on a fresh box** (`Host key verification failed`, 3 s);
   https clone works. Docs should name the https URL.

## Performance (nothing a user feels — no PERF row)

- `resolve` warm: 2561 / 2494 / 2723 ms; cold (fresh hilo cache, scratch copy): 3188 ms.
- `push-check`: 86–96 ms. `doctor`: 96 ms. All comfortably fast.

## Install leg (RUN, bunker-mvp agent a4912683, destroyed + verified gone)

Named host bunker-las-03 offline (ssh timeout); bunker4 offline; bunker-las-02 UP but
spawn failed transiently (`slice-limits: containment landing did not converge`) and the
retry succeeded on spawn, then the node dropped off the tailnet before the clone — leg
completed on **bunker-mvp** per the sibling-host rule. Clone (https) 6 s @ 3549660,
`python3 -m venv + pip install -e .` **13 s**, `gitreins --version` → 0.16.0, smoke =
first real `resolve` (honest ABSTAIN, see POC-86) + `preflight` (exact fail-open
contract). No repo visibility/permission change; ssh-clone failure recorded as POC-87.
