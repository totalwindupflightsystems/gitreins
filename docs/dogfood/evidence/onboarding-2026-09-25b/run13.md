# Run 13 evidence — onboarding surface, 2026-09-25

Environment: scratch repo /tmp/dogfood-gr-0925/proj (fresh git init), PyPI
gitreins 0.15.0 in /tmp/dogfood-gr-0925/userenv. Host had no
GITREINS_LLM_API_KEY for the standalone-refusal probe; a second probe ran with
one exported (real Tier 2 verdict, id 3d3c57fb). No repo files modified.

## Command log (verbatim results)

```
$ gitreins install          → RC=0, hook+config+6 gitignore entries (doc promises 11)
$ gitreins init             → RC=0, 0.12s, python detected, uv run pytest, mypy+pyright
$ gitreins resolve "does add() handle negatives"
  → ABSTAIN surface-disabled, RC=1, fix printed (§2 verified fail-closed)
$ gitreins guard            (no staged) → DEGRADED PASS, skips named, RC=0
$ gitreins guard            (tests staged) → PASS 4/4 lanes, RC=0
$ git commit                → hook re-ran guard, RC=0
$ gitreins task create fix-auth "Fix authentication" "Login..." "Invalid..."
  → RC=0, criteria echoed
$ gitreins task create api-crud "CRUD endpoints" "POST /api/users creates a user" --depends-on build
  → RC=0 (created; 'build' does not exist — POC-65)
$ gitreins task start fix-auth → RC=0
$ gitreins task complete --skip-tier2 fix-auth → tier1 PASS, verdict 058d12b9, RC=0
$ gitreins task complete t3 (no credential) → refuses, names --skip-tier2
$ GITREINS_LLM_API_KEY=... gitreins task complete t3 → real Tier 2 verdict 3d3c57fb PASS
$ gitreins task worktree t3 → FAILED:
  Error: git worktree add -b gitreins/task/t3 ... failed:
  fatal: cannot lock ref 'refs/heads/gitreins/task/t3': 'refs/heads/gitreins' exists (POC-62)
$ git branch -a → gitreins, master
$ gitreins worktree list → "No worktrees registered. Fleet cap: 2" RC=0
$ gitreins worktree doctor → Resolution: valid; "Fleet board: not configured" honest note
$ gitreins worktree fresh --cmd "echo fresh-ok" → exit 0 in 0.035s
$ gitreins worktree repro --cmd "true" -k 2 → 2/2 passed (pass rate 1.00)
$ commit-msg hook (doc snippet) → "commit audit: no pipeline stage with type
  commit_audit for trigger commit-msg — audit NOT run", commit succeeded (doc-exact)
$ gitreins report -n 3 → 2 evaluations, 100% pass, storage: git
```

## Perf (Step 2b)

```
hyperfine --warmup 3 --runs 20 'gitreins guard'
  258.6 ms ± 11.7 ms [User 173.0ms, Sys 62.3ms] min 236.1 max 287.2 (warm)
hyperfine --warmup 3 --runs 20 'gitreins task list'
  83.2 ms ± 5.1 ms
```

## Bunker install leg (las-bunker-03, agent caeeca94, ttl 2h, DESTROYED + verified gone)

```
ssh probe: HOST_OK / bunkerd active / Docker 26.1.5
git clone https://github.com/totalwindupflightsystems/gitreins.git ~/app → 6.6s, HEAD 3817cc4 (public origin, no visibility change)
pip install gitreins → PEP 668 externally-managed-environment, exit 1 (POC-64; rc eaten by tail pipe on first attempt, re-probed)
python3 -m venv .venv && .venv/bin/pip install -e . → 20s, gitreins 0.15.0
smoke (fresh /tmp/smoke-df-GD8N repo): install → init → guard =
  DEGRADED PASS (skips: lint/tests no staged), secrets ✓ via builtin
  cross-check with HONEST warning: "gitleaks not found at expected path .../go/bin/gitleaks"
hook commit → RC 0
first smoke attempt in /tmp/smoke was voided: dir pre-polluted by an earlier
lane's clone (rm: Permission denied on corpus/sources.json); rerun in mktemp -d — no repo impact
```
