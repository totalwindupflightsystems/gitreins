# GitReins Architecture

**IMPLEMENTED (v0.1.0)**

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
stdio transport (`gitreins_mcp/server.py`) exposing 9 tools:
- `task.create`, `task.start`, `task.complete` — task lifecycle
- `task.list`, `task.get`, `task.delete` — task queries
- `commit` — the only path to a git commit (runs guards, rejects if fails)
- `guard.run` — run Tier 1 static guards
- `judge.evaluate` — run full evaluation pipeline on a task

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
Static checks (`engine/guard_manager.py`): secrets (gitleaks or built-in pattern scanner), lint (ruff/flake8), staged tests (pytest). Runs Tier 1 — no LLM dependency. All checks are optional and configurable via `.gitreins/config.yaml`.

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
