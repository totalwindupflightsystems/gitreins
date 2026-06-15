# Component Map

| Component | Path | Lines | Status | Responsibility |
|---|---|---|---|---|
| Task Manager | `engine/task_manager.py` | 148 | Implemented ✅ | Task lifecycle: create, start, complete, status, nesting, dependencies |
| Agentic Evaluator | `engine/evaluator.py` | 569 | Implemented ✅ | Agentic loop + 7 evaluation tools + verdict parsing |
| Guard Manager | `engine/guard_manager.py` | 241 | Implemented ✅ | Static checks: secrets (gitleaks), lint, staged tests |
| Judge Orchestrator | `engine/judge.py` | 134 | Implemented ✅ | Tier 1 → Tier 2 pipeline, verdict compilation |
| LLM Interface | `engine/llm.py` | 298 | Implemented ✅ | Multi-provider chat completions (OpenAI, Anthropic) |
| Pipeline Engine | `engine/pipeline.py` | 428 | Implemented ✅ | Configurable multi-stage evaluation pipelines |
| MCP Server | `gitreins_mcp/server.py` | 406 | Implemented ✅ | MCP stdio transport, tool registration, primary agent interface |
| CLI | `gitreins/cli.py` | 218 | Implemented ✅ | CLI entry point for gitreins commands |
| Installer | `gitreins/install` | 76 | Implemented ✅ | Symlinks hooks into .git/hooks/ — one command activation |
| Config | `.gitreins/` directory | — | Implemented ✅ | config.yaml, tasks.yaml, history/ |

## Specs

| Spec | Path | Status |
|---|---|---|
| Architecture | `specs/01-Architecture.md` | Not yet created |
| Agentic Evaluator | `specs/03-Agentic-Evaluator.md` | Not yet created |
| Pipeline Engine | `specs/06-Pipeline-Engine.md` | Not yet created |
| MCP Server | `specs/08-MCP-Server.md` | Not yet created |
| Configuration | `specs/11-Configuration.md` | Not yet created |

## Dependency Chain

```
LLM Interface → Agentic Evaluator ← Guard Manager
Task Manager → Agentic Evaluator
Pipeline Engine → Judge Orchestrator
Judge Orchestrator → Guard Manager + Agentic Evaluator
MCP Server → Task Manager + Judge Orchestrator
CLI → Judge Orchestrator + Guard Manager
```

Note: Test execution is handled inside `GuardManager._check_tests()`; sandboxing is via an in-memory dict inside `AgenticEvaluator._sandbox`. Neither is a separate module.

## Build Order

```
Phase 0: Template + Bootstrap
Phase 1: Static Guards
Phase 2: Task Manager
Phase 3: Agentic Evaluator    ← CRITICAL PATH
Phase 4: MCP Server
Phase 5: CLI
Phase 6: Config
Phase 7: Advanced Features
```

<!-- axiom:trace work_item=GR-008 spec=specs/01-Architecture.md impl=docs/component-map.md -->
