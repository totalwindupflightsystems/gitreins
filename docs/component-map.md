# Component Map

| Component | Path | Lines | Responsibility |
|---|---|---|---|
| Task Manager | `engine/task_manager.py` | ~200 | Task lifecycle: create, start, complete, status, nesting, dependencies |
| Agentic Evaluator | `engine/evaluator.py` | ~300 | Agentic loop + 7 evaluation tools + verdict parsing |
| Guard Manager | `engine/guard_manager.py` | ~150 | Static checks: secrets (gitleaks), lint, staged tests |
| Judge Orchestrator | `engine/judge.py` | ~100 | Tier 1 → Tier 2 pipeline, verdict compilation |
| LLM Interface | `engine/llm.py` | ~100 | Multi-provider API (OpenAI, Anthropic), model selection |
| Test Runner | `engine/test_runner.py` | ~80 | Run registered tests, capture output, report results |
| MCP Server | `mcp/server.py` | ~150 | MCP stdio transport, tool registration, primary agent interface |
| Git Hooks | `hooks/pre-commit` | ~20 | Thin relay — calls engine for Tier 1 + Tier 2 checks |
| Bootstrap | `install` | ~30 | Symlinks hooks into .git/hooks/ — one command activation |
| Config | `gitreins` branch | — | config.yaml, guardrails/, prompts/, tasks/, history/ |

## Dependency Chain

```
Task Manager ──→ Agentic Evaluator ←── Guard Manager
                     │
                     ├── LLM Interface
                     ├── Test Runner
                     └── Sandbox
                     
MCP Server ──→ Task Manager + Judge Orchestrator
Judge Orchestrator ──→ Guard Manager + Agentic Evaluator
Git Hooks ──→ Judge Orchestrator
```

## Build Order

```
Phase 0: Template + Bootstrap
Phase 1: Static Guards
Phase 2: Task Manager
Phase 3: Agentic Evaluator    ← CRITICAL PATH
Phase 4: MCP Server
Phase 5: CLI
Phase 6: gitreins Branch Config
Phase 7: Advanced Features
```
