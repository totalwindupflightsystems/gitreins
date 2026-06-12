# GitReins (PoC)

**Git-Native Agent Co-Harness — Proof of Concept**

GitReins lives inside your git repository as a co-harness. It provides MCP tools for task lifecycle management, an agentic evaluator that judges code completeness against task definitions, and git hooks that ensure nothing bypasses the quality gates.

> ⚠️ **Proof of Concept** — This repo captures the architecture, design decisions, and implementation plan. Code implementation is pending.

## How It Works

- **Primary Agent** — Any AI coding agent (Pi, Claude, Hermes, Codex) does the creative work
- **GitReins Co-Harness** — Manages task lifecycle, enforces quality, validates completeness
- **Agentic Evaluator** — Not a single LLM call — an agentic loop with 7 tools that reads, tests, searches, and verifies every task criterion

## Architecture

- [Full Architecture](docs/architecture.md)
- [Build vs Borrow — Runtime Decision](docs/build-vs-borrow.md)
- [Agentic Evaluator Design](docs/evaluator-loop.md)
- [Sandbox: gitreins Branch Persistence](docs/sandbox.md)
- [Technology Choices](docs/technology-choices.md)
- [Component Map](docs/component-map.md)
- [Implementation Plan](docs/implementation-plan.md)
- [Pi Research Summary](docs/pi-research.md)

## Status

**Phase: Architecture & Design** — All design docs complete. Implementation tracked in [implementation-plan.md](docs/implementation-plan.md).

## Quick Start (Future)

```bash
git clone https://gitlab.readydedis.com/totalwindup/gitreins-poc.git
cd your-project
./gitreins/install    # Activates hooks in 10 seconds
```

## Tech Stack

- **Language:** Python 3.10+
- **Dependencies:** mcp, pyyaml, requests (3 packages)
- **MCP Transport:** stdio
- **Config:** YAML on `gitreins` branch
- **Evaluator Model:** Haiku / GPT-4o-mini (<2s, ~$0.001/check)
