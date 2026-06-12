# Implementation Plan

## Phases

### Phase 0: Template & Bootstrap (Day 1)
gitreins-template repo, install/uninstall scripts, hook stubs.
Clone → active in 10 seconds.

### Phase 1: Static Guards (Day 1–2)
Secrets scanning, lint, staged tests. Tier 1 enforcement.
No LLM dependency. Under 100ms.

### Phase 2: Task Manager (Day 2–3)
Task lifecycle without evaluation. Create, start, complete (checkbox), status, nesting. YAML-backed, version-controlled.

### Phase 3: Agentic Evaluator (Day 3–5) ⚡ CRITICAL PATH
The core innovation. Agentic loop with 7 tools, LLM judge, verdict system.
Python 3.10+, mcp SDK, ~3s per evaluation.

### Phase 4: MCP Server (Day 5–6)
Primary agent integration. stdio transport, tool registration.
Works with any MCP-compatible agent.

### Phase 5: CLI (Day 6–7)
Human-usable command line: `gitreins task`, `gitreins commit`, `gitreins guard`.

### Phase 6: gitreins Branch Config (Day 7–8)
Config, tasks, prompts, history on dedicated branch.
Version-controlled, cloneable, auditable.

### Phase 7: Advanced Features (Week 3+)
- Deep Review (Tier 3) — full architecture/code review
- Multi-agent collaboration
- π-learning from past evaluations
- IDE integrations (VS Code, JetBrains)

## Dependency Chain

```
Phase 0: Template + Bootstrap
    ↓
Phase 1: Static Guards ──────────────────┐
    ↓                                     │
Phase 2: Task Manager                     │
    ↓                                     │
Phase 3: Agentic Evaluator ←─────────────┘  ← CRITICAL PATH
    ↓
Phase 4: MCP Server
    ↓
Phase 5: CLI
    ↓
Phase 6: gitreins Branch Config
    ↓
Phase 7: Advanced Features
```

## Success Criteria

1. **Instant Activation** — Clone → `./gitreins/install` → hooks active. Under 10 seconds.
2. **Secrets Caught Instantly** — Tier 1 static guard blocks secrets at pre-commit. Under 100ms.
3. **Fast Evaluation** — Verdict returned <3s for typical tasks. Haiku/GPT-4o-mini.
4. **Agent Workflow Works** — Create task → complete items → commit. End-to-end with any MCP agent.
5. **Zero Infrastructure** — Everything lives in the repo. No servers, no CI, no external services beyond LLM API.
6. **Universal Runtime** — Python 3.10+ — works on any system with git + Python: Linux, macOS, WSL, CI runners.
