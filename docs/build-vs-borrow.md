# Build vs Borrow — Agent Runtime Decision

## Context

We researched three approaches for the evaluator's agentic runtime. The evaluator needs to run an LLM-powered loop that reads files, runs tests, searches for patterns, and delivers a verdict. It must be embeddable, minimal, and reliable.

## Options Considered

### Option A: Pi (Compiled Binary) — REJECTED

Pi Coding Agent (28K stars) has a battle-tested agent loop and can compile to a standalone binary via `bun build --compile`.

**Pros:**
- 28K stars, production-tested agent loop
- MCP via extensions
- Can compile to standalone binary

**Cons:**
- **Bun --compile has macOS SIGKILL regression** (Bun 1.3.12) — not reliable for embedded evaluator
- ~60MB binary (embeds full JS runtime)
- Requires Bun to BUILD (users need Bun)
- Carries full coding agent (TUI, themes, sessions) — massive overkill
- Cross-platform reliability concern for a component that must NEVER fail

### Option B: mcp SDK + Custom Loop — **SELECTED ✓**

~100 lines of Python using the official `mcp` SDK.

**Pros:**
- ~100 line custom agentic loop — fully controlled
- Official mcp Python SDK for MCP protocol handling
- `requests` for LLM API calls
- **3 pip dependencies:** mcp, pyyaml, requests
- Python 3.10+ (Ubuntu 22.04 LTS standard)
- Full control, minimal dependencies
- No framework lifecycle — embed anywhere
- Simple to maintain, easy to debug
- The complexity (JSON-RPC, tool discovery, connection lifecycle) is handled by the official SDK

**Cons:**
- Requires Python runtime (acceptable — every dev machine has it)
- Must write the ~10 line loop ourselves (trivial)

### Option C: mcp-agent / smolagents — REJECTED

Full-featured agent frameworks with MCP support.

**Pros:**
- mcp-agent: 8.3K stars, Apache 2.0
- smolagents: 27K stars, HuggingFace-backed
- MCP-native, evaluator pattern could fit

**Cons:**
- Heavy async framework (MCPApp lifecycle) — not designed for embedding
- smolagents uses code-gen agent paradigm (wrong fit for evaluation)
- Significant dependency weight
- Designed for standalone apps, not as embedded components
- Overkill for a ~100 line evaluator loop

## Decision

**Option B: mcp SDK + Custom Loop**

The agentic loop itself is trivial — the complexity is in MCP protocol handling, which the official `mcp` Python SDK solves. We get full control with 3 dependencies and Python 3.10+.

```python
while True:
    response = llm.chat(messages, tools)
    if not response.tool_calls:
        return response.content  # Verdict — agent decided it's done
    for call in response.tool_calls:
        result = execute_tool(call)  # read_file, mcp_call, sandbox_write...
        messages.append(tool_result(result))
    # Loop — LLM gets results, may call more tools
```

## Status

Accepted — May 2026
