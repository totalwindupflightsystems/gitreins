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
