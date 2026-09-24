# GitReins Dogfood Integration — 2026-09-24 — the Parallel Worktree Fleet

**Verdict: 🟡 PROMISING-BUT-ROUGH** — the guard engine is genuinely good; the
fleet orchestrator built on top of it cannot complete its own documented
happy path on a stock install.

**Run 10 of 10. Surfaces 1-9 (PyPI/CLI/guard/judge/MCP/resolve/security-scan/
Go-guards) untouched this run; this run took the one surface no earlier run
reached: `gitreins worktree fleet` (manifest lanes, guard/judge phases,
`--merge`), plus the ephemeral-bunker install leg.**

## The promise under test

"An orchestrator can run an explicit JSON manifest of task lanes concurrently
in isolated worktrees, with optional guard and judge phases, and apply
successful lanes to canonical main via `--merge`" (README §Parallel worktree
fleet, docs/cli-reference.md §12).

## What a real consumer had to do to get the fleet to run at all

Scratch consumer repo outside the project (`/tmp/dg-fleet/consumer`): git init,
tiny `app.py` with two real bugs (subtraction instead of addition; slug not
slugging), `gitreins install`, `gitreins init`, three tasks with verifiable
criteria, a 3-lane manifest. Twelve fleet invocations later, `--merge` had
still never merged a lane. Every blocker, in the order a user meets them:

1. **Harness config must be committed.** `init` leaves `.gitreins/config.yaml`
   untracked; worktrees branch from HEAD so the lane guard dies with
   "no .gitreins/config.yaml — run `gitreins init` first" (following that hint
   inside the worktree cannot fix it). Committing it is step zero and no doc
   says so. (POC-47, POC-53)
2. **The fleet's own runtime files block its own merge gate.** With all lanes
   passing (guard exit 0), `--merge` refused every lane:
   "canonical main has uncommitted changes". The files were
   `.gitreins/worktrees.json`, `worktrees.lock`, `disposable.json`,
   `disposable.lock`, `tasks.yaml.lock` — all created by the fleet run itself,
   none covered by the installer's gitignore template
   (`GITREINS_GITIGNORE_ENTRIES`, gitreins/cli.py:51). Then `lanes.json` (the
   manifest I had to pass) blocked the next attempt; then `.venv`+`uv.lock`
   inside the worktree (created by the guard's configured `uv run pytest`).
   The main-tree gate exempts exactly {worktrees.json, worktrees.lock,
   board/events.jsonl} + history//logs/ prefixes
   (engine/worktree_manager.py:815-822) — a list that lost the race with the
   feature it gates. (POC-47)
3. **Judge-gated merge is unreachable.** With everything clean, the gate wants
   a PASS verdict for the exact lane commit. The README's own judge-phase
   example `["gitreins","judge","API-1"]` exits 1 "Task not found" — the task
   was created in-tree by the lane, but `tasks.yaml` is gitignored so a fresh
   worktree has none. `judge --ephemeral` persists nothing by design
   (EVID-003), so it can never satisfy the gate. (POC-48)
4. **Failed lanes are a tarpit.** `worktree clean` keeps failed lanes forever
   (exit 0, "Nothing to reap"); `--confirm-stale-orphan` doesn't reap them
   either (they're not stale until 24h); the reconcile error hint names plain
   `clean`, which doesn't reconcile; and a re-run REUSES a failed lane's
   stale-HEAD tree (engine/worktree_manager.py:474-499 reuse=True), so my
   fixed manifest ran against pre-feature trees. Escape required manual
   `git worktree remove --force` + `git branch -D` + `clean
   --confirm-stale-orphan`. (POC-50)
5. **Judge ≠ guard on the same tree.** `guard`: exit-5 "no tests collected" is
   a PASS with a warning (GR-GAP-048). `judge`'s Tier 1: the same condition is
   a hard FAIL (engine/pipeline.py:663 grades the exit code only), so every
   no-test-suite repo gets Overall: FAIL even when the Tier 2 LLM verifies
   every criterion by direct execution — which mine did. (POC-49)
6. **Mid-fleet edits + commits.** Lane commands must be idempotent (a rerun
   into an existing tree dies on `git commit` with nothing to commit) and
   must commit their work, because the worktree merge gate rejects dirty
   trees — including dirt the guard itself made (.venv, uv.lock). (POC-47/53)

## What worked, and worked well

- **The guard engine is the best part of this product.** In run 4, `slugify`'s
  guard correctly FAILED a lane whose command appended a duplicate `slug`
  definition — lint caught the redefinition, static_analysis (pyright) named
  the exact line. A broken lane does not slip through.
- **The Tier 2 LLM judge is honest and rigorous.** It verified my criteria by
  direct execution, quoted the command outputs, and one early run even caught
  that the lane had left `a - b` in main's tree while the worktree had `a + b`
  (that discrepancy was itself a real finding: the judge reads the canonical
  main checkout, not the worktree it was invoked from — see run log; the
  --ephemeral probe confirmed per-directory reading is correct, so the
  worktree-local judge path is the buggy one).
- **Fresh-box install (bunker leg).** Clone → venv install (17 s) → install →
  init → guard → first hook commit, all from zero state on a bare Debian
  agent, with honest degraded-mode hints (gitleaks path, linter skip). After
  installing pytest, the full loop ran clean. (With the pytest-missing caveat:
  POC-51.)
- Exit codes and JSON reports matched the docs everywhere they were tested;
  `worktree list` / `doctor` / `qa list` all behaved as documented.

## The verdict in one line

The engine (guards, judge, evidence) is SHIPPABLE quality; the fleet
orchestrator wrapped around it is a PROMISING-BUT-ROUGH beta — its own
artifacts jam its own gates, and the documented happy path cannot merge a
lane on a stock install. Rows DF-GITREINS-POC-47…53 carry the details.

## Reproduction pointers

- Scratch scenario: `/tmp/dg-fleet/consumer` (may be cleaned; every command
  and output quoted in this file and in the board rows).
- Fleet run records: `/tmp/dg-fleet/fleet-run2..12.json` (retained).
- Bunker install leg: las-bunker-03, agent 93435c08, TTL 2h, destroyed and
  verified gone (`bunker list` empty).
- Verdict evidence: consumer worktree `.gitreins/history/2026-09-24/*/`.
