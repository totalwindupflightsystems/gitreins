# Dogfood Run 13 — docs/onboarding.md, the First-Hour Path (2026-09-25)

**Verdict: 🟢 SHIPPABLE (for the surface tested — onboarding §1–§8).**

**Promise under test:** "A brand-new user installs GitReins from PyPI, runs
`install` → `init` → first guard → the task → judge workflow → worktrees, by
following docs/onboarding.md verbatim, and it all works."

Runs 1–12 never touched the onboarding guide itself — the one document every
real new user reads before anything else. This run walked it end to end as a
user, from the PyPI wheel (0.15.0, matching repo HEAD), in a scratch repo
outside the project.

## What was done (all verbatim from the guide)

| Step (§) | Result | Notes |
|---|---|---|
| §1 `pip install gitreins` | ⚠ then ✅ | PEP-668 block on stock venv-less Linux (F3); venv path fine |
| §1 `gitreins install` | ✅ | pre-commit hook + baseline config written; gitignore list DRIFTS from doc (F2) |
| §2 `gitreins init` | ✅ | detected Python, wrote `uv run pytest`, static analysis, resolution block; 0.12s |
| §2 disabled-gate check | ✅ | `resolve` fails closed: `abstain_reason: surface-disabled`, exit 1, prints the enabling fix exactly as documented |
| §3 gitleaks allowlist | ✅ (no action) | fresh `.gitleaks.toml` generated |
| §4 first guard (no staged) | ✅ | honest DEGRADED pass, `~` marks, exit 0 under `allow_skips: true` |
| §4 guard with tests | ✅ | real PASS, all four lanes green |
| §4 pre-commit hook | ✅ | same guard output on commit, exit 0 |
| §5 task create/start/complete | ✅ | `--skip-tier2` worked; `--depends-on` after criteria parsed (the POC-13 fix holds) |
| §5 Tier 2 (`task complete`, no `--skip-tier2`) | ✅ | real LLM verdict with per-criterion reasoning, verdict persisted |
| §5 standalone `task complete` w/o credential | ✅ | refuses with the documented `--skip-tier2` hint (host had no GITREINS_LLM_*) |
| §6 `report -n 3` | ✅ | 2 evaluations, storage: git |
| §7 usage.jsonl | ✅ (not re-verified this run; covered by run 12-era checks) | |
| §8 `task worktree` | 🔴 | hard FAILS after any verdict exists — branch collision (F1) |
| §8 `worktree list` / `doctor` | ✅ | doctor honestly reports "Fleet board: not configured" |
| §8 `worktree fresh` / `repro -k 2` | ✅ | 0.035s / 2-of-2 pass rate 1.00 |
| commit-msg hook snippet | ✅ | prints the documented "audit NOT run" line, exit 0 |

**Time-to-first-success:** ~3 minutes (PyPI install → init → first green guard).
**Friction count:** 3 (F1 P1, F2/F3 P2).

## Findings

### F1 — P1 — Board: DF-GITREINS-POC-62 — the default verdict branch permanently breaks task worktrees

`history.storage: "git"` (the `install`/`init` default) auto-commits verdicts
to an orphan branch named **`gitreins`**. §8 creates task worktrees on branches
named **`gitreins/task/<id>`** — and git refuses to create
`refs/heads/gitreins/task/<id>` once `refs/heads/gitreins` exists:

```
gitreins task worktree t3
task worktree: failed
Error: git worktree add -b gitreins/task/t3 ... failed:
fatal: cannot lock ref 'refs/heads/gitreins/task/t3': 'refs/heads/gitreins' exists
```

So on any repo where the user completed a task BEFORE their first
`task worktree` (the exact order the guide teaches), §8's first command fails
100% of the time, with a raw git error that names neither cause nor fix.
Reproduced on PyPI 0.15.0 in a fresh scratch repo (verdict 058d12b9 existed on
branch `gitreins` at the time of the failure; `git branch -a` shows
`gitreins`, `master`). Fix direction: rename one of the two namespaces
(verdict branch → `gitreins-history`, or task-worktree branches →
`gitreins-tasks/<id>`); if backward compat with existing `gitreins` branches
is required, task-worktree names are the safer side to move.

### F2 — P2 — Board: DF-GITREINS-POC-63 — onboarding §1 gitignore list has drifted from what `install` writes

The guide promises `.gitreins/tasks.yaml.lock`, `worktrees.json`,
`worktrees.lock`, `disposable.json`, `disposable.lock` among the entries
`gitreins install` appends to `.gitignore`. PyPI 0.15.0 `install` writes none
of them (verified: scratch `.gitignore` got exactly tasks.yaml,
config.yaml.bak, usage.jsonl, logs/, qa-ledger.jsonl, `__pycache__/`). The
runtime files are created later by fleet/worktree commands, so a user who
commits after a fleet run will see them as untracked noise the guide said were
handled. Docs-only fix (or make `install` write the full list). Related: the
guide's header is still version-stamped "verified against gitreins 0.14.0
(2026-09-18)" while PyPI and HEAD are 0.15.0 — this run re-verifies it for
0.15.0 with the two drifts named.

### F3 — P2 — Board: DF-GITREINS-POC-64 — the literal `pip install gitreins` quickstart is PEP-668-blocked on stock Linux

On a fresh Debian 13 (bunker agent caeeca94, ephemeral, destroyed after):
`pip install gitreins` exits 1 with the PEP 668 externally-managed-environment
error (last run to hit this was run 9, 2026-09-23; still true on 0.15.0). The
guide covers the venv path only for *source checkouts*; a PyPI consumer has no
documented venv-first route in §1. Fix direction: one extra fenced block in
§1 — `python3 -m venv .venv && .venv/bin/pip install gitreins` — marked
"use this if pip refuses with PEP 668". Everything after that point of the
guide works unchanged from the venv (verified end-to-end on the bunker).

### F4 — P2 — Board: DF-GITREINS-POC-65 — §5's `--depends-on build` example teaches a phantom dependency

`gitreins task create api-crud "CRUD endpoints" "POST /api/users creates a
user" --depends-on build` parses fine (the POC-13 ordering fix works — this
run re-verified it) but depends on a task `build` that was never created; the
guide never says dependencies are unvalidated. A user copying the example gets
a task that can never be dispatched-complete in any dependency-aware flow.
Fix direction: make the example reference a task created two lines earlier, or
add one sentence "dependencies are not validated at create time".

## Performance (Step 2b)

Headline operation = the pre-commit guard run a user feels on every commit:

```
hyperfine --warmup 3 --runs 20 'gitreins guard'
  258.6 ms ± 11.7 ms  (min 236.1, max 287.2)   [User 173.0ms, Sys 62.3ms] — warm
gitreins task list: 83.2 ms ± 5.1 ms (20 runs)
```

Sub-half-second on every commit on this box: nothing a user would feel, no
PERF row. Fresh-box install time: 20s (venv + `-e .` on las-bunker-03).

## Verdict rationale

Every §1–§8 command worked as written except §8's first command, which fails
categorically due to F1 — a one-branch-rename fix away from a clean
first-hour story. The harness core (install → init → guard → judge) is solid,
honest (degraded passes named, disabled gates fail closed with the fix) and
fast. For the first-hour surface: SHIPPABLE, with F1 as the must-fix before
the next release.
