# Run 14 raw evidence — report / commit / setup-tools (2026-09-25, HEAD 5e9d38d)

Environment: consumer clone /tmp/dogfood-gitreins/consumer14 (fresh clone of
/home/kara/gitreins @ 5e9d38d), gitreins 0.15.0 from the repo venv, config
copied from HEAD (secrets/lint/tests diff-mode, allow_skips). Empty repo
/tmp/dogfood-gitreins/empty14 for zero-state probes.

## P1 / report surface

```
$ gitreins report            # fresh clone, before any local verdict
Recent: 2 evaluations
  ✓ GR-GAP-033  2026-08-18 ...
  ✓ GR-GAP-031  2026-08-17 ...
Storage: git  Total entries: 2

$ gitreins report --json | jq '{schema:."$schema", checks:(.checks|length), redacted:.metadata.redacted}'
{"schema":"https://gitreins.dev/schemas/evidence/v1.json","checks":2,"redacted":true}

$ gitreins report --interactive
Interactive mode requires 'textual'. Install with: pip install textual
Falling back to text mode...    # exit 0, text report rendered

$ gitreins report               # in empty14 (no config, no history)
No verdict history found.       # exit 0
```

## F1 / POC-66 — the two commit doors

```
$ gitreins task start consumer-14 && gitreins commit "consumer-14: verdict counter reading report --json"
Tier 1 PASSED — committing...
[main 8e1c239] consumer-14: verdict counter reading report --json
 1 file changed, 37 insertions(+)
# no mention of the in_progress task

# same state via MCP (raw JSON-RPC over stdio, tools/list = 13 tools):
{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"commit",
 "arguments":{"message":"mcp probe commit"}}}
→ {"error":"Tasks still in progress: mcp-probe — commits are blocked while a
   task is in_progress because task.complete runs the quality judge against
   the committed state.", "tasks":["mcp-probe"]}
```

## F2 / POC-67 — the banner contradiction

```
# staged leaky.py with sk- token (hook leg, also the secrets regression check):
gitreins.guard: WARNING: Secrets scan: 1 potential findings
Tier 1 Guards: FAIL  (test mode: diff, full suite — safety trigger)
  ✗ secrets — FAIL (gitleaks: 1 finding; builtin cross-check: 1 finding)
      findings: 1 finding(s): leaky.py:1
  ✓ lint — ok
  ~ tests — skipped (no test files match the changed sources (diff mode))
  ~ lsp — skipped (no LSP tool on PATH (pylsp not installed))
EXIT=1   # commit refused, HEAD unchanged
```
Banner says "full suite"; the lane line one row below says "skipped". Code
sites: cli.py:2087-2088 (banner) vs guard_manager.py:1499-1500 +
2201-2209 (both the full-suite fallback and the zero-match skip set
test_targets=None); run log inherits it via _log_test_scope
guard_manager.py:1028.

## F3 / POC-68 — the fresh-clone fallback probe trail

```
# clone only fetches branches:
$ git config --get remote.origin.fetch
+refs/heads/*:refs/remotes/origin/*
$ git rev-parse -q --verify refs/gitreins/history   # canonical, POC-52
NO

# (a) as cloned: 2 stale TRACKED dirs serve
$ gitreins report → Recent: 2 evaluations (2026-08-17/18)

# (b) stale dirs untracked+removed (scratch clone only):
$ gitreins report
No verdict history found.        # origin/gitreins exists with 617 entries!

# (c) manual local branch:
$ git branch gitreins origin/gitreins
$ gitreins report → Recent: 10 evaluations ... (branch served)

# canonical ref on the origin repo:
$ git -C /home/kara/gitreins rev-parse refs/gitreins/history
e98582d...   # 626 entries; legacy branch tip 6518aa0 = 617 entries (9 behind)
```

## F4 / POC-69 — setup-tools

```
$ gitreins setup-tools
Static Analysis Tools for Python + C + SQL:
  mypy         ✓ found  (mypy)
  pyright      ✓ found  (npx pyright)
2 tools available, 0 missing.

$ env PATH=/usr/bin:/bin gitreins setup-tools
  mypy         ✗ not installed — install: pip install mypy
  pyright      ✓ found  (npx pyright)
1 tools available, 1 missing.

$ cd empty14 && gitreins setup-tools
No static analysis tools are tracked for unknown.   # exit 0
```

## F5 / POC-70 — commit with nothing staged

```
$ gitreins commit "nothing staged"
Tier 1 PASSED — committing...
On branch main
Your branch is ahead of 'origin/main' by 1 commit.
  (use "git push" to publish your local commits)

nothing to commit, working tree clean
Tier 1: DEGRADED PASS (skips: lint=no staged files, tests=no staged files,
lsp=no LSP tool on PATH (pylsp not installed))  (test mode: diff,
full suite — safety trigger)
  ✓ secrets — clean (gitleaks + builtin cross-check)
  ...
EXIT=1
```

## Perf

```
$ hyperfine --warmup 3 --runs 20 'gitreins report'
Time (mean ± σ): 89.0 ms ± 4.6 ms  [User: 74.1 ms, System: 14.9 ms]
Range (min … max): 81.2 ms … 100.6 ms, 20 runs

$ cold, brand-new clone: real 0.08s (user 0.07 sys 0.01)
$ gitreins commit happy path: 0.51 s wall (guards + hook + commit, 1 file)
```
