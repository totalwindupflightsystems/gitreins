# GitReins Architecture

**IMPLEMENTED** — this document describes the architecture as built. For the exact
release you are running see `gitreins --version` (the MCP handshake's
`serverInfo.version` reports the same value; DF-GITREINS-POC-5 replaced a frozen
"v0.1.0" banner that disagreed with the shipped release).

## System Overview

```
PRIMARY AI AGENT (Pi / Claude / Hermes / Codex)
       │  MCP stdio
       ▼
MCP SERVER (task.* commit() guard.run() judge.evaluate())
       │
       ▼
┌──────────────────────────────────┐
│      GITREINS ENGINE             │
│                                  │
│  Task Manager    Agentic Eval    │
│  Guard Manager   Judge Orchestr  │
└──────────────────────────────────┘
       │
       ▼
Git Hooks (.git/hooks/)
       │
       ▼
Git Repository (main + .gitreins/ directory)
```

## Core Components

### 1. Primary Agent (External)
Any MCP-compatible coding agent (Pi, Claude Code, Hermes, Codex CLI). Interacts with GitReins MCP tools. Has no direct git access — commit must go through the harness.

### 2. MCP Server
stdio transport (`gitreins_mcp/server.py`) exposing 13 tools:
- `configure` — hot-reload LLM config at runtime
- `task.create`, `task.start`, `task.complete` — task lifecycle
- `task.list`, `task.get`, `task.delete` — task queries
- `commit` — the only path to a git commit (runs guards, rejects if fails)
- `guard.run` — run Tier 1 static guards
- `judge.evaluate` — run full evaluation pipeline on a task (async job by default)
- `judge.status` — poll a background evaluation job
- `context.resolve` — resolve a question against the repo's code (Jev resolution gate)
- `propagate` — propagate guard config to sibling repos

### 3. Task Manager
Manages TODO items as structured tasks with nesting and dependencies (`engine/task_manager.py`). Tasks stored in `.gitreins/tasks.yaml`. Tracks state, progress, and completion criteria. The TODO items ARE the guardrails.

### 4. Agentic Evaluator
An LLM-powered agentic loop with 7 tools (`engine/evaluator.py`):
1. `read_file(path, offset?, limit?)` — Read any file in the working tree
2. `run_command(cmd)` — Run a shell command (tests, lint, build)
3. `search_pattern(regex, file_glob?)` — Grep the codebase for a pattern
4. `read_diff()` — Show staged and unstaged changes
5. `get_task_item(id)` — Read a task's full definition and criteria
6. `sandbox_write(key, content)` — Write to evaluator scratch space
7. `sandbox_read(key)` — Read from evaluator scratch space

Iterates until it has enough evidence to deliver a verdict.

### 5. Guard Manager
Static checks (`engine/guard_manager.py`): secrets (gitleaks or built-in pattern scanner), lint (ruff/flake8 — plus `ruff format --check` over the same graded scope, GR-GAP-063), staged tests (pytest). Runs Tier 1 — no LLM dependency. All checks are optional and configurable via `.gitreins/config.yaml`. Both scanners skip GitReins' own `.gitreins/**` state (config, guard run logs, verdict history, disposable bookkeeping): the judge never grades the harness itself, and the exclusion is named in the Tier 1 secrets step output.

Failure evidence is named rather than counted: the console tests line prints the **first failing test id** parsed from the pytest output (`FAIL (<id> [first failing id]; N failure(s))`, `engine/types.py:parse_first_failing_test`), and the secrets line prints every scanner that ran with its own outcome (`clean (gitleaks + builtin cross-check)` / `FAIL (builtin cross-check: 2 findings; gitleaks: clean)`), because a finding raised only by the low-entropy built-in cross-check is not the same problem as one gitleaks reported. Both facts are also written to the run log's `diagnostics:` block, and the judge's Tier 1 secrets step echoes the same scanner attribution into its step output.

### 6. Judge Orchestrator
Runs the full pipeline: Tier 1 (static guards) → Tier 2 (agentic evaluator). Compiles verdict from all tiers (`engine/judge.py`).

### 7. Git Hooks
Thin relay in `.git/hooks/` that calls the engine. Two-layer enforcement: MCP tools (friendly path) + git hooks (hard gate).

## Data Flow

```
Agent completes items → Tier 1 (Static Guards) → Tier 2 (Agentic Evaluator) → git commit
                                                                                     ↑
                          Bypass Attempt (git commit directly) → pre-commit hook → REJECTED
```

## .gitreins/ Directory

A checked-in directory at the repo root (`.gitreins/`) stores:
- `config.yaml` — engine configuration
- `tasks.yaml` — task definitions and state
- `guardrails/` — guard rules (optional)
- `prompts/` — evaluator prompts (optional)
- `history/` — evaluation records (optional, auto-committed after verdict)

Everything is version-controlled, auditable, and cloneable.
