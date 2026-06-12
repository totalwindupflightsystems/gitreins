# Pi Research Summary

Pi Coding Agent by Mario Zechner ([pi.dev](https://pi.dev)) is a TypeScript monorepo with a minimal coding agent CLI. We analyzed its architecture to inform GitReins' design.

## Architecture Layers

```
pi-ai (LLM API) → pi-agent-core (loop) → pi-coding-agent (CLI) → pi-tui (terminal)
```

## Key Findings

### 4 Built-in Tools
`read`, `write`, `edit`, `bash`. System prompt under 1,000 tokens. Maximizes context window for actual code.

### Agent Loop
Loops until the model stops calling tools. No max-steps, no iteration limits. Extension system for custom tools.

### 4 Modes
Interactive (TUI), Print, JSON stream, RPC (JSONL stdio). Embeddable in other systems.

### Binary Compilation
Can compile to standalone binary via `bun build --compile`. Removes Node.js runtime requirement. However, Bun 1.3.12 has a macOS SIGKILL regression that makes this unreliable for cross-platform embedding.

## What We Borrowed

- **Minimal system prompt** — Evaluator prompt should be equally lean, maximizing context for evidence
- **Tool-first design** — The agent's power comes from tools, not prompt engineering
- **Clean layer separation** — LLM interface → evaluator loop → tools → verdict

## Why Not Pi for the Evaluator

Pi is a **general coding agent** optimized for creative code generation. The evaluator needs a **focused judgment agent** that:
- Never writes code (only reads and judges)
- Always produces a structured verdict
- Runs deterministically (always check every criterion)
- Has no user-facing TUI or session management

Different purpose, different constraints. The evaluator is 100 lines of Python, not 28K stars of TypeScript.

## Status
Analysis complete — May 2026. Decision documented in [build-vs-borrow.md](build-vs-borrow.md).
