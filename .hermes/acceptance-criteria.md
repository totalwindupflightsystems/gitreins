# GitReins PoC — Acceptance Criteria

> **Bootstrapped:** 2026-06-21 by cron wake (first run — no prior AC file)
> **Project:** GitReins — Git-Native Agent Co-Harness
> **Language:** Python 3.11 (MCP server + CLI)
> **Container:** opencode-gitreins-poc (port 4102, v1.17.7)
> **Binary:** `.venv/bin/python3 gitreins/cli.py`
> **Test runner:** `.venv/bin/pytest tests/ -x --tb=short -q`
> **MCP transport:** stdio (JSON-RPC 2.0 line-delimited)
> **Last run:** 2026-06-25 20:00 UTC — maintenance mode, all clear. 759 passed, 7 skipped. Tier 1 PASS. Container healthy (Up 16h).

## Demo Infrastructure

| Service | Command |
|---------|---------|
| MCP Server | `PYTHONPATH=. .venv/bin/python3 gitreins/cli.py mcp-server` |
| Guards (CLI) | `.venv/bin/python3 gitreins/cli.py guard` |
| Evaluator | `DEEPSEEK_API_KEY=$DEEPSEEK_API_KEY .venv/bin/python3 gitreins/cli.py judge <id>` |
| Test suite | `.venv/bin/pytest tests/ -x --tb=short -q` |
| Setup tools | `.venv/bin/python3 gitreins/cli.py setup-tools` |

---

## AC-010 — Guards (Tier 1)

**Status:** ✅ passed (2026-06-21)
**Dependency:** AC-020 (MCP must serve guard.run)

### AC-010a: Secrets guard detects API keys

✅ passed — Guard passes on clean repo. gitleaks configured with `.gitleaks.toml`.

### AC-010b: Lint guard catches code quality issues

✅ passed

### AC-010c: Test guard runs tests

✅ passed — 759 tests as of 2026-06-25

### AC-010d: Static analysis guard works

✅ passed — `guard` shows ✓ static_analysis

---

## AC-020 — MCP Server (JSON-RPC Protocol)

**Status:** ✅ passed (2026-06-21)
**Dependency:** None (foundational)

### AC-020a-020f: All MCP tools operational

✅ passed — 10+ tools registered: configure, guard.run, commit, judge.evaluate, task.create/start/complete/delete/get/list. Full round-trip test suite passes.

---

## AC-030 — Evaluator (Tier 2)

**Status:** ✅ passed (2026-06-21)
**Dependency:** AC-010, AC-020

### AC-030a-030c: Evaluator judges tasks using LLM with caps

✅ passed — Evaluator with EvalCap system, verdict history store, report command.

---

## AC-040 — CLI

**Status:** ✅ passed (2026-06-21)
**Dependency:** AC-010, AC-030

### AC-040a-040c: All CLI subcommands work

✅ passed — install, init, task, guard, judge, commit, mcp-server, setup-tools, report all functional.

---

## AC-050 — Commit Flow

**Status:** ✅ passed (2026-06-21)
**Dependency:** AC-010, AC-020

### AC-050a-050b: Commit flow with guard integration

✅ passed — Guards run on commit, block on secrets, pass on clean tree.

---

## AC-060 — Dead Code Detection

**Status:** deferred (2026-06-21)

---

## AC-070 — Skylos Integration

**Status:** deferred (2026-06-21)

---

## AC-080 — LSP Guard

**Status:** ✅ passed (2026-06-22)

✅ LSP guard: engine/lsp.py (337 lines), tests/test_lsp.py (208 lines, 22 tests), GuardManager wired.

---

## AC-090 — Static Analysis Guard

**Status:** ✅ passed (2026-06-21)

✅ `static_analysis: true` in config, guard passes.

---

## AC-100 — Pre-Commit Hook Reliability

**Status:** deferred (2026-06-21)

---

## AC-110 — Secrets Scanner Completeness

**Status:** ✅ passed (2026-06-22)

✅ 68 tests in tests/test_secrets_completeness.py, 17 fixture files. All 759 tests pass.

---

## AC-120 — Guard Exit Codes & Commit Blocking

**Status:** ✅ passed (2026-06-22)

✅ 8 integration tests in tests/test_guard_exit.py.

---

## AC-130 — Config Loading Priority Chain

**Status:** ✅ passed (2026-06-22)

✅ 16 tests in tests/test_config_priority.py.

---

## AC-140 — Static Analysis Guard Tests (Tier 1)

**Status:** ✅ passed (2026-06-23)
**Dependency:** AC-010, AC-090
**GitReins task:** sa-guard-tests

### AC-140a: Static analysis guard runs on commit

**How to verify:**
```bash
cd /home/kara/gitreins-poc && .venv/bin/python3 gitreins/cli.py guard 2>&1 | grep static_analysis
# Expected: ✓ static_analysis
```

### AC-140b: Tool dispatch per language tests exist

**How to verify:**
```bash
cd /home/kara/gitreins-poc && .venv/bin/python3 -m pytest tests/test_static_analysis.py -x --tb=short -q 2>&1
# Expected: 46 passed
```

✅ **Verified (2026-06-23):** 46 tests in tests/test_static_analysis.py covering all parsers (mypy, pyright, sorbet, sqlfluff, phpstan), tool discovery (find_tool, list_available_tools), command building, and run_static_check with mocked subprocess. Axiom delegation via opencode-gitreins-poc container. Commit e3747e2.

---

## AC-150 — Static Analysis Init Integration

**Status:** ✅ passed (2026-06-23 — was already implemented, verified this wake)
**Dependency:** AC-140
**GitReins task:** static-analysis-init

### AC-150a: gitreins init auto-detects static analysis tools

**How to verify:**
```bash
cd /tmp && mkdir -p gr-init-sa && cd gr-init-sa && git init -q && \
  /home/kara/gitreins-poc/.venv/bin/python3 /home/kara/gitreins-poc/gitreins/cli.py init 2>&1 | grep -i 'static analysis'
# Expected: shows detected tools (mypy ✓, pyright ✓ for Python)
```

✅ **Verified (2026-06-23):** Init already had static_analysis detection via _detect_static_analysis_tools() (cli.py:420). For Python projects, detects mypy + pyright and enables `static_analysis: true` with `static_analysis_tools: {python: [mypy, pyright]}`. For compiled/unknown languages, shows "disabled". No code changes needed — already shipped.

---

## AC-160 — Static Analysis Evaluator Tests (Tier 2)

**Status:** ✅ passed (2026-06-23)
**Dependency:** AC-140
**GitReins task:** sa-eval-tests

### AC-160a: Evaluator feeds static analysis output to LLM

**How to verify:**
```bash
cd /home/kara/gitreins-poc && .venv/bin/python3 -m pytest tests/test_eval_static_analysis.py -x --tb=short -q 2>&1
# Expected: 10 passed
```

✅ **Verified (2026-06-23):** 10 tests in tests/test_eval_static_analysis.py covering: disabled-by-default gating, empty tools, mypy configured, path arg scoping, tool failure handling, EVALUATOR_TOOLS definition check, tool exclusion when disabled, pyright configured, return structure validation, multiple tools. Axiom delegation. Commit 059a9d4.

---

## AC-170 — Setup Tools Command

**Status:** ✅ passed (2026-06-23)
**Dependency:** AC-140
**GitReins task:** setup-tools-cmd

### AC-170a: gitreins setup-tools is discoverable and functional

**How to verify:**
```bash
cd /home/kara/gitreins-poc && .venv/bin/python3 gitreins/cli.py setup-tools 2>&1
# Expected: shows mypy ✓ found, pyright ✓ found, exits 0
```

✅ **Verified (2026-06-23):** Command implemented via Axiom delegation (44 lines added to cli.py). Detects installed tools via find_tool(), shows install commands from _TOOL_INSTALL_GUIDE for missing tools. Exits 0. `setup-tools --help` works. For Python projects: shows mypy + pyright found, plus install hints for sorbet/sqlfluff/phpstan. Commit b37fa55.

---

## ALL CRITERIA PASSED ✅

**Summary (2026-06-24):**
- 17 acceptance criteria total
- 14 passed
- 3 deferred (AC-060 dead code, AC-070 skylos, AC-100 pre-commit hook)
- Full test suite: 759 passed, 7 skipped
- Tier 1 guards: all PASS (secrets, lint, tests, static_analysis, lsp)
- Latest commits: 9d14b5b (GR-060 LSP retry), c0ddea8 (v0.8.1 fix), 25f7b02 (v0.8.0 file_scope)
- Container: healthy, recreated this wake (was missing from `docker ps -a`)
