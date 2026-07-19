# 00-PRD — GitReins Product Requirements Document

## 1. Product Vision

**GitReins is a git-native AI agent quality harness.**

AI coding agents (Claude, Hermes, Codex, Pi) generate code at unprecedented speed. But speed without quality gates produces broken commits, leaked secrets, and half-finished features. GitReins sits inside every git repository and blocks bad commits before they reach the remote.

The core insight: the commit boundary is the natural quality checkpoint. Every developer already runs `git commit`. GitReins makes that moment intelligent — running static guards (secrets, lint, tests) and, when configured, an agentic LLM evaluator that reads the actual code and judges whether task criteria are truly met.

**One command install. Works across languages. Ships on PyPI.**

---

## 2. Target Users

| User | Workflow | Value |
|------|----------|-------|
| **AI coding agents** (Claude, Hermes, Codex, Pi) | Connect via MCP stdio; create tasks, run guards, commit through the harness | Quality gates prevent bad commits from reaching production |
| **Humans using AI-assisted coding** | `gitreins install` → pre-commit hook runs automatically | Catches secrets, lint errors, and broken tests on every commit |
| **CI/CD pipelines** | `gitreins guard` in CI step | Language-agnostic static checks without per-project configuration |
| **Team leads / maintainers** | Define task criteria in `.gitreins/tasks.yaml` | Agentic evaluation verifies that completed work meets acceptance criteria |

---

## 3. Core Value Proposition

### 3.1 Tier 1: Static Guards (Fast, No LLM)

Runs on every commit. Configurable via `.gitreins/config.yaml`:

- **Secrets scanning** — pattern-based detection of API keys, tokens, credentials
- **Lint** — Python (ruff), Go (`go vet`), TypeScript (configurable)
- **Tests** — full suite or diff-mode smart selection
- **Dead code detection** — opt-in Python AST-based unused function/import detection
- **LSP diagnostics** — opt-in language-server diagnostics from staged files (undefined names, type errors, imports, and syntax issues)
- **Static analysis** — opt-in language-specific tools such as mypy, pyright, staticcheck, cppcheck, clippy, and eslint
- **Build verification** — Go projects get `go build` check

Diff-mode test selection: when `test_mode: "diff"`, GitReins maps staged source files to their corresponding test files using basename matching (`engine/foo.py` → `tests/test_foo.py`). Force-full triggers (`pyproject.toml`, `conftest.py`, config changes) run the full suite for safety.

### 3.2 Tier 2: Agentic Evaluator (LLM-Powered)

When a task is marked complete, the evaluator iterates with tools:

- `read_file(path, offset, limit)` — inspect source code
- `run_command(cmd)` — run tests, lint, build
- `search_pattern(regex)` — grep the codebase
- `read_diff()` — review staged changes
- `get_task_item(id)` — load criteria
- `sandbox_write/read(key)` — scratch space for tracking
- `detect_dead_code()` — AST-based dead code scan
- `skylos_scan()` — multi-language dead code + AI mistake detection

The evaluator delivers a structured verdict: `COMPLETE` or `INCOMPLETE`, with per-criterion `PASS`/`FAIL` status and specific file:line evidence. Caps limit consumption: `max_iterations` (default 100), `max_time` (e.g. `"30m"`), `max_input_tokens` (e.g. `"200k"`), `max_output_tokens` (e.g. `"50k"`), and `tool_call_weight` (default 0.1 — tool calls cost a fraction of an iteration).

### 3.3 One Command Install

```bash
pip install gitreins
cd my-repo
gitreins install
```

Creates:
- `.gitreins/config.yaml` — default configuration
- `.git/hooks/pre-commit` — runs `gitreins guard` on every commit
- `.gitignore` entry for `.gitreins/tasks.yaml`

### 3.4 Language Agnostic

- **Python** — ruff, pytest, dead code detection
- **Go** — `go vet`, `go test`, `go build`
- **TypeScript / JavaScript** — configurable via `test_command`
- **Any language** — custom `test_command` in config

---

## 4. Key Capabilities

### 4.1 MCP Server (10 Tools)

Primary AI agents connect via stdio JSON-RPC 2.0:

| Tool | Purpose |
|------|---------|
| `task.create` | Create task with criteria that must be verified before commit |
| `task.start` | Mark task as in-progress |
| `task.complete` | Mark task complete; triggers evaluation if LLM configured |
| `task.list` | List all tasks, filter by status |
| `task.get` | Get single task by ID |
| `task.delete` | Delete task by ID |
| `commit` | Create git commit; runs guards first; rejects if guards fail |
| `guard.run` | Run Tier 1 static guards; optional `workdir` for cross-repo use |
| `judge.evaluate` | Run full evaluation pipeline (Tier 1 + Tier 2) on a task; supports individual caps or legacy `eval_cap` string |
| `propagate` | Copy guard configuration to sibling repositories with recursive merging that preserves target overrides |

Cross-repo `workdir`: every tool accepts an optional `workdir` parameter, enabling a single MCP server instance to manage tasks and run guards across multiple repositories.

### 4.2 CLI

Human-facing commands:

```bash
gitreins install              # One-command repo activation
gitreins task create <id> <title> [criteria...]
gitreins task start <id>
gitreins task complete <id>
gitreins task list [--status pending|in_progress|complete]
gitreins task delete <id>
gitreins guard --dead-code    # Run static guards; enable AST dead-code detection on demand
gitreins judge <id> [--skip-tier2]  # Run evaluator, or Tier 1 only
gitreins commit <message>     # Commit with guard gate
gitreins commit-audit [message]  # Run configured commit-message/code review
gitreins mcp-server           # Start MCP stdio server
```

`propagate` is an MCP tool for agents rather than a local `gitreins propagate` command. It creates or recursively merges `.gitreins/config.yaml` into explicit sibling-repository targets while preserving target overrides.

### 4.3 Diff-Mode Test Selection

When `guards.test_mode: "diff"`, GitReins:

1. Gets staged files via `git diff --cached --name-only --diff-filter=ACM`
2. Checks force-full triggers (`pyproject.toml`, `conftest.py`, `.gitreins/config.yaml`, CI workflows, `Makefile`)
3. Maps source files to test files: `engine/foo.py` → `tests/test_foo.py`, `gitreins_mcp/server.py` → `tests/test_mcp_server.py`
4. If no mapping found, falls back to full suite (safety default)
5. For pytest runners, appends test file paths to the command

### 4.4 Evaluator Caps

Flexible resource limits with fractional tool-call weighting:

```yaml
evaluator:
  max_iterations: 100          # -1 = unlimited
  max_time: "30m"              # 30s, 5m, 2h
  max_input_tokens: "200k"     # 200k, 0.1M, 1.5M
  max_output_tokens: "50k"
  tool_call_weight: 0.1        # each tool call costs 0.1 iterations
```

Legacy combined string also supported: `eval_cap: "100/30m/200k/50k"`.

Cap behavior: checked BEFORE each LLM call (lenient — allows going slightly over), hard-checked for time and tokens. Summary string shows real-time usage: `iterations: 12.3/100, time: 45s/30m, in: 45k/200k, out: 3k/50k`.

### 4.5 Configurable Pipeline Engine

YAML-defined evaluation pipelines with sequential and parallel stages:

```yaml
pipeline:
  stages:
    - id: tier1
      parallel: true
      on: [pre-commit, pre-eval]
      steps:
        - id: secrets
          type: script
          run: "gitleaks detect --source . --no-git"
          on_fail: continue
        - id: lint
          type: script
          run: "ruff check ."
    - id: tier2
      type: ai_eval
      condition: "stage.tier1.any_failed"
      max_iterations: 20
```

Features: conditional execution (`stage.tier1.any_failed or task.has_criteria`), result piping between stages, parallel step execution via ThreadPoolExecutor.

### 4.6 Commit Audit and CVE-Style Severity

The optional Tier 2 `commit_audit` stage validates a proposed commit message and can review the staged diff in CodeRabbit-style `review` or tool-using `agent` mode. Each review finding has a CVE-style numeric score from 1 to 10. GitReins calculates `effective_score = issue.score × review_score_offset`; block-mode reviews reject scores at or above `review_score_threshold`.

### 4.7 PyPI Distribution

```bash
pip install gitreins
```

- Pure Python, no compiled extensions
- Supports Python 3.10, 3.11, 3.12
- Dependencies: `mcp>=1.0.0`, `pyyaml>=6.0`, `requests>=2.28`, `packaging>=21.0`
- Optional dev deps: `pytest>=7.0`, `twine>=5.0`, `build>=1.0`

---

## 5. Scope Boundaries

### What GitReins Does NOT Do

| Out of Scope | Rationale |
|--------------|-----------|
| **Full pull-request review workflow** | GitReins can run a scoped CodeRabbit-style staged-diff commit audit, but it is not a replacement for human PR review, ownership rules, or hosted review workflow. |
| **Deployment** | GitReins operates at the commit boundary. CI/CD pipelines handle deployment. |
| **Monitoring / observability** | No runtime metrics, no log aggregation, no alerting. GitReins is a pre-commit quality gate, not a production monitoring system. |
| **Issue tracking** | Tasks are lightweight YAML entries with criteria. For full issue tracking, use GitHub Issues, Jira, or Linear. |
| **IDE integration** | No VS Code extension, no JetBrains plugin. MCP stdio is the primary agent interface; CLI is the human interface. |
| **Multi-language dead code by default** | Python dead code is built-in. Go/TS dead code requires opt-in Skylos integration. |

---

## 6. Success Metrics

| Metric | Target | Measurement |
|--------|--------|-------------|
| **Guard pass rate** | >95% of commits pass Tier 1 on first run | Pre-commit hook exit code tracking |
| **Evaluator verdict accuracy** | >90% agreement with human review on 50 sampled tasks | Manual audit of evaluator verdicts |
| **Adoption** | 16+ production projects using GitReins | Self-reported via GitHub stars + issue mentions |
| **Install time** | <30 seconds from `pip install` to working pre-commit hook | Manual timing on clean environment |
| **False positive rate** | <5% of guard failures are incorrect | User-reported override frequency |
| **Cross-repo MCP usage** | 3+ repos managed by single MCP server instance | Integration test coverage |

---

## 7. Competitive Landscape

| Tool | What It Does | GitReins Differentiator |
|------|--------------|------------------------|
| **pre-commit hooks** | Framework for running checks before commit | GitReins is a complete product, not a framework — includes evaluator, MCP server, task management, and cross-repo support out of the box |
| **gitleaks** | Secrets detection only | GitReins includes secrets + lint + tests + build + agentic evaluation in one tool |
| **CI linters** (GitHub Actions, GitLab CI) | Run checks in CI pipeline | GitReins runs at commit time — catches issues before they enter the remote, not after |
| **Code review tools** (GitHub PR review, CodeRabbit) | Review code quality post-commit | GitReins judges criteria pre-commit; complementary, not competing |
| **AI agent harnesses** (Axiom, OpenCode) | Agent orchestration and task management | GitReins is the quality gate that sits between the agent and the commit — it integrates with any agent via MCP |

**Key differentiator:** GitReins is the only tool that combines (1) language-agnostic static guards, (2) an agentic LLM evaluator with tool-use capabilities, and (3) git-native integration (pre-commit hook + task storage in `.gitreins/`), all installable via `pip install` in under 30 seconds.

---

## 8. Version & Maturity

**Current version: v0.6.0**

| Attribute | Value |
|-----------|-------|
| **Test suite** | 384 tests across 26 test files |
| **Eval cap tests** | 39 tests including real LLM calls (integration) |
| **Production projects** | 16 repos actively using GitReins |
| **MCP tools** | 10 tools exposed via stdio JSON-RPC 2.0 |
| **Supported languages** | Python, Go, TypeScript (configurable for any) |
| **Python versions** | 3.10, 3.11, 3.12 |
| **License** | MIT |
| **Distribution** | PyPI (`pip install gitreins`) |
| **Repository** | https://github.com/totalwindupflightsystems/gitreins |

### Module Map

| Module | Responsibility |
|--------|--------------|
| `engine/guard_manager.py` | Tier 1 static checks (secrets, lint, tests, diff-mode selection) |
| `engine/evaluator.py` | Agentic LLM evaluator with 9 tools, iterative loop, verdict delivery |
| `engine/eval_cap.py` | Resource caps with fractional tool-call weighting |
| `engine/llm.py` | Multi-provider LLM client (OpenAI-compatible + Anthropic native) with retry |
| `engine/pipeline.py` | Configurable evaluation pipelines with sequential/parallel stages |
| `engine/config.py` | Unified defaults, config.yaml overlay, update checking |
| `engine/dead_code.py` | Python AST detector for unused functions/imports, unreachable code, and empty stubs |
| `engine/lsp.py` | Tier 1 language-server diagnostics over stdio subprocesses |
| `engine/static_analysis.py` | Tier 1 normalized diagnostics from configured language-specific analyzers |
| `engine/commit_audit.py` | Tier 2 commit-message validation and CodeRabbit-style review with scored findings |
| `engine/propagate.py` | Cross-repository guard-config propagation with target-preserving recursive merge |
| `engine/task_manager.py` | YAML-backed task lifecycle (create, start, complete, delete) |
| `gitreins_mcp/server.py` | MCP stdio server exposing 10 tools to primary AI agents |
| `gitreins/cli.py` | Human-facing CLI with install, task, guard, judge, commit commands |

---

## 9. Document Status

| Field | Value |
|-------|-------|
| **Version** | v0.6.0 |
| **Status** | Active — shipping on PyPI |
| **Last updated** | 2026-06-20 |
| **Author** | totalwindupflightsystems <totalwindupflightsystems@gmail.com> |
| **Co-author** | wojons <wojonstech@gmail.com> |
