# GitReins (PoC)

**Git-Native Agent Co-Harness — Proof of Concept**

[![CI](https://github.com/totalwindupflightsystems/gitreins/actions/workflows/ci.yml/badge.svg)](https://github.com/totalwindupflightsystems/gitreins/actions/workflows/ci.yml)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![version](https://img.shields.io/badge/version-0.1.0-blue)](https://github.com/totalwindupflightsystems/gitreins/releases)

![GitReins Banner](assets/banner-dark.jpg)

GitReins lives inside your git repository as a co-harness. It provides MCP tools for task lifecycle management, an agentic evaluator that judges code completeness against task definitions, and git hooks that ensure nothing bypasses the quality gates.

> ✅ **Proof of Concept — Implemented (v0.1.0)** — All engine modules, MCP server, CLI, and git hooks are built and working. 221 tests pass.

## How It Works

1. **Create tasks** — Define criteria via CLI or MCP tools
2. **Work with your AI agent** — Pi, Claude, Hermes, or Codex does code generation
3. **Complete tasks** — Agent calls `task.complete`
4. **Automatic evaluation** — Tier 1 static guards (secrets, lint, tests) + Tier 2 agentic evaluator
5. **Commit through harness** — `commit` tool runs guards, blocks if checks fail

The evaluator is an agentic loop: it reads files, runs tests, searches patterns, and delivers a structured verdict with per-criterion PASS/FAIL. No single-shot LLM judgment.

## Architecture & Docs

| Document | Purpose |
|----------|---------|
| [Full Architecture](docs/architecture.md) | System design and data flow |
| [Component Map](docs/component-map.md) | Module inventory with paths and line counts |
| [Agentic Evaluator Design](docs/evaluator-loop.md) | How the 7-tool agentic loop works |
| [Sandbox](docs/sandbox.md) | Evaluator scratch space (in-memory, with filesystem plans) |
| [Implementation Plan](docs/implementation-plan.md) | Phase history |

Full reverse-engineered specs are in `specs/` — one per component, with realized-by links to actual code files.

## Status

**Phase: Fully Implemented (v0.1.0)** — All seven engine modules, MCP server (9 tools), CLI (5 top-level commands: task, guard, judge, commit, mcp-server), git hooks, and install script are built. See [Component Map](docs/component-map.md) for current state.

## Quick Start

```bash
cd gitreins-poc
./gitreins/install                    # Activate hooks in <10 seconds

# Create a task and evaluate it
python3 gitreins/cli.py task create demo "Demo task" \
  "File exists" "Has tests" "No secrets"

# Start the MCP server for your AI agent
python3 gitreins_mcp/server.py
```

## Demos
Three demo projects are included showing GitReins in action:
| Project | Type | What it tests |
|---------|------|---------------|
| demo-slugify/ | Single-file | URL slug generator — basic criteria verification |
| demo-calc/ | Multi-file CLI | Calculator with operations, parser, CLI — 13 pytest tests |
| demo-string-utils/ | Single-file | String utilities with intentional palindrome bug — FAIL→FIX→PASS cycle |

Run any demo:

## Tech Stack

- **Language:** Python 3.10+
- **Dependencies:** mcp, pyyaml, requests (3 packages)
- **MCP Transport:** stdio
- **Config:** YAML in `.gitreins/` directory
- **Evaluator Model:** Haiku / GPT-4o-mini (<2s, ~$0.001/check)

<!-- axiom:trace work_item=GR-012 spec=specs/01-Architecture.md,specs/09-CLI.md,specs/10-Install-Bootstrap.md,specs/11-Configuration.md plan=.memory-bank/work-items/GR-012/plan.yaml -->
