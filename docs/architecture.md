# GitReins Architecture

## System Overview

```
PRIMARY AI AGENT (Pi / Claude / Hermes / Codex)
       │  MCP stdio
       ▼
MCP SERVER (task.* commit() guard.*)
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
Git Repository (main + gitreins branch)
```

## Core Components

### 1. Primary Agent (External)
Any MCP-compatible coding agent (Pi, Claude Code, Hermes, Codex CLI). Interacts with GitReins MCP tools. Has no direct git access — commit must go through the harness.

### 2. MCP Server
stdio transport exposing:
- `task.create`, `task.start`, `task.complete` — task lifecycle
- `commit()` — the only path to a git commit
- `guard.*` — guard management tools

### 3. Task Manager
Manages TODO items as structured tasks with nesting and dependencies. Tracks state, progress, and completion criteria. The TODO items ARE the guardrails.

### 4. Agentic Evaluator
An LLM-powered agentic loop with 7 tools. Reads files, runs tests, searches for patterns, calls external MCP servers. Iterates until it has enough evidence to deliver a verdict.

### 5. Guard Manager
Static checks: secrets (gitleaks), lint, staged tests. Runs Tier 1 — no LLM dependency.

### 6. Judge Orchestrator
Runs the full pipeline: Tier 1 (static guards) → Tier 2 (agentic evaluator). Compiles verdict from all tiers.

### 7. Git Hooks
Thin relay in `.git/hooks/` that calls the engine. Two-layer enforcement: MCP tools (friendly path) + git hooks (hard gate).

## Data Flow

```
Agent completes items → Tier 1 (Static Guards) → Tier 2 (Agentic Evaluator) → git commit
                                                                                    ↑
                         Bypass Attempt (git commit directly) → pre-commit hook → REJECTED
```

## gitreins Branch

A dedicated branch (`gitreins`) stores:
- `config.yaml` — engine configuration
- `guardrails/` — guard rules
- `prompts/` — evaluator prompts
- `tasks/` — task definitions
- `history/` — evaluation records (auto-committed after verdict)

Everything is version-controlled, auditable, and cloneable.
