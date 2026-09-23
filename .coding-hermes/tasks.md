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
