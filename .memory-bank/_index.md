# GitReins — Project Memory Bank

> **Institutional memory that survives Axiom removal.** This directory
> contains the project context, decisions, and lessons learned that
> are not in the code itself. Read `_index.md` first.

**Status:** All 12 work items (GR-001 → GR-012) complete as of 2026-06-15.
**Codebase:** ~/gitreins-poc/ — 7 engine modules, MCP server, CLI, 322 tests passing.
**Source-of-truth priority when in conflict:**
  1. `engine/*.py` (the actual code)
  2. `.hermes/acceptance-criteria.md` (what passed)
  3. `docs/*.md` (the design, possibly stale)
  4. this memory bank (institutional context)

---

## Index

| File | Purpose | Audience |
|------|---------|----------|
| `architecture.md` | How the system is wired together — evaluator loop, guard pipeline, task lifecycle, MCP server, LLM client, .gitreins/ storage | Anyone touching the engine |
| `adr/adr-001-gitreins-dir-storage.md` | Why `.gitreins/` directory beats a dedicated `gitreins` branch | Architects, storage decisions |
| `adr/adr-002-openai-compatible-llm.md` | Why the LLM client is one OpenAI-compatible HTTP layer (not per-provider SDKs) | Anyone adding a provider |
| `adr/adr-003-guard-pipeline.md` | Why the guard order is secrets → lint → tests → dead_code → skylos, and why Tier 1 → Tier 2 | Anyone modifying guards |
| `adr/adr-004-evaluator-agent-loop.md` | Why the evaluator is an LLM agent with 9 tools, not static rules | Anyone touching the evaluator |
| `findings/finding-001-deepseek-provider-quirks.md` | DeepSeek-specific behavior (default provider, quirks, gotchas) | Anyone using DeepSeek |
| `findings/finding-002-minimax-reasoning-mode.md` | MiniMax M3 reasoning/content split, max_tokens requirement | Anyone using MiniMax M3 |
| `findings/finding-003-skylos-integration.md` | How Skylos was wired in, what it catches, opt-in status | Anyone enabling Skylos |
| `findings/finding-004-precommit-guard-false-positives.md` | The `--no-verify` incident, false positive pattern, the fix | Anyone debugging precommit |
| `provider-integration.md` | How to wire any OpenAI-compatible provider — 3 env vars, base URL table, troubleshooting | Anyone configuring an LLM |
| `work-items/STATUS.md` | All 12 work items (GR-001 → GR-012), phases, completion state | Project manager, onboarding |

---

## Quick orientation

**What is GitReins?**
A git-native agent co-harness. It lives inside your repo as a `.gitreins/`
directory, exposes MCP tools for task lifecycle and evaluation, runs static
guards at pre-commit time, and uses an agentic LLM loop to judge whether
completed tasks actually meet their criteria.

**Three deps.** `mcp`, `pyyaml`, `requests`. Python 3.10+. See
`docs/technology-choices.md` for the rationale.

**Read order for a new contributor:**
  1. `architecture.md` (this dir) — how it fits together
  2. `docs/evaluator-loop.md` — the 9 evaluator tools in detail
  3. `docs/architecture.md` — the system diagram
  4. `work-items/STATUS.md` — what was built when, why
  5. ADRs as needed

**Read order when debugging:**
  1. `architecture.md` — find the relevant module
  2. `provider-integration.md` — if the LLM is misbehaving
  3. `findings/` — known quirks for the provider you're using
  4. ADRs — if the question is "why is it this way"

---

## Source files for every claim in this bank

Where the claim came from (real file path → line range):

| Claim | Source |
|-------|--------|
| 7 engine modules, 3 deps | `README.md:49-56`, `requirements.txt` |
| 9 MCP tools, JSON-RPC 2.0 | `gitreins_mcp/server.py:34-44` |
| 7-9 evaluator tools | `engine/evaluator.py:64-177` (9 tools defined, 2 are sk/dead-code) |
| Tier 1 → Tier 2 flow | `engine/judge.py:32-94` |
| `.gitreins/` paths | `engine/task_manager.py:41-43`, `engine/pipeline.py:383-386` |
| Config keys | `.gitreins/config.yaml` |
| Work item completion evidence | `.hermes/acceptance-criteria.md` (AC-001 through AC-020) |
| Git log per GR | `git log --all --oneline` in repo |
| GR-010, GR-011 prior docs | `git show 72aeef0:docs/adr/*.md` (in git history) |

**Honest gap notes:**
- `plan.yaml` files referenced in `axiom:trace` comments live in Axiom
  (the external orchestrator), not in this repo. So we infer the
  scope of GR-004, GR-005, GR-006 from the git timeline and current
  test/code state — not from a plan file.
- The original `docs/adr/` directory existed at commit `72aeef0` but
  was removed later. The ADRs in this memory bank restore the
  substantive content from that history.
- The `.gitreins-hooks/` and `.skylos/` directories exist but are
  empty (artifacts from the .gitreins-hooks install path and
  Skylos cache respectively).
