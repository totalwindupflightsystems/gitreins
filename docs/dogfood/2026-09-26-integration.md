# GitReins Dogfood Run 15 — Disposable Verification (`worktree fresh` / `worktree repro`)

**Date:** 2026-09-26 · **Target:** gitreins 0.15.0 @ a4e89ce · **Verdict: 🟢 SHIPPABLE (for the surface tested)**

**Promise under test** (docs/disposable-verification.md, docs/cli-reference.md §worktree, untouched by
runs 1–14): *"A user can verify a command in a clean detached worktree (`worktree fresh`), run the same
command N times from one captured HEAD to measure flakiness (`worktree repro`), keep the failed trees
for inspection, and every outcome is recorded to the QA ledger — with the documented exit contracts."*

## Setup (real consumer, scratch repo outside the harness repo)

```bash
cd /tmp/dogfood-repro && git init
# one deliberately flaky test:
#   import random
#   def test_coin_flip():
#       assert random.random() > 0.5, "flaky: coin came up tails"
git add -A && git commit -m "flaky test"     # HEAD 96bec73
gitreins install   # 0.086s, wrote hook + config + gitignore entries
gitreins init      # detected Python, wrote uv test cmd, static analysis mypy+pyright
gitreins guard     # DEGRADED PASS, skips honest, exit 0
```

## What was run (the real workload)

| # | Command (documented form) | Result | Exit |
|---|---|---|---|
| 1 | `gitreins worktree repro --cmd "pytest test_flaky.py -q" -k 10 --concurrency 3 --keep-failures --json /tmp/repro.json` | `repro: 7/10 passed (pass rate 0.70) — 3 failure(s) — 3 failure(s) kept at /tmp/dogfood-repro-wt/.disposable/run-…`, 1.399s wall | 1 (propagated, matches docs) |
| 2 | re-run `pytest test_flaky.py -q` **inside a kept failure tree** | PASSED (see F1 — PATH inheritance) | 0 |
| 3 | `worktree repro -k 2` with an always-failing `-k nonexistent` selector | `0/2 passed (pass rate 0.00)`, exit 1 | 1 |
| 4 | `gitreins worktree fresh --cmd "which pytest && pytest --version && python3 --version" --json /tmp/fresh.json` | exit 0, output captured verbatim in JSON `output` field | 0 |
| 5 | `gitreins worktree list` / `worktree doctor` | "No worktrees registered. Fleet cap: 2" / "Resolution: valid" (honest zero-state, board-absent note) | 0 |
| 6 | `gitreins worktree clean` | `Reaped 3 disposable run(s)` | 0 |
| 7 | `gitreins qa list` / `--json` | 3 rows: ✗ repro 7/10 exit 1, ✗ repro 0/2 exit 1, ✓ fresh 1/1 exit 0 — every outcome outlived the reaped trees | 0 |
| 8 | self-check: `gitreins worktree dogfood --skip-judge --json` in the real repo (headless) | exit 0; init/task/guard passed, judge skipped (`--skip-judge`), tree reaped | 0 |

**Exit contracts verified:** nonzero command exit propagates unchanged (repro exit 1, fresh exit 1 with
`--cmd false --keep`), exit 0 on all-pass, JSON evidence shape exactly as docs show
(`command/k/concurrency/head/passes/failures/pass_rate/runs[]` with per-run `exit_code`, `duration_s`,
`tree`, `kept`).

## Findings

### F1 (P2 → DF-GITREINS-POC-71): kept failure trees are not self-contained — child PATH is inherited, not the tree's toolchain
The flag `--keep-failures` exists to preserve a failing state for inspection. But the disposable run's
child resolves `pytest` from the **consumer session's PATH** (proved: `fresh --cmd "which pytest"` inside
the repo printed the *home checkout's* `/home/kara/gitreins/.venv/bin/pytest`). Re-running the test in a
kept tree therefore uses a different interpreter than the failing run — here it flipped FAIL → PASS.
Related: the home repo's own `.venv/bin/pytest` is a dead shebang
(`#!/home/kara/gitreins-poc/.venv/bin/python` — dir renamed), so the guard's tests lane is unrunnable by
hand while still grading PASS in the harness. Two faces of one contract gap: what environment do
disposable/child runs promise?

### F2 (P2 → DF-GITREINS-POC-72): no documented performance envelope (see Perf below)

### F3 (P2, folded into POC-71): stale venv shebang on the home checkout
`.venv/bin/pytest` shebang points at `/home/kara/gitreins-poc/.venv/bin/python` (pre-rename path);
`python -m pytest` works. uv recreated the venv at the new path but bin shebangs were not all rewritten.

### F4 (P2 → DF-GITREINS-POC-73): `worktree dogfood --skip-judge` human summary reads as a failure
JSON is honest (steps named, statuses, judge skip reason). The human line "dogfood: 3/4 steps passed;
judge skipped" looks like a failed step to a CI consumer keying on the passed-count.

## Performance (Step 2b — coding-hermes-perf law: measure, same command, warm)

hyperfine 10 runs, warm (cold-cache drop needs root — warm numbers only, stated as such), toy repo
(1 test), this host, release console script:

| Operation | Mean ± σ |
|---|---|
| `worktree fresh --cmd "pytest test_flaky.py -q"` | **0.3505 s ± 0.0127** |
| raw `python -m pytest test_flaky.py -q` (baseline) | 0.0800 s ± 0.0121 |
| → harness overhead per disposable run | **~4.4x** at toy scale (tree create + reap + env) |
| `worktree repro -k 10 --concurrency 3` | **1.258 s ± 0.116** |
| 10 × sequential raw pytest (baseline) | 2.181 s ± 0.283 |
| → repro at k=10 is **faster than sequential raw** despite per-run overhead | ~1.7x |

Single-step timing shows no cold-cache anomaly (fresh: 0.61s → 0.58s → 0.57s → 0.58s across back-to-back
runs). Nothing here is slow enough that a user would notice at realistic test sizes (a 5s test suite is
~3% overhead at k=1) — **no PERF row filed**; the finding is that these numbers exist nowhere in the
docs for users who need to size `-k` (POC-72).

## Install leg — SKIPPED-install-bunker (explicit, evidence attached)

- **bunker-las-03** (the skill's standard node): ssh connect timeout; tailscale "offline, last seen 7h
  ago"; ping 100% packet loss — same outage as run 14.
- **bunker-las-02** (fallback): HOST_OK but `systemctl is-active bunkerd` = **activating (auto-restart)**.
  Root journal, every 5s since a config change:
  `bunkerd: refusing to start: refusing to bind non-loopback plaintext listener :10002, :10001: set
  tls.enabled: true to serve TLS, or explicitly set tls.insecure_dev: true` — restart counter **27343**;
  `bunker list --server bunker-las-02` → `dial tcp …:10001: connect: connection refused`.
- Other nodes: las-01 offline 10h, las-04 offline 15h. No credential-free path remained; no
  visibility/permission was changed (hard rule honored). Row **DF-GITREINS-POC-74** carries the evidence.

## What a new user would need that the docs don't say

1. The PATH-inheritance contract for `fresh`/`repro`/`dogfood` children (F1) — the single biggest
   surprise; a kept tree that re-runs green defeats its own purpose.
2. A performance-envelope line per command so `-k`/`--concurrency` can be sized (F2).
3. `worktree doctor`'s fleet-board note ("worktree fresh|repro|dogfood run without it") is honest and
   useful — no change needed.

## Bottom line

The disposable-verification surface does what it claims with exact exit contracts, honest JSON evidence,
a QA ledger that outlives the trees, and a concurrency win at k=10. Value: real (flake triage in 1.3s
where raw sequencing takes 2.2s and gives no artifacts). Trust: high — every skip/failure state was
represented truthfully. The kept-tree reproducibility gap (F1) is the one thing standing between this
surface and a clean ✅ for the full documented workflow.
