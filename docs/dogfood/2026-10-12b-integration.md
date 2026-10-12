# Dogfood Run 20 — commit-audit deterministic floor (re-proof) — 2026-10-12

Target: gitreins 0.16.0 @ 7800f01 (fix commit a4567a5, "deterministic commit audit floor",
landed 2026-10-12 addressing DF-GITREINS-POC-85 from run 18).

## Promise under test

"A message outside the deterministic floor's recognized contradictions can still be accepted
or rejected differently across runs; the floor guarantees rejection only for unambiguous
prefix/diff contradictions (feat:/docs: vs docs-only/source-only), with no LLM credential
needed." — docs/cli-reference.md §"Deterministic floor and limits" (new in a4567a5).

## Re-proof: the documented five-run smoke (10/10 GREEN)

Executed the docs' smoke script verbatim (README-edit case + planted-file case, 5 runs each,
`feat: rewrite guard engine in Rust` over documentation-only diffs, mode: block):

- Control host, repo venv 0.16.0: run 1-5 rc=1 + "documentation-only" each case. 10/10.
- Fresh bunker agent (see install leg below): 5/5 rc=1 + "documentation-only" after the
  stage id was set to the documented `commit_audit` (see finding POC-88 for why the first
  attempt differed).
- The inverse floor also works: `docs:` over a source-only diff (app.py) blocks rc=1 with
  "staged diff contains only source files: app.py".
- Repo's own suite: tests/test_commit_audit.py 112 passed in 1.81s.
- Docs checks: check_cli_examples.py 132 examples parse; check_cli_doc_sync.py 18
  subcommands match live CLI.

Verdict on the fix itself: the floor does exactly what the new doc section promises.

## New findings (filed as board rows)

### DF-GITREINS-POC-88 (P1) — stage-id lookup contract

cli.py reads `result["stages"]["commit_audit"]` by hardcoded id. A config with a custom
stage id (`{id: ca, type: commit_audit, ...}`) runs and blocks inside the Pipeline (DEBUG:
"Running stage: ca") but the CLI misses the result, prints the FALSE skip line
"pipeline stage armed for trigger commit-msg was skipped (condition not met) — audit NOT
run" and exits 0. A custom-id block gate blocks nothing. Proven on control host and fresh
bunker box; canonical id blocks correctly in both places.

### DF-GITREINS-POC-89 (P1) — fail-open on credential death in mode: block

With every LLM credential env unset (or present-but-401 — the control host's own
GITREINS_LLM_API_KEY hits a 401 fallback), a message outside the floor gets
"WARNING: Commit audit LLM call failed: 401 ..." then "✓ Commit message looks good." rc=0.
Block mode's promise holds only for the floor's small recognized set; everything else
opens when the credential dies.

## Performance (headline op, no PERF row)

`gitreins commit-audit` on the floor path (fresh scratch repo, mode: block, no creds):
10 runs, median 163.9ms, min 154ms, max 186ms. Nothing a user feels; no PERF row filed.

## Install leg (fresh machine, RUN)

- bunker CLI 0.2.0; bunker-las-03 + bunker-las-04 offline (ssh timeout); bunker-las-02 and
  bunker-mvp reachable with bunkerd active. Spawned ephemeral agent 1c2fa709 on
  bunker-las-02 (ttl 2h).
- Clone https://github.com/totalwindupflightsystems/gitreins.git @ 7800f01 (public origin,
  no visibility/permission change): OK.
- Bare Debian user: make present, no managed pip (PEP-668) → venv route (documented
  pattern from prior runs); `pip install -e .` 17s; `gitreins --version` = 0.16.0.
- Smoke: documented floor case 5/5 rc=1 (after POC-88 workaround: stage id=commit_audit).
- Agent DESTROYED, verified absent from `bunker list`.

## Verdict

🟢 SHIPPABLE for the surface tested — the POC-85 fix re-proves green end to end on a fresh
machine with its new documented contract, and the floor path is fast. Two new P1s (88, 89)
are gate-integrity gaps in the CLI wrapper around the engine, not in the floor itself.
