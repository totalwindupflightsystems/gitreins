# Agentic Evaluator Design

The evaluator is **not** a single LLM call. It's an agentic loop — the LLM iterates, calling tools and incorporating results, until it has enough evidence to deliver a verdict.

## Loop Architecture

```
LOAD CONTEXT → LLM CALL → TOOL CALL? → (yes) Execute → Back to LLM
                                    → (no) → VERDICT: COMPLETE / INCOMPLETE
```

The loop terminates when the LLM issues no more tool calls — it has decided it has sufficient evidence.

## Evaluation Tools (7)

### Repo Inspection (5 tools)

| Tool | Purpose |
|---|---|
| `read_file(path)` | Read any file in the working tree |
| `run_command(cmd)` | Run tests, linters, build commands |
| `search_pattern(regex)` | Find patterns across the codebase |
| `read_diff(filter?)` | Inspect staged changes |
| `get_task_item(id)` | Read task definition and criteria |

### External & Sandbox (2 tools)

| Tool | Purpose |
|---|---|
| `mcp_call(server, tool)` ✦ | Call external MCP servers (Jira, docs, APIs) — opt-in via allowlist |
| `sandbox_write/read()` ◇ | Virtual FS scratch space — isolated from working tree |

✦ = Pluggable via `.gitreins.yaml` mcp_allowlist  
◇ = Virtual FS — created per evaluation, cleaned after verdict

## Verdict Output

```json
{
  "verdict": "INCOMPLETE",
  "items": [
    {"criterion": "error-handling", "status": "FAIL", "detail": "No 401 for invalid credentials — routes.py:65 missing"},
    {"criterion": "tests", "status": "FAIL", "detail": "Missing 3 required tests — test_login.py only has happy-path"},
    {"criterion": "login-endpoint", "status": "PASS", "detail": "POST /login confirmed at routes.py:42-68"}
  ],
  "gaps_to_fix": 2
}
```

## Tier System

```
Tier 1: Static Guards (no LLM)
  ├── gitleaks (secrets)
  ├── lint
  └── staged tests
      ↓ PASS
Tier 2: Agentic Evaluator (LLM)
  ├── reads code
  ├── runs tests
  ├── searches patterns
  └── delivers verdict
      ↓ PASS
git commit ✓
```

Tier 1 runs first because it's fast and free. Tier 2 only fires if Tier 1 passes.
