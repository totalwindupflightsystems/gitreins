\n## Dogfood Findings (2026-09-07)\nVerdict: PROMISING-BUT-ROUGH\nPromise: {"entry_point":"Python console-script CLI usage: gitreins [-h] [--version]
                {install,init,task,guard,judge,commit,commit-audit,mcp-server,security-scan,setup-tools,report}
                ...

GitReins — Git-Native Agent Co-Harness

positional arguments:
  {install,init,task,guard,judge,commit,commit-audit,mcp-server,security-scan,setup-tools,report}
    install             Install GitReins hooks and config in the current repo
    init                Smart init — detect language, size, optimal config
    task                Task management
    guard               Run Tier 1 guards
    judge               Evaluate a task
    commit              Commit with guard checks
    commit-audit        Validate commit message against staged diff (commit-
                        msg hook)
    mcp-server          Run MCP stdio server
    security-scan       Run the Antares CVE localization scanner (opt-in)
    setup-tools         Show available static analysis tools and install
                        instructions
    report              Show verdict history

options:
  -h, --help            show this help message and exit
  --version             show program's version number and exit, with an optional MCP stdio server launched by .","promise":"Promise: this project claims a developer or AI coding agent can manage criteria-based tasks, verify code with static guards and an agentic LLM evaluator, and prevent\n\n- [P0] The documented evaluated-task-to-commit workflow does not preserve the evaluated payload — After task complete passed Tier 1 and criterion-level Tier 2 evaluation, calculator.py and test_calculator.py were silently removed from the index. The immediately following gitreins commit exited 0 b\n- [P1] A successful commit can be materially incomplete without warning — The harness reported success even though the evaluated implementation and its five passing tests were absent from the resulting commit. Users must independently run git show --name-only and git status\n- [P1] Fresh initialization is inconsistent and leaves unexplained artifacts — Init announced 'uv run pytest -x --tb=short' but persisted 'pytest -x --tb=short', enabled static_analysis despite README default-off language, and left config.yaml.bak, usage.jsonl, and __pycache__/ \n- [P1] The product has real value but high workflow friction — Installation produced a working CLI in 15 seconds, and guards, five tests, task lifecycle, Tier 2 evaluation, reports, and all 12 advertised MCP tools worked against real data; however, the run record\n- [P2] Diagnostics, versioning, and MCP onboarding weaken usability and trust — Tier 1 evidence was truncated mid-line, versions disagreed across CLI 0.12.1, README 0.12.0, and MCP 0.1.0, and the stdio server lacked startup acknowledgement or a documented JSON-RPC client example,
\\n## Dogfood Findings (2026-09-15)\\nVerdict: PROMISING-BUT-ROUGH (two legs: fresh-machine bunker install + HEAD consumer workflow).\\nPromise: pip install gitreins, install/init in any repo, criteria tasks, per-criterion agentic judge, guards that block secrets.\\nTop findings (full rows on the real board .coding-hermes/board/tasks.jsonl):\\n- [P1] DF-GITREINS-POC-6 task complete FAILS in fresh consumer envs: init dirties config.yaml -> full-suite trigger -> tests/test_lsp.py FAILS (not skips) without pylsp -> Overall FAIL although criteria + judge PASS.\\n- [P1] DF-GITREINS-POC-7 PyPI 0.12.1 (08-28) ~70 commits behind HEAD: wheel still ships the POC-3 init mismatch, no worktree command; release pipeline (DF-010) still missing.\\n- [P2] DF-GITREINS-POC-8 tier1 failure evidence in task complete is 500 head-chars of pytest banner; failing test name never shown.\\nDetails: docs/dogfood/2026-09-15-integration.md + diagnostics.md 09-15 section + evidence/. Skill updated: skills/gitreins-usage/SKILL.md v1.1.0.\\n
## Dogfood Findings (2026-09-16)
Verdict: PROMISING-BUT-ROUGH
Promise: {"entry_point":"Python CLI console script `gitreins` (entry point `gitreins = \"gitreins.cli:main\"` in pyproject.toml, package dirs engine/, gitreins/, gitreins_mcp/), which also exposes an MCP stdio server via `gitreins mcp-server` (gitreins_mcp/server.py, JSON-RPC 2.0 line-delimited over stdin/st

- [P1] `gitreins judge` exits 0 on a FAIL verdict — the documented evaluate command cannot gate CI — Same tree, same verdict, opposite exit codes: `gitreins judge --skip-tier2 t-secret` printed 'Stage tier1: FAIL … Overall: FAIL ✗' and returned JUDGE_EXIT=0, while `gitreins task complete --skip-tier2
- [P1] guard is vacuously green on a clean tree: lint and tests skip silently and Tier 1 still reports PASS, exit 0 — Fresh repo (gitreins install + init + guard): 'Tier 1 Guards: PASS (test mode: full) / ✓ secrets / ✓ lint / ✓ tests / ✓ static_analysis', exit 0 — but the persisted run log (.gitreins/logs/guard-20260
- [P1] judge's Tier 1 is a narrower second implementation than guard's — it reported PASS on a tree where guard FAILed — Scratch repo staged a failing test (test_broken.py::test_broken) and had no packaging file: `gitreins judge --skip-tier2 t-secret` printed 'Stage tier1: PASS … Overall: PASS ✓' with verdict.json steps
- [P2] README's own --depends-on example does not parse — Verbatim from README.md:361 — `gitreins task create api-crud "CRUD endpoints" --depends-on build "POST /api/users creates a user" "…"` → 'gitreins: error: unrecognized arguments: POST /api/users creat
- [P2] Failure diagnostics are opaque or raw: unnamed LLM failure, Python traceback for a bad task id, noisy verdict lines — With a credential present but not working (env falls back through GITREINS_LLM_API_KEY → OPENAI/ANTHROPIC/OPENROUTER…, engine/llm.py:100-113) `task complete` printed 'Completed: nokey-1 → complete' th

## Dogfood Findings (2026-09-16b — qa lane)
Verdict: PROMISING-BUT-ROUGH (wheel-verification run; install leg RUN this time)
Promise: pip install gitreins (0.13.0 shipped 05:41Z today), install/init in any repo, criteria tasks, per-criterion agentic judge, guards that block secrets on commit.

- [P1] DF-GITREINS-POC-16 judge tier1 runs secrets-only — no tests, no lint; guard and judge disagree on the same tree (reopens POC-12): verdict.json stages.tier1.steps == ['secrets'] at HEAD and on the 0.13.0 wheel; staged failing test + missing pytest → judge PASS exit 0 where guard FAILs exit 1. The 09-15 "fixed at HEAD" closure was confounded by untracked secrets in the scratch repo (judge scans the whole worktree; guard scans staged scope). Discriminator reproduced both directions this run.
- [P3] DF-GITREINS-POC-15 guard's secrets verdict does not name the scanner — gitleaks vs built-in fallback coverage differs: shape-strict ghp_ token FAILs guard only where gitleaks runs; fallback warning prints only when gitleaks is MISSING. Fix direction: stamp scanner id into guard output/logs/verdict data.
- Regression matrix on the 0.13.0 wheel (fresh bunker install, las-bunker-03 agent 696a61d3, 13s to CLI): POC-16 multi-finding secrets FIXED, DF-011 hook pin FIXED, POC-13 README example parses, POC-3/D init persist FIXED, POC-10 exit codes FIXED, DF-015 version correct, worktree ships. Anti-tamper canary (exact-shape ghp_, 40 chars) BLOCKED by the control hook. Still open on wheel: POC-11 vacuous-green clean-tree guard; README quickstart hits PEP-668 on fresh Debian (venv path works); fresh venvs need pytest installed for the tests guard.
- Self-finding recorded: first canary used a malformed 33-char ghp_ token, sailed through both scanners, briefly looked like a P0 DF-012 regression — killed by direct gitleaks A/B before filing. Exact-shape fixtures or no P0. Trail: docs/dogfood/diagnostics.md 09-16b section.
Details: docs/dogfood/2026-09-16b-integration.md, docs/dogfood/diagnostics.md (09-16b), skills/gitreins-usage/SKILL.md v1.2.0 (pitfalls 18–20). Board rows: DF-GITREINS-POC-15, -16 (pending). Install leg: RUN (supersedes morning SKIPPED — host was down then, up now). Foreman not woken, cooldowns untouched per 2026-09-09 fleet law.

## Dogfood Findings (2026-09-20)
Verdict: PROMISING-BUT-ROUGH
Angle: MCP stdio server driven by a raw JSON-RPC client (12-tool surface, async judge,
disk resume) + gitreins serve over HTTP — the two surfaces runs 1-6 never touched.
Full rows on the board (.coding-hermes/board/tasks.jsonl): DF-GITREINS-POC-23 (P1),
-24 (P2), -25 (P2, harness). Report: docs/dogfood/2026-09-20-integration.md.
Bunker install leg: RUN (by hand, las-bunker-02 agent 70bc1d49, 16s PyPI to 0.14.0;
smoke clean except known fresh-venv pytest gap; agent destroyed). bunker-qa.sh launch
failures filed as -25 with evidence.

## Dogfood Findings (2026-09-20b)
Verdict: PROMISING-BUT-ROUGH
Angle: the QA-ledger / commit-msg-audit / disposable-battery surfaces and a fresh-machine
install leg on a NON-fleet consumer repo — grep of docs/dogfood/ shows runs 1-6 never touched
any of them (last run, this morning, took MCP + serve).
Promise: "A team (or an agent fleet) can record what its QA runs actually did — harness-run or
outside the harness — into one browsable ledger, gate commits on a message audit, and self-verify
the whole thing in disposable worktrees without a bunker."
Full rows on the real board (.coding-hermes/board/tasks.jsonl):
- [P1] DF-GITREINS-POC-27 worktree fresh|repro|dogfood refuse to run in any repo lacking `.coding-hermes/board/` (the fleet scheduler's layout, undocumented): WorktreeResolutionError, raw traceback, exit 1 rather than the documented infra code 2. `mkdir -p` and the identical command passes in 0.19s and self-records in the QA ledger.
- [P1] DF-GITREINS-POC-28 fresh venv install: `gitreins guard` from an UNACTIVATED venv fails `tests (full) — /bin/sh: 1: pytest: not found` even with pytest installed into that venv; the README's documented "Try the hook" first commit is BLOCKED (exit 1). `source .venv/bin/activate` -> DEGRADED PASS, tests pass, commit lands.
- [P1] DF-GITREINS-POC-29 `qa record` with neither --verdict nor --exit-code writes `verdict: UNKNOWN / status: unknown`, exit 0, contradicting docs ("a passing verdict when neither is given"); `--evidence <nonexistent>` is also accepted silently (dangling audit pointer).
- [P1] DF-GITREINS-POC-30 `commit-audit` is a silent no-op on a fresh install (empty stdout AND stderr, exit 0) unless the user hand-writes a `pipeline.stages[]` entry; and only a TOP-LEVEL `commit_audit.mode: block` actually blocks — stage-level `mode: block` (the documented placement) and `defaults.commit_audit.mode` are both dead config.
- [P2] DF-GITREINS-POC-31 `gitreins install` omits `.gitreins/qa-ledger.jsonl` from the consumer `.gitignore` (only the vendor repo's own file has it, added by the same commit as the feature), so the next `git add -A` commits fleet QA rows — agent ids, server names, evidence paths.
- [P2] DF-GITREINS-POC-32 rotation at `max_entries` is silent: with the ledger full, `qa record` exits 0 ("recorded") and the row count is unchanged — oldest row evicted without a word.
Details: docs/dogfood/2026-09-20b-integration.md + diagnostics.md 09-20b section; skills/gitreins-usage/SKILL.md v1.4.0 (pitfalls 21-25).
Install leg: RUN on las-bunker-03 (host UP; agent 3f4f7cdc spawned, used, destroyed and verified gone); all three P1s reproduce on the shipped 0.14.0 wheel. Foreman not woken; cooldowns untouched (2026-09-09 fleet law).

## Dogfood Findings (2026-09-23 — run 7: the v0.15.0 resolution gate)

Ran the never-dogfooded flagship surface: `gitreins resolve`, `gitreins preflight`, MCP
`context.resolve` (JEVRES-001..006). The gate itself WORKS — real discrimination on real
premises (true premise 0.85 → skip-dispatch; open premise 0.29 → dispatch; unanswerable
0.05; budget law enforced and disclosed; fail-closed ABSTAIN exit 1 vs preflight's
documented fail-open dispatch). ~2.3s warm per call at $0.0005 — fast enough that no PERF
row is filed (hyperfine warm+cold numbers in the integration report). Findings:

- [P1] DF-GITREINS-POC-35 Every resolution surface ships disabled with zero documentation —
  `preflight` on this very repo fails in 0.12s with `abstain_reason: surface-disabled`, the
  fix hint cites docs/jev-resolution-gate.md "§9" which does not exist (doc ends at §8),
  no doc (README quickstart, onboarding, cli-reference §15/16, mcp-api §13) mentions the
  `resolution.enabled.<surface>` knob, and this repo's own tracked config has no resolution
  block (the project does not run its own flagship).
- [P1] DF-GITREINS-POC-36 resolution verdicts are never persisted — no .gitreins/history
  entry, no usage.jsonl line, report/serve show nothing; violates the spec's own §8
  acceptance criterion and repeats the POC-23 invisibility class.
- [P2] DF-GITREINS-POC-37 preflight --json embeds verdict_json as an escaped string and
  has no top-level `verdict` field (resolve uses `verdict`; preflight exposes `band`) —
  dual shape for the same gate's output is scripting friction.
Details: docs/dogfood/2026-09-23-integration.md; diagnostics.md 09-23 section;
skills/gitreins-usage/SKILL.md v1.5.0 (resolution-gate section).
Install leg: RUN — bunker-las-02 battery complete (16 cells: fresh-install OK, native suite
PASS incl. 3G-cap run; act fallback documented in the log).

## Dogfood Findings (2026-09-23b — run 8: the security-scan guard, never touched by runs 1-7)

Ran the opt-in Antares CVE guard for real: enabled it via the README's documented
config block, staged deliberately vulnerable Python (SQL string-format + pickle.loads +
MD5), and drove `security-scan` (text/json/force-ml) and the live `gitreins guard`
commit gate, locally AND on a fresh bunker box. The scanner pipeline WORKS end-to-end
(heuristic fires on keyword lines, exit 0/1/2 exactly as the README table promises —
one earlier exit-0 reading was my own PIPESTATUS bug, retracted). Findings:

- [P1] DF-GITREINS-POC-38 config-home split: the guard reads
  `guards.security_scan.enabled` but the README's documented block puts
  `security_scan:` under `defaults:` — a user following the README verbatim gets a
  guard that SILENTLY DOES NOT RUN (`Tier 1 Guards: PASS` with no security_scan
  line; proven: documented shape → PASS-no-scan, duplicate key under `guards:` →
  FAIL fires). CLI `security-scan` reads `defaults.security_scan` (cli.py:2941),
  guard reads `guards.security_scan.enabled` (guard_manager.py:950-954). Two homes,
  one documented, one not; README + cli-reference + onboarding all document the dead one.
- [P1] DF-GITREINS-POC-39 `min_confidence` never filters scanner findings — it only
  filters the CVE FEED (cve_feed.py:221); heuristic findings are hard-coded
  confidence 0.0 (antares.py:258) and `_check_security_scan` fails on ANY finding
  (guard_manager.py:2416), so the documented `min_confidence: 0.7` knob is a no-op
  for heuristic users and the guard blocks on comment-only keyword matches (the
  word "injection" in a comment fails a commit).
- [P2] DF-GITREINS-POC-40 `--force-ml` failure message goes to stderr and the
  README's `pip install huggingface_hub transformers` hint names only huggingface_hub
  for the DOWNLOAD dep — a fresh user installing just that hits transformers-missing
  at guard time; also the guard's not-available PASS line (guard_manager.py:2391)
  is the only place the never-block-on-missing-infra promise is visible — guard
  config `model:`/`cve_source:` keys are read by nothing (scanner constructed bare
  at guard_manager.py:2395, `use_ml=False` hard-coded).
Install leg: RUN on bunker-las-03 (agent a8015da1, spawn→install 20s→guard reproduced
→destroyed+verified gone). Local probe timing: security-scan 0.096s warm — no PERF row.
Foreman not woken, cooldowns untouched per the 2026-09-09 fleet law. No code changed.

## Dogfood Findings (2026-09-23c) — run 9: the Go guard lane

Real use: a fresh Go consumer repo (`example.com/quotasvc` — quota package, a
cmd, tests) with `gitreins init`, real commits through the installed pre-commit
hook, plus the same probe on a fresh bunker box. Promise tested: "on a Go project
the commit gate compiles, vets and tests my code and refuses a commit that does
not build." Verdict: **PROMISING-BUT-ROUGH** — the lane works when files are
staged, and returns a false green in the two places a Go user needs it to be
honest. Full report: `docs/dogfood/2026-09-23c-integration.md`.

- [P1] DF-GITREINS-POC-42 Go lanes grade the INDEX: `gitreins guard --full` on a
  tree with an untracked uncompilable `.go` file prints `Tier 1 Guards: PASS
  (test mode: full, whole tree)` while `go build ./...` fails — all three lanes
  return PASS with `output: No Go files staged` before running any tool
  (`_changed_go_files` falls back to `git diff --cached`, `guards.py:82-103`;
  `_scope_files_or_none()` passes None unless scope == working-tree,
  `guard_manager.py:1037-1045`). Same with the broken file COMMITTED and a clean
  index — so the pre-commit hook passes it. `--scope working-tree` FAILs
  correctly (exit 1, real compiler text); the judge's tier1 tests step sees the
  file the guard missed (verdict 45186e59). Evidence:
  `docs/dogfood/evidence/go-lane-2026-09-23c/`.
- [P1] DF-GITREINS-POC-43 `go_lint` treats every golangci-lint FINDING as
  "linter unavailable" and falls through to `go vet`, whose verdict becomes the
  lane's (`guards.py:117-147` tests `exit_code == 0`, and `run_bounded` returns an
  `error` key only on spawn failure). An ignored `os.Mkdir` error (compiles, vets
  clean) → console `✓ go_lint — ok`, log `go vet: clean`, while
  `golangci-lint run --new-from-rev=HEAD~1 <file>` exits 1 naming errcheck.
  28 captured go_lint results: 15 vet-graded fallbacks, 9 vacuous, 2 real, 2
  correct FAILs. `go_build` already covers what `go vet` catches.
- [P2] DF-GITREINS-POC-44 the DEGRADED-PASS net is keyed on lane NAMES
  (`_SUBSTANTIVE_STEPS = {lint, tests, lsp}`, `types.py:44` vs `go_lint`/
  `go_tests`/`go_build`), so a Go run where every gate did no work is not
  degraded, prints the plain green header, and exits 0 even under
  `allow_skips: false`. The vacuous PASS is also neither failed nor skipped, so
  `guards: 4 (0 failed, 0 skipped)` reads as a full clean run.
- [P2] DF-GITREINS-POC-45 `init` prints `Test cmd: go test -short -count=1 ./...`
  but writes no `test_command` key for Go, and the Go tests lane never reads
  `guards.test_command` (hard-coded argv, `guards.py:168-173`). Python honours the
  key (control: exit 127 when pointed at a broken command).
- [P2] DF-GITREINS-POC-46 a missing Go toolchain fails all three lanes with no
  reason on the console (`passed=False, error=...`; `error` never reaches
  `summary`) — only the run log says `error: [Errno 2] No such file or directory:
  'go'`. Reproduced on the fresh bunker box; Python solved this class on purpose
  (`_resolve_test_command`, GR-GAP-037).

Regression facts GREEN this run: `init` detects Go and writes the lane defaults
(Python lanes correctly off); a STAGED uncompilable file FAILs all three lanes
with exit 1 and the pre-commit hook refuses the commit; `guards.go.lint: false`
and `tests: false` really drop their lanes; a no-scope run is honest in the log
and does not crash; the judge's tier1 leg catches a committed-broken HEAD.
Perf: whole run 822ms ± 33 warm / 736ms ± 8 whole-tree — nothing a user feels,
NO PERF row filed.

Install leg: RUN on las-bunker-03 (agent 2db38df6, ttl 2h) — README `pip install
gitreins` blocked by PEP-668 on fresh Debian (known, not re-filed); venv install
~24s; clone OK with existing public access (no visibility/permission change);
`init` + gate + commit reproduced on a box with NO Go toolchain; agent DESTROYED
and verified gone (`bunker list | grep` = 0). Foreman not woken, cooldowns
untouched per the 2026-09-09 fleet law. No code changed.

## Dogfood Findings (2026-09-24 — run 10: the parallel worktree fleet)

Scenario: scratch consumer repo (/tmp/dg-fleet/consumer), 3 tasks with real
criteria, 3-lane manifest, guard+judge phases, --merge. 12 fleet invocations;
--merge never merged a lane on a stock install. Full narrative:
docs/dogfood/2026-09-24-integration.md. Install leg RUN on las-bunker-03
(agent 93435c08, destroyed + verified gone).

- [P0] DF-GITREINS-POC-47: `worktree fleet --merge` can never merge on a stock
  install — the harness's own runtime files (disposable.json/lock,
  tasks.yaml.lock, the manifest, .venv+uv.lock in the worktree) sit untracked
  and the merge gate's hardcoded ignore set (engine/worktree_manager.py:815-822)
  misses them; GITREINS_GITIGNORE_ENTRIES (gitreins/cli.py:51) predates the
  fleet feature.
- [P1] DF-GITREINS-POC-48: judge-gated --merge unreachable in practice —
  README's example judge phase fails 'Task not found' in-tree (tasks.yaml
  gitignored), --ephemeral persists nothing; judge-failed lanes report
  error:null with the refusal reason dropped (worktree_fleet.py:238-243).
- [P1] DF-GITREINS-POC-49: judge and guard disagree — pytest exit-5 is
  'PASS + not blocking' in guard (guard_manager.py:2081) but hard FAIL in the
  judge's Tier 1 (engine/pipeline.py:663 grades exit code only); no-test-suite
  repos get Overall FAIL even when the LLM verifies every criterion.
- [P1] DF-GITREINS-POC-50: failed fleet lanes unrecoverable through the CLI —
  clean keeps them forever, the error hint names the wrong command, a re-run
  reuses the stale-HEAD tree (worktree_manager.py:474-499).
- [P1] DF-GITREINS-POC-51: fresh box, zero-deps repo — the pre-commit hook
  blocks the repo's FIRST commit ('pytest: not found' = hard FAIL) while the
  same repo's standalone guard passes (reproduced on las-bunker-03).
- [P2] DF-GITREINS-POC-52: verdict persistence fails inside fleet worktrees —
  refs/heads/gitreins collides with gitreins/task/<id> branch namespace.
- [P2] DF-GITREINS-POC-53: docs never state the fleet's operations contract
  (commit harness config, in-tree task creation, committing idempotent lanes,
  judge/verdict gate); README quickstart flow cannot merge.

## Dogfood Findings (2026-09-24b — run 11: `gitreins serve`, the judgment browser)

Angle: runs 1-10 swept CLI/guards/judge/MCP/resolve/security-scan/Go/fleet and
`report`; this run took the human-facing surface — `gitreins serve` (documented
API contract + security model) — plus the fresh-clone/install leg. Verdict
evidence: docs/dogfood/2026-09-24b-integration.md. Install leg RUN on
las-bunker-03 (agent fbf41e9d, clone+18s venv install, smoke loop, destroyed +
verified gone).

- [P0] DF-GITREINS-POC-56: serve detail pane renders ONLY the header for every
  verdict — serve.py:584-591 show() composes
  `header + criteria-join || fallback + Tier1 + Tier2 + summary + telemetry +
  evidence`; the left operand of `||` is always a truthy string, so everything
  from the fallback through the Evidence section is unreachable. Verified in
  Chrome: 216/216 verdict panes show just the header + "CRITERIA (0)" while
  /api/verdicts/<date>/<hash> serves all of it (0 of 216 render Tier 1/2,
  telemetry or evidence). Born in the original serve commit c377f0f (09-12).
  Standalone node repro + Playwright sweeps in the integration report.
- [P2] DF-GITREINS-POC-57: serve contract drift, 4 items — stats excludes
  kind=resolution rows (total 212 vs list 216) with no payload field or doc
  line saying so and the SPA card labels the 212 "JUDGMENTS" over a 216-row
  list; judgment-viewer.md:152 describes `unattributed` as unattributed usage
  LINES but usage.py summarize() counts verdicts with no telemetry;
  judgment-viewer.md:216 promises the --host warning on stderr but serve.py
  prints it to stdout (verified); aggregate cost_usd ships 0.0 where the doc
  says unpriced is null.
- [P2] DF-GITREINS-POC-58: verdict-history surface rots silently — two verdict
  dirs (2026-08-17/96dd2464, 2026-08-18/9b129d91) are git-TRACKED despite
  .gitignore:23, so a fresh clone serves stale history (bunker clone served
  total: 2) against README's "fresh clone therefore has no local
  .gitreins/history/"; and refs/heads/gitreins has no upstream and is on
  neither remote, so report's documented branch fallback can only ever be
  empty for anyone else.
- Install-leg regression: POC-51 reproduced on HEAD (fresh repo first commit
  blocked by `pytest: not found` — known open row, not refiled). POC-47
  verified FIXED (0.15.0 merge gate ignores the runtime files; commit c4a4c05).
- Serve perf: /api/stats 43.5ms±4.5 warm; SPA cold boot 844ms, reload 722ms,
  detail open 201ms, 0 longtasks (1360px Chrome, 216-row board); CLI report
  87.6ms±5.9. Nothing a user feels — no PERF row.
- Security model verified: 5 traversal shapes on verdict/evidence routes all
  400/404, evidence strictly manifest-bound, board name-bound, 0.0.0.0 bind
  works with the documented (mis-streamed) warning.

## Dogfood Findings (2026-09-25b) — run 13: docs/onboarding.md, the first-hour path

Angle: runs 1-12 never walked the onboarding guide itself — the one document
every real new user reads first (still version-stamped 0.14.0 while PyPI/HEAD
are 0.15.0). This run followed §1-§8 VERBATIM from the PyPI wheel 0.15.0 in a
scratch repo, plus the bunker install leg. Verdict: 🟢 SHIPPABLE for this
surface (first run ever to earn it): 15 of 17 documented steps worked exactly
as written; time-to-first-success ~3 min. Evidence:
docs/dogfood/2026-09-25b-integration.md + evidence/onboarding-2026-09-25b/run13.md.

- [P1] DF-GITREINS-POC-62: default verdict branch 'gitreins' (history.storage:
  git) permanently breaks task worktrees — git cannot lock
  refs/heads/gitreins/task/<id> once refs/heads/gitreins exists, so §8's first
  command fails 100% on any repo where a task was completed before the first
  worktree (the order the guide teaches). Reproduced on 0.15.0; raw git error
  names no fix. Fix: rename one namespace.
- [P2] DF-GITREINS-POC-63: onboarding §1 gitignore list drifted — install
  writes 6 entries, doc promises 11 (missing tasks.yaml.lock, worktrees.json,
  worktrees.lock, disposable.json, disposable.lock); header stamp still 0.14.0.
- [P2] DF-GITREINS-POC-64: literal `pip install gitreins` PEP-668-blocked on
  stock Linux (3rd sighting: runs 9/12/13); no documented venv route for PyPI
  consumers — one fenced block in §1 closes it. Bunker leg otherwise green
  (venv install 20s, smoke DEGRADED-pass with honest gitleaks warning).
- [P2] DF-GITREINS-POC-65: §5's `--depends-on build` example references a task
  that is never created — dependencies are unvalidated at create time, doc
  doesn't say. (POC-13's ordering fix itself re-verified green.)
- Perf: guard 258.6ms±11.7 warm (hyperfine, 20 runs), task list 83.2ms±5.1 —
  nothing a user feels, no PERF row. Install leg RUN on las-bunker-03 (agent
  caeeca94, clone 6.6s @ 3817cc4, destroyed + verified gone).
