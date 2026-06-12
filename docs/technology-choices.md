# Technology Choices

| Choice | Decision | Rationale |
|---|---|---|
| Language | **Python 3.10+** | Ubuntu 22.04 LTS standard; mcp SDK requirement |
| Dependencies | **mcp, pyyaml, requests** | Official MCP SDK + YAML + HTTP — 3 packages |
| MCP Client | mcp SDK (official) | Handles JSON-RPC, connection lifecycle, tool discovery |
| LLM API | Direct HTTP | Simple, no SDK overhead (OpenAI/Anthropic) |
| Evaluator Model | **Haiku / GPT-4o-mini** | Fast (<2s), cheap (~$0.001/check) |
| MCP Transport | stdio | Standard, works everywhere |
| Git Backend | subprocess (git CLI) | Most reliable, no libgit2 dependency |
| Config Format | YAML | Human-readable, widely supported |
| Task Storage | YAML on gitreins branch | Version-controlled, cloneable |
| Distribution | gitreins/ in template repo | Clone → install → works |

## Rationale Notes

### Why Python 3.10+?
Ubuntu 22.04 LTS ships Python 3.10 by default. No need for deadsnakes PPA or pyenv. The official `mcp` SDK requires 3.10+.

### Why only 3 dependencies?
Every dependency is a supply chain risk. `mcp` (official SDK), `pyyaml` (config), `requests` (HTTP) — all are mature, well-maintained, and have minimal transitive deps.

### Why direct HTTP with no LLM SDK?
OpenAI and Anthropic APIs are simple enough that `requests` is sufficient. No need for `openai` or `anthropic` packages with their version churn.

### Why Haiku / GPT-4o-mini?
The evaluator needs to be fast (<2s) and cheap (~$0.001/check). These small models are perfectly capable of reading code, checking criteria, and making binary judgments. The creative heavy lifting is done by the primary agent (Claude, GPT-4, etc.).

### Why git CLI via subprocess?
Libgit2 bindings (pygit2, GitPython) add significant dependency weight and have subtle behavioral differences from the git CLI. `git` is guaranteed to be present (it's the whole point of the harness).
