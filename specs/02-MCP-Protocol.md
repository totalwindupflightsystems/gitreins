# 02-MCP-Protocol.md — MCP Protocol Specification

Document Status: Draft v0.1 — Specification only, no code.

---

## 1. Mission

Define the Model Context Protocol (MCP) interface exposed by GitReins' stdio JSON-RPC 2.0 server. Primary AI coding agents (Pi, Claude, Hermes, Codex) connect via stdio and use these 15 tools (count generated from the live `tools/list` surface — see §8) to manage tasks, run guards, evaluate work, commit code, and propagate guard configuration through the harness. This specification covers the wire protocol, tool catalog, cross-repository semantics, evaluator caps, error taxonomy, server lifecycle, and security model.

---

## 2. Scope

### In scope (v1)

- JSON-RPC 2.0 framing over line-delimited stdio
- Multi-line JSON buffering with brace-count parsing
- Initialize → tools/list → tools/call lifecycle
- 15 exposed tools: configure, repo.init, task.create, task.start, task.complete, task.list, task.get, task.delete, commit, guard.run, judge.evaluate, judge.status, quality.status, propagate, context.resolve (count derived from the live `tools/list` response — 10 schemas carry a `workdir` property; `configure`, `judge.status`, `propagate`, `context.resolve`, and `commit` do not)
- Cross-repo workdir resolution
- Evaluator cap priority chain (individual params > eval_cap string > config.yaml)
- JSON-RPC standard errors + domain-specific errors
- Server startup, stdio loop, and SIGTERM shutdown
- Security model (local stdio, field validation, tool name validation)

### Out of scope (v1)

- Transport other than stdio (HTTP, WebSocket, SSE)
- Authentication or authorization layers
- Streaming responses (all responses are single JSON-RPC objects)
- MCP resources/prompts (tools-only server)
- Batching multiple JSON-RPC requests in one message
- Server-sent notifications to the client

---

## 3. Inputs

### 3.1 Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `GITREINS_LLM_API_KEY` | No | `""` | API key for LLM evaluation. If absent, `task.complete` skips evaluation. |
| `GITREINS_WORKDIR` | No | `"."` | Server default working directory. Overridden by per-tool `workdir` param. |

### 3.2 Command-Line Arguments

```
python -m gitreins_mcp.server [workdir]
```

| Arg | Position | Default | Description |
|-----|----------|---------|-------------|
| `workdir` | 1 | `"."` | Server default working directory. Special value `"stdio"` is treated as `"."` (Hermes MCP compatibility). |

### 3.3 JSON-RPC Request Schema

```json
{
  "jsonrpc": "2.0",
  "id": <number|string|null>,
  "method": "tools/call",
  "params": {
    "name": "task.create",
    "arguments": { ... }
  }
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `jsonrpc` | string | Yes | Must be exactly `"2.0"`. |
| `id` | number \| string \| null | Yes for requests, absent for notifications | Request correlation ID. |
| `method` | string | Yes | One of: `initialize`, `tools/list`, `tools/call`, `notifications/initialized`. |
| `params` | object | No | Method-specific parameters. For `tools/call`, contains `name` and `arguments`. |

---

## 4. Operating Contract

- **NEVER** respond to `notifications/initialized` — it is a notification, not a request.
- **ALWAYS** validate `jsonrpc: "2.0"` before processing any request. Reject with `-32600` if missing or wrong.
- **ALWAYS** return tool results as JSON text inside a single `content[0].text` MCP result block.
- **NEVER** expose raw Python tracebacks in JSON-RPC error messages. Log internally, return sanitized message.
- **ALWAYS** check for in-progress tasks before `commit`. Reject commit if any tasks are in-progress.
- **NEVER** authenticate or authorize — stdio is local-only by design.
- **ALWAYS** buffer multi-line JSON until brace balance reaches zero before parsing.

---

## 5. Assumptions

- The client and server run on the same host. stdio is the only transport.
- The client speaks proper JSON-RPC 2.0 and MCP `initialize`-handshake revisions — the server implements `2025-11-25`, `2025-06-18`, `2025-03-26` and `2024-11-05`, echoes a supported request and answers with its newest one otherwise.
- The server workdir contains a valid git repository with `.gitreins/config.yaml` (optional but recommended).
- Task IDs are unique within a single repository's task store. Cross-repo collisions are allowed.
- The LLM client (`GITREINS_LLM_API_KEY`) is optional. Evaluation features degrade gracefully when absent.
- Guard and judge configurations are loaded from `<workdir>/.gitreins/config.yaml` on each call.

---

## 6. Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                      AI Agent Client                         │
│  (Pi / Claude / Hermes / Codex)                              │
└──────────────────────┬──────────────────────────────────────┘
                       │ stdin / stdout (line-delimited JSON)
                       ▼
┌─────────────────────────────────────────────────────────────┐
│              GitReinsMCPServer (stdio loop)                │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────────────┐  │
│  │  JSON-RPC   │  │  Tool       │  │  Cross-Repo         │  │
│  │  Dispatcher │──│  Handlers   │──│  TaskManager        │  │
│  │             │  │ (15 tools)  │  │  Resolution         │  │
│  └─────────────┘  └─────────────┘  └─────────────────────┘  │
│         │                  │                  │               │
│         ▼                  ▼                  ▼               │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────────────┐  │
│  │  initialize │  │  task.*     │  │  GuardManager       │  │
│  │  tools/list │  │  commit     │  │  (per-workdir)      │  │
│  │  tools/call │  │  guard.run  │  │                     │  │
│  │             │  │  judge.*    │  │  Judge (per-workdir)│  │
│  └─────────────┘  └─────────────┘  └─────────────────────┘  │
└─────────────────────────────────────────────────────────────┘
                       │
                       ▼
              ┌─────────────────┐
              │  .gitreins/      │
              │  ├── tasks.yaml  │
              │  └── config.yaml │
              └─────────────────┘
```

### Layering Rules

1. **Transport Layer** — stdio line reader, multi-line JSON buffer, brace-counter parser.
2. **Protocol Layer** — JSON-RPC 2.0 request/response framing, method dispatch, error code mapping.
3. **Tool Layer** — 15 tool handlers, each validates arguments, delegates to engine, formats result.
4. **Engine Layer** — `TaskManager`, `GuardManager`, `Judge`, `LLMClient` (shared or per-workdir).

---

## 7. Protocol — JSON-RPC 2.0 over stdio

### 7.1 Transport

- **Medium:** stdin → stdout, one process per connection.
- **Framing:** Each JSON-RPC message is a single JSON object. Objects are separated by newlines (`\n`).
- **Multi-line JSON:** Messages may span multiple lines. The server buffers input and uses brace-counting to detect complete JSON objects before parsing.
- **Encoding:** UTF-8.
- **Logging:** Server logs to stderr. Client must not read from stderr.

### 7.2 Brace-Count Parser

The server maintains a `buffer` string. For each line read from stdin:

1. Append line to buffer.
2. Attempt `json.loads(buffer)`. If success, process the request and clear buffer.
3. If `JSONDecodeError`, scan the buffer character-by-character:
   - Track `depth` (brace nesting level), `in_string` state, and `escape` state.
   - When `depth` reaches 0 after an opening brace, extract the substring from first `{` to closing `}` as a complete JSON object.
   - Process the extracted object, remove it from buffer, repeat.
   - If no complete object found, wait for more input.

### 7.3 Lifecycle

```
Client                          Server
  │                               │
  ├─ initialize ───────────────►  │
  │  {jsonrpc:2.0, id:1,        │
  │   method:initialize}         │
  │                               │
  │◄─────────────── {result:     │
  │                   protocolVersion: "2025-11-25",  (negotiated)
  │                   capabilities: {tools:{}},
  │                   serverInfo: {name:"gitreins",version:"0.1.0"}}
  │                               │
  ├─ tools/list ───────────────►  │
  │                               │
  │◄─────────────── {result:     │
  │                   tools: [ ... 15 schemas ... ]}  (count generated from
  │                                 the live `_tool_schemas()` list)
  │                               │
  ├─ notifications/initialized ►│  (no response)
  │                               │
  ├─ tools/call ───────────────►  │
  │  {name:"task.create",...}    │
  │                               │
  │◄─────────────── {result:     │
  │                   content:[{type:"text",text:"<JSON result>"}]}
  │                               │
  ├─ tools/call ───────────────►  │
  │  {name:"commit",...}         │
  │                               │
  │◄─────────────── {result: ...}│
  │                               │
  │  (SIGTERM or stdin EOF)      │
  │                               │  Server exits loop
```

### 7.4 Response Format

All tool responses are wrapped in MCP `content` array:

```json
{
  "jsonrpc": "2.0",
  "id": 42,
  "result": {
    "content": [
      {
        "type": "text",
        "text": "<JSON-stringified tool result>"
      }
    ]
  }
}
```

The inner `text` field contains a JSON-encoded string of the actual tool result (e.g., `{"task": {...}}`). This is a double-encoding: the tool result is JSON-stringified, then placed inside the MCP text field.

---

## 8. Tool Catalog

The catalog below covers the live `tools/list` surface — 15 tools, count generated from the schemas returned by `GitReinsMCPServer._tool_schemas()` (a reverse parity test, `tests/test_mcp_protocol_spec.py`, fails the build if a live tool name is missing here or a non-live name appears). Reproduce the live list with:

```
echo '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' | python -m gitreins_mcp.server
```

| # | Tool | workdir | § |
|---|------|---------|---|
| 1 | `configure` | — | 8.1 |
| 2 | `repo.init` | ✓ | 8.2 |
| 3 | `task.create` | ✓ | 8.3 |
| 4 | `task.start` | ✓ | 8.4 |
| 5 | `task.complete` | ✓ | 8.5 |
| 6 | `task.list` | ✓ | 8.6 |
| 7 | `task.get` | ✓ | 8.7 |
| 8 | `task.delete` | ✓ | 8.8 |
| 9 | `commit` | ✓ | 8.9 |
| 10 | `guard.run` | ✓ | 8.10 |
| 11 | `judge.evaluate` | ✓ | 8.11 |
| 12 | `judge.status` | — | 8.12 |
| 13 | `quality.status` | ✓ | 8.13 |
| 14 | `propagate` | — | 8.14 |
| 15 | `context.resolve` | — | 8.15 |

### 8.1 configure

Hot-reload the MCP server's LLM configuration at runtime: sets environment variables and recreates the LLM client + Judge so subsequent tool calls (judge.evaluate, task.complete) use the new config. Works with any MCP client — no config file editing or server restart needed.

**inputSchema properties:** `env` (object of string env vars), `model` (string), `base_url` (string), `provider` (string). None required.

**Behavior / returns:**
- `env` values are pushed into `os.environ`; `model` sets `GITREINS_LLM_MODEL`, `base_url` sets `GITREINS_LLM_BASE_URL`, `provider` sets `GITREINS_LLM_PROVIDER`.
- Deliberately performs **no validation** of the values — validation happens at evaluation time when the recreated client is first used. A bad model or unreachable `base_url` surfaces in `judge.evaluate` / `task.complete`, not here.
- Returns `{"configured": true, "previous": {...}, "current": {...}, "note": "..."}` where each snapshot is `{model, provider, api_key_configured, api_key_prefix, base_url, env_keys}`. No arguments is a no-op.

### 8.2 repo.init

Writes the same default `.gitreins/config.yaml` the CLI's `gitreins init` writes, so the workflow repo.init → guard.run → judge works entirely over MCP (DF-GITREINS-POC-77). Read-only elsewhere: never touches tracked files, never parses or overwrites an existing config.

**inputSchema:** `{ "workdir": {"type": "string"} }` — no required properties.

**Behavior:**
- Resolves `workdir` (default: server workdir).
- Refuses (writes nothing, creates no `.gitreins/` directory) when the target is not a git repository.

**Return shapes:**
- Created: `{"created": true, "config_path": "<wd>/.gitreins/config.yaml", "workdir": "..."}`
- Idempotent: `{"created": false, "config_path": "...", "workdir": "...", "note": "config already present — not overwritten"}`
- Not a git repo: `{"error": "<wd> is not a git repository (no .git directory)", "workdir": "..."}`

**Test coverage:** `tests/test_mcp_init.py` (advertised in tools/list, config creation, idempotence, customized config not clobbered, plain-dir refusal, default workdir, guard.run works after repo.init).

### 8.3 task.create

Create a new task with completion criteria.

| Property | Value |
|----------|-------|
| **Name** | `task.create` |
| **Description** | Create a new task with criteria that must be met before commit. |

**inputSchema:**

```json
{
  "type": "object",
  "properties": {
    "id": {
      "type": "string",
      "description": "Unique task ID (e.g., 'login-endpoint')"
    },
    "title": {
      "type": "string",
      "description": "Human-readable title"
    },
    "criteria": {
      "type": "array",
      "items": {"type": "string"},
      "description": "List of completion criteria — each must be verified"
    },
    "workdir": {
      "type": "string",
      "description": "Absolute path to the repo. Tasks are stored in <workdir>/.gitreins/tasks.yaml. Defaults to the MCP server's workdir."
    }
  },
  "required": ["id", "title", "criteria"]
}
```

**Behavior:**
- Resolves `workdir` to absolute path (default: server workdir).
- Creates a `TaskManager` for the target workdir if different from server default.
- Calls `TaskManager.create(id, title, criteria)`.
- Persists task to `<workdir>/.gitreins/tasks.yaml`.
- Returns the task as a dictionary (id, title, criteria, status, created_at).

**Return shape:**

```json
{
  "id": "login-endpoint",
  "title": "Implement login endpoint",
  "criteria": ["POST /login returns 200", "Password hashed with bcrypt"],
  "status": "pending",
  "created_at": "2026-06-20T14:32:00Z"
}
```

**Error conditions:**
- Task ID already exists (engine-level error, returned as `{"error": "..."}` in result text).
- Invalid workdir (filesystem errors bubble up as JSON-RPC `-32000` server error).

---

### 8.4 task.start

Mark a task as in-progress.

| Property | Value |
|----------|-------|
| **Name** | `task.start` |
| **Description** | Mark a task as in-progress. |

**inputSchema:**

```json
{
  "type": "object",
  "properties": {
    "id": {
      "type": "string",
      "description": "Task ID to start"
    },
    "workdir": {
      "type": "string",
      "description": "Absolute path to the repo containing the task. Defaults to the MCP server's workdir."
    }
  },
  "required": ["id"]
}
```

**Behavior:**
- Resolves `workdir` (default: server workdir).
- Calls `TaskManager.start(id)`.
- Transitions task status from `pending` to `in_progress`.
- Returns updated task dictionary.

**Return shape:** Same as `task.create` with `status: "in_progress"`.

**Error conditions:**
- Task not found → returns `{"error": "Task not found: <id>"}` in result text.
- Task already in-progress or complete → engine-level state error.

---

### 8.5 task.complete

Mark a task as complete. Triggers evaluation if LLM is configured.

| Property | Value |
|----------|-------|
| **Name** | `task.complete` |
| **Description** | Mark a task as complete. Triggers evaluation if LLM is configured. |

**inputSchema:**

```json
{
  "type": "object",
  "properties": {
    "id": {
      "type": "string",
      "description": "Task ID to complete"
    },
    "workdir": {
      "type": "string",
      "description": "Absolute path to the repo containing the task. Defaults to the MCP server's workdir."
    }
  },
  "required": ["id"]
}
```

**Behavior:**
- Resolves `workdir` (default: server workdir).
- Calls `TaskManager.complete(id)`.
- Transitions task status to `complete`.
- If the LLM credential resolves right now:
  - Dispatches a background evaluation job (async by default — see §8.11.1) and returns the task plus `job_id` / `status: "running"` / note. Poll `judge.status`.
  - The job persists its verdict into the workdir's `.gitreins/history` (source marker `mcp`).
- If no LLM credential resolves: returns task with `"note": "LLM not configured — skipping evaluation"`.

**Return shape (with evaluation — async dispatch):**

```json
{
  "task": { "id": "login-endpoint", "status": "complete", "...": "..." },
  "job_id": "J-abc123",
  "status": "running",
  "note": "evaluation running in background — poll judge.status"
}
```

**Return shape (without evaluation):**

```json
{
  "task": { ... },
  "note": "LLM not configured — skipping evaluation"
}
```

**Error conditions:**
- Task not found → `{"error": "Task not found: <id>"}`.
- Evaluation failure → the dispatched job transitions to `status: "error"` (non-fatal — poll `judge.status` for the message; see §8.11.1).

---

### 8.6 task.list

List all tasks, optionally filtered by status.

| Property | Value |
|----------|-------|
| **Name** | `task.list` |
| **Description** | List all tasks, optionally filtered by status. |

**inputSchema:**

```json
{
  "type": "object",
  "properties": {
    "status": {
      "type": "string",
      "enum": ["pending", "in_progress", "complete"],
      "description": "Filter by status"
    },
    "workdir": {
      "type": "string",
      "description": "Absolute path to the repo. Defaults to the MCP server's workdir."
    }
  }
}
```

**Behavior:**
- Resolves `workdir` (default: server workdir).
- Calls `TaskManager.list_tasks(status)`.
- Returns array of task dictionaries.

**Return shape:**

```json
{
  "tasks": [
    {"id": "...", "title": "...", "criteria": [...], "status": "pending", "created_at": "..."},
    ...
  ]
}
```

**Error conditions:**
- Invalid status filter (not in enum) → engine ignores filter, returns all tasks.

---

### 8.7 task.get

Get a single task by ID.

| Property | Value |
|----------|-------|
| **Name** | `task.get` |
| **Description** | Get a task by ID. |

**inputSchema:**

```json
{
  "type": "object",
  "properties": {
    "id": {
      "type": "string",
      "description": "Task ID"
    },
    "workdir": {
      "type": "string",
      "description": "Absolute path to the repo containing the task. Defaults to the MCP server's workdir."
    }
  },
  "required": ["id"]
}
```

**Behavior:**
- Resolves `workdir` (default: server workdir).
- Calls `TaskManager.get(id)`.
- Returns task dictionary or error.

**Return shape (found):** Task dictionary (same as `task.create` return).

**Return shape (not found):**

```json
{"error": "Task not found: <id>"}
```

---

### 8.8 task.delete

Delete a task by ID.

| Property | Value |
|----------|-------|
| **Name** | `task.delete` |
| **Description** | Delete a task by ID. |

**inputSchema:**

```json
{
  "type": "object",
  "properties": {
    "id": {
      "type": "string",
      "description": "Task ID to delete"
    },
    "workdir": {
      "type": "string",
      "description": "Absolute path to the repo containing the task. Defaults to the MCP server's workdir."
    }
  },
  "required": ["id"]
}
```

**Behavior:**
- Resolves `workdir` (default: server workdir).
- Calls `TaskManager.delete(id)`.
- Removes task from `<workdir>/.gitreins/tasks.yaml`.
- Returns confirmation.

**Return shape:**

```json
{"deleted": "login-endpoint"}
```

**Error conditions:**
- Task not found → `{"error": "Task not found: <id>"}`.

---

### 8.9 commit

Create a git commit. Runs guards first. Rejects if guards fail or tasks are in-progress.

| Property | Value |
|----------|-------|
| **Name** | `commit` |
| **Description** | Create a git commit. Runs guards first. Rejects if guards fail. |

**inputSchema:**

```json
{
  "type": "object",
  "properties": {
    "message": {
      "type": "string",
      "description": "Commit message"
    },
    "workdir": {
      "type": "string",
      "description": "Absolute path to the repo to commit in. Defaults to the MCP server's workdir."
    }
  },
  "required": ["message"]
}
```

**Behavior:**
1. Check for in-progress tasks in the **resolved workdir** (the `workdir` param if given, else the server default — DF-GITREINS-POC-76). A cross-repo commit is held to the target repo's gate; the default workdir keeps its byte-for-byte former behavior.
2. If any in-progress tasks exist, reject with error listing their IDs.
3. Run Tier 1 guards: on the default workdir via the guard manager built at server construction; on a cross-repo target via a fresh config-bound `GuardManager` — including the missing-config refusal, so a config-less repo can never report a false green (GR-GAP-054).
4. If guards fail, reject with error and guard summary.
5. Execute `git commit -m <message>` in the resolved workdir.
6. Return commit result (success flag + output).

**inputSchema:** `message` (string, required) + `workdir` (string, optional — the tool's schema exposes the optional workdir; see `test_commit_tool_schema_exposes_optional_workdir`).

**Return shape (success):**

```json
{
  "committed": true,
  "output": "[main abc1234] Implement login endpoint\n 2 files changed, 45 insertions(+)"
}
```

**Return shape (in-progress tasks):**

```json
{
  "error": "Tasks still in progress: <ids> — commits are blocked while a task is in_progress because task.complete runs the quality judge against the committed state. Complete them via task.complete, or delete them via task.delete, then retry commit.",
  "tasks": ["login-endpoint", "password-hash"]
}
```

**Return shape (guards failed):**

```json
{
  "error": "Tier 1 guards failed — commit blocked",
  "details": "...guard summary..."
}
```

**Error conditions:**
- In-progress tasks → blocking error (no commit attempted).
- Guard failures → blocking error (no commit attempted).
- Git command failure → `{"committed": false, "output": "..."}`.
- Exception → `{"error": "<exception message>"}`.

---

### 8.10 guard.run

Run Tier 1 static guards (secrets, lint, tests, and configured static-analysis/LSP checks). Optional `dead_code` enables the Python AST detector for this invocation; optional workdir supports cross-repo use.

| Property | Value |
|----------|-------|
| **Name** | `guard.run` |
| **Description** | Run Tier 1 static guards. `dead_code` enables the Python AST detector; configured `static_analysis` and `lsp` guards report normalized diagnostics. Optional workdir supports cross-repo use. |

**inputSchema:**

```json
{
  "type": "object",
  "properties": {
    "workdir": {
      "type": "string",
      "description": "Absolute path to the repo to guard. Defaults to the MCP server's workdir."
    },
    "dead_code": {
      "type": "boolean",
      "description": "Enable Python AST-based dead-code detection for this run, overriding the repository guard config.",
      "default": false
    }
  }
}
```

**Behavior:**
- Resolves `workdir` (default: server workdir).
- Loads config from `<workdir>/.gitreins/config.yaml` (if exists).
- Creates `GuardManager(wd, config=config)`.
- Runs `gm.run_all(force_dead_code=dead_code)`.
- Returns pass/fail status, workdir, and truncated results (output capped at 500 chars per guard).
- When enabled in `guards`, the result list can include `dead_code`, `static_analysis`, and `lsp`. Static-analysis and LSP output contains normalized `file:line`, tool, and diagnostic-message evidence; an error diagnostic makes that guard fail.

**Return shape:**

```json
{
  "passed": true,
  "workdir": "/home/kara/my-project",
  "results": [
    {
      "name": "secrets",
      "passed": true,
      "output": "No secrets found"
    },
    {
      "name": "lint",
      "passed": false,
      "output": "main.go:42: error: ..."
    },
    {
      "name": "lsp",
      "passed": true,
      "output": "  pylsp — clean"
    },
    {
      "name": "static_analysis",
      "passed": false,
      "output": "  ✗ src/auth.py:42 [mypy] Incompatible return value type"
    }
  ]
}
```

**Error conditions:**
- Config file unreadable → silently ignored, guards run with empty config.
- Guard execution exception → bubbles up as JSON-RPC `-32000` server error.

---

### 8.11 judge.evaluate

Run full evaluation pipeline (Tier 1 + Tier 2) on a task. **Async by default** (see §8.11.1): `wait=false` (schema default) dispatches a background job and returns `{"job_id", "status": "running", "task_id", "workdir"}`; pass `wait=true` for the legacy blocking behavior. Caps can be set individually or via legacy `eval_cap` string.

| Property | Value |
|----------|-------|
| **Name** | `judge.evaluate` |
| **Description** | Run full evaluation pipeline (Tier 1 + Tier 2) on a task. By default the evaluation runs in a background job and the call returns immediately with a job_id — poll judge.status for the result. Pass wait=true for the legacy synchronous behavior. Caps can be set individually or via legacy eval_cap string. |

**inputSchema:**

```json
{
  "type": "object",
  "properties": {
    "id": {
      "type": "string",
      "description": "Task ID to evaluate"
    },
    "workdir": {
      "type": "string",
      "description": "Absolute path to the repo containing the task. Defaults to the MCP server's workdir."
    },
    "wait": {
      "type": "boolean",
      "description": "If true, block until the evaluation finishes and return the full result dict (legacy synchronous behavior). If false (default), dispatch a background job and return immediately — poll judge.status with the returned job_id.",
      "default": false
    },
    "max_iterations": {
      "type": "number",
      "description": "Max LLM reasoning turns (-1 = unlimited). Tool calls cost 0.1 by default."
    },
    "max_time": {
      "type": "string",
      "description": "Wall-clock cap: '30s', '5m', '2h'."
    },
    "max_input_tokens": {
      "type": "string",
      "description": "Input token budget: '200k', '0.1M'."
    },
    "max_output_tokens": {
      "type": "string",
      "description": "Output token budget: '50k', '0.05M'."
    },
    "tool_call_weight": {
      "type": "number",
      "description": "Fraction of an iteration each tool call costs (default 0.1)."
    },
    "eval_cap": {
      "type": "string",
      "description": "Legacy combined cap string: '100/30m/200k/50k'. Individual params take priority if both are set."
    }
  },
  "required": ["id"]
}
```

**Behavior:**
- Resolves `workdir` (default: server workdir).
- Builds `EvalCap` from parameters (see §9 for priority chain).
- Looks up task by ID in target workdir.
- Creates a fresh `Judge` for the target workdir with the computed `EvalCap`.
- Runs `Judge.evaluate_task(task)` (Tier 1 guards + Tier 2 LLM evaluation).
- A Tier 2 pipeline may additionally run the `commit_audit` stage. It supports message validation, single-pass CodeRabbit-style review, or tool-using agent review; review findings include CVE-style 1–10 scores and are evaluated against the configured score threshold and multiplier.
- Returns evaluation result.

**Return shape:**

```json
{
  "task_id": "login-endpoint",
  "passed": true,
  "workdir": "/home/kara/my-project",
  "tier1_passed": true,
  "verdict": "PASS",
  "items": [
    {"criterion": "POST /login returns 200", "status": "PASS", "detail": "..."}
  ],
  "summary": "All criteria passed"
}
```

**Error conditions:**
- Task not found → `{"error": "Task not found: <id> in <workdir>"}` (cross-repo) or `{"error": "Task not found: <id>"}` (default workdir).
- Evaluation exception → bubbles up as JSON-RPC `-32000` server error.

---

### 8.11.1 Async job semantics (judge.evaluate / task.complete / judge.status)

By default (`wait=false`, the schema default) `judge.evaluate` dispatches the full Tier 1 + Tier 2 pipeline to a **background job** and returns immediately:

```json
{"job_id": "<id>", "status": "running", "task_id": "<id>", "workdir": "..."}
```

`task.complete` with a configured LLM credential dispatches the same kind of job and returns `{"task": {...}, "job_id": "<id>", "status": "running", "note": "evaluation running in background — poll judge.status"}`. Pass `wait=true` to block and return the full result (legacy sync; risks client tool-call timeouts on slow suites). The sync path persists its verdict too (source marker `mcp-sync` vs `mcp` for async jobs).

Jobs are **disk-backed** (`~/.local/share/gitreins/jobs/`, override `GITREINS_JOB_DIR`): they survive server restarts, an orphaned `running` job is auto-resumed on the next poll, and CLI `gitreins judge --async` dispatches share the store. Evaluation jobs run ONE at a time per server instance (parallel judges contend on ports/tmp and the shared `.gitreins/history` git storage). Every dispatched job carries a 1h wall-clock ceiling so a hung network connect cannot stall it.

"Configured" means the credential `LLMClient` resolves **right now** (DF-GITREINS-POC-78): a key rotated away mid-session is refused at dispatch, and a key the provider rejects fails the job fast to `status: "error"` — it never sits in `running` forever.

### 8.12 judge.status

Poll a background evaluation job started by `judge.evaluate` (async) or `task.complete` (with LLM configured).

**inputSchema:** `{ "job_id": {"type": "string"} }`, required `["job_id"]`. No `workdir` — job IDs are global to the job store.

**Return shapes:**
- `{"job_id": ..., "status": "running", "running": true}` (fresh payloads also carry `pid` and `started_at`)
- `{"job_id": ..., "status": "complete", "running": false, "result": {"task_id", "passed", "workdir", "tier1_passed", "verdict", "items", "summary"}}`
- `{"job_id": ..., "status": "error", "running": false, "error": "..."}`
- Unknown job: `{"error": "Job not found: <job_id>"}` (from disk).

**Poll-loop contract:** every payload carries a boolean `running` (absent on old builds → reported `false`). Terminate on `not running and status in {"complete", "error"}`; never poll a bare `running` field.

### 8.13 quality.status

Read-only: reports the repo-produced quality snapshot this run has already computed — the SAME dict `guard.run` (response field `quality_snapshot`), `judge.evaluate`/`judge.status` (result field `quality_snapshot`) and `gitreins doctor` surface. One authority per run (GR-143): the first surface runs the repository's producer once, every other surface reads that computed snapshot. This tool **never triggers the producer**.

**inputSchema:** `{ "workdir": {"type": "string"} }` — no required properties.

**Return shapes:**
- `{"workdir", "status": "available", "quality_snapshot": {...}}`
- `{"workdir", "status": "not-computed", "note": "...call guard.run (or judge.evaluate) first"}`
- `{"workdir", "status": "disabled"}` — no enabled quality config in the repo.

### 8.14 propagate

Copy `.gitreins/config.yaml` guard configuration from one repository to sibling repositories. The target config is merged recursively: source-only keys are added while existing target keys and nested overrides win on conflicts.

| Property | Value |
|----------|-------|
| **Name** | `propagate` |
| **Description** | Propagate guard configuration to sibling repositories without clobbering target overrides. |

**inputSchema:**

```json
{
  "type": "object",
  "properties": {
    "source": {
      "type": "string",
      "description": "Source repo path. Defaults to the MCP server's workdir."
    },
    "targets": {
      "type": "array",
      "items": {"type": "string"},
      "description": "Target repository paths to receive the merged guard configuration."
    }
  },
  "required": ["targets"]
}
```

**Behavior:**
- Resolves `source` (default: server workdir) and requires at least one target.
- Reads `<source>/.gitreins/config.yaml`; returns an error without changing targets if the source config is absent or unreadable.
- Creates `<target>/.gitreins/` when needed.
- Creates a target config when missing; otherwise recursively merges source keys into it, preserving target scalar values and nested overrides.
- Returns `source` and one result per target with `action` (`created` or `merged`), `keys_added`, and `keys_preserved`.

**Error conditions:**
- Missing or empty `targets` → `{"error": "targets list is required"}`.
- Missing/unreadable source config → an error result identifying the expected source config path; no target is updated.

### 8.15 context.resolve

Asks the Jev resolution gate (JEVRES-001, `engine/resolution.py`) whether the code already answers a question: traces the question to seed files with Hilo, assembles a bundle inside a measured token budget, asks the Jev decisions model for a calibrated probability that the evidence resolves the question, and bands the answer in code. The probability, bundle manifest and both token counts are part of the verdict — an answer with no traceable evidence is never returned (spec: `docs/jev-resolution-gate.md`).

**inputSchema:** `{ "question": {"type": "string"}, "budget": {"type": "integer"} }`, required `["question"]`. No `workdir` — it resolves against the server's workdir. `budget` defaults to the engine's measured 28,000-token ceiling.

**Returns:** the full verdict object (`{"question", "verdict", "probability", "missing_kind", "evidence_quality", "manifest": [...], "model", "input_tokens", "tokens_estimated", "cost_usd", "abstain_reason", "exit_code", ...}`). Verdict bands in code: RESOLVED (≥0.85), REVIEW (0.50–0.85), UNRESOLVED (<0.50), ABSTAIN for any failure (fail-closed, with a named `abstain_reason` and `abstain_action`).

**Disabled by default:** runs only when `resolution.enabled.mcp: true` is set in the repo's `.gitreins/config.yaml`; absent, returns ABSTAIN with `abstain_reason: "surface-disabled"`. A real band is filed in the server workdir's `.gitreins/history` (`kind: "resolution"`, `source: "mcp"`); an ABSTAIN files nothing.

---

## 9. Cross-Repo Workdir

### 9.1 Resolution Pattern

All tools that touch a repo accept an optional `workdir` parameter: `repo.init`, `task.create`, `task.start`, `task.complete`, `task.list`, `task.get`, `task.delete`, `commit`, `guard.run`, `judge.evaluate`, `quality.status` — 10 schemas total carry `workdir`. `configure`, `judge.status`, `propagate`, and `context.resolve` do not. The resolution follows this pattern:

```
if workdir param provided:
    wd = os.path.abspath(workdir)
    if wd != server.workdir:
        create fresh TaskManager(wd)
        create fresh Judge(llm, wd) if needed
else:
    use server.default TaskManager / Judge
```

### 9.2 Task Storage

Tasks are stored in `<workdir>/.gitreins/tasks.yaml`. Each workdir has its own independent task store. Task IDs need only be unique within a single workdir.

### 9.3 Guard Config Loading

`guard.run` loads guard configuration from `<workdir>/.gitreins/config.yaml`. A config-less repo is **refused, never run on defaults** (GR-GAP-054): `{"error": "no .gitreins/config.yaml in <workdir> — run `gitreins init` first. ..."}` — never a false green. MCP-only clients create the config with `repo.init`.

### 9.4 Judge Fresh Instance

`judge.evaluate` always creates a fresh `Judge` instance for the target workdir. This ensures:
- Correct config loading from target repo.
- Clean evaluator state (no cumulative iteration credit from prior evaluations).
- Isolated `EvalCap` per evaluation call.

---

## 10. Evaluator Caps via MCP

### 10.1 Cap Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `max_iterations` | number | `-1` (unlimited) | Max LLM reasoning turns. `-1` = unlimited. Tool calls cost `tool_call_weight`. |
| `max_time` | string | `""` (unlimited) | Wall-clock cap. Formats: `30s`, `5m`, `2h`. |
| `max_input_tokens` | string | `""` (unlimited) | Input token budget. Formats: `200k`, `0.1M`. |
| `max_output_tokens` | string | `""` (unlimited) | Output token budget. Formats: `50k`, `0.05M`. |
| `tool_call_weight` | number | `0.1` | Fraction of an iteration each tool call costs. |
| `eval_cap` | string | `""` | Legacy combined cap string: `"100/30m/200k/50k"`. |

### 10.2 Priority Chain

When multiple cap sources are present, the priority is:

1. **Individual MCP params** (`max_iterations`, `max_time`, etc.) — highest priority
2. **Legacy `eval_cap` string** — parsed if individual params are absent
3. **`config.yaml` defaults** — lowest priority, used when neither 1 nor 2 is present

### 10.3 Legacy eval_cap String Format

The `eval_cap` string uses slash-separated components in this order:

```
<iterations>/<time>/<input_tokens>/<output_tokens>
```

Examples:
- `"100/30m/200k/50k"` → 100 iterations, 30 minutes, 200k input, 50k output
- `"50/5m"` → 50 iterations, 5 minutes, unlimited tokens
- `"200"` → 200 iterations, unlimited time/tokens
- `"-1"` or `"unlimited"` → all caps disabled

Parsing is lenient: missing components are treated as unlimited. Token suffixes `k` (×1000) and `M` (×1,000,000) are supported. Time suffixes `s`, `m`, `h` are supported.

### 10.4 Iteration Accounting

- **LLM reasoning call:** costs `1.0` iterations.
- **Tool call:** costs `tool_call_weight` iterations (default `0.1`).
- **Cap check:** The iteration cap is checked **before** each call. At `99.9/100`, a full `1.0` call is still allowed (final count may slightly exceed cap).
- **Time/token caps:** Hard limits checked continuously. No leniency.

---

## 11. Error Taxonomy and Exit Codes

### 11.1 JSON-RPC Standard Errors

| Code | Name | Condition | Example Message |
|------|------|-----------|-----------------|
| `-32600` | Invalid Request | `jsonrpc` field missing or not `"2.0"` | `"Invalid Request: jsonrpc field must be '2.0'"` |
| `-32601` | Method Not Found | Unknown `method` or unknown `tool` name | `"Unknown method: foo"` / `"Unknown tool: foo"` |
| `-32000` | Server Error | Unhandled exception in handler | `"<exception message>"` (sanitized) |

### 11.2 Domain Errors (in tool result text)

These are **not** JSON-RPC errors. They are returned as successful JSON-RPC responses with `{"error": "..."}` in the result text.

| Condition | Tool(s) | Result Shape |
|-----------|---------|--------------|
| Task not found | task.start, task.complete, task.get, task.delete, judge.evaluate | `{"error": "Task not found: <id>"}` |
| Task not found (cross-repo) | judge.evaluate | `{"error": "Task not found: <id> in <workdir>"}` |
| In-progress tasks blocking commit | commit | `{"error": "Tasks still in progress: <ids> — commits are blocked while a task is in_progress because task.complete runs the quality judge against the committed state. Complete them via task.complete, or delete them via task.delete, then retry commit.", "tasks": [...]}` |
| Tier 1 guards failed | commit | `{"error": "Tier 1 guards failed — commit blocked", "details": "..."}` |
| Guard config missing | guard.run (and cross-repo commit) | `{"error": "no .gitreins/config.yaml in <workdir> — run `gitreins init` first. ..."}` |
| Not a git repository | repo.init | `{"error": "<wd> is not a git repository (no .git directory)", "workdir": "..."}` |
| Job not found | judge.status | `{"error": "Job not found: <job_id>"}` |
| LLM not configured (dispatch) | judge.evaluate | `{"error": "LLM not configured — set GITREINS_LLM_API_KEY"}` |
| Missing propagate targets | propagate | `{"error": "targets list is required"}` |
| Source config missing | propagate | error result naming the expected source config path (no target updated) |
| LLM not configured | task.complete | `{"task": {...}, "note": "LLM not configured — skipping evaluation"}` |

### 11.3 Server Exit Codes

| Code | Condition |
|------|-----------|
| `0` | Clean shutdown (SIGTERM or stdin EOF) |
| `1` | Uncaught exception during startup |

---

## 12. Test Strategy

The MCP surface is covered by four real test files (the original per-layer file names below
— `test_stdio_transport.py`, `test_jsonrpc_dispatch.py`, `test_task_tools.py`,
`test_commit_tool.py`, `test_guard_tool.py`, `test_judge_tool.py`, `test_mcp_lifecycle.py`
— were never created; this table lists what actually exists):

| File | What it verifies | Mock/Real |
|-------|---------------|-----------|
| `tests/test_mcp_server.py` | The bulk of the surface against an in-process server: initialize handshake + version, tools/list (15 tools, names, schema shape), unknown method/tool error codes, multi-line JSON buffering (`TestStdioBuffering`), tools/call content wrapping, all task.* tools, commit (incl. cross-repo workdir gate + schema exposing optional workdir), guard.run (pass/fail, missing-config refusal, cross-repo), judge.evaluate caps + async job dispatch/poll/persistence (single-flight, restart survival, `running` field semantics), configure hot-reload | Real server instance, mocked LLM |
| `tests/test_mcp_init.py` | `repo.init`: advertised in tools/list, config creation, idempotence, customized config not clobbered, plain-dir refusal (writes nothing), default workdir, guard.run works after repo.init | Real filesystem + git repos |
| `tests/test_mcp_integration.py` | Real-subprocess stdio integration: server boot, tools/list over the wire, task lifecycle round-trip, cross-repo task workdir, guard.run scans the correct repo | Real server subprocess |
| `tests/test_mcp_verdict_persistence.py` | Verdict persistence: `.gitreins/history` writes with `job_id`/`source` markers, ordering (verdict lands before job reads complete), no stdout pollution, history-disabled behavior, resume/supersede leaving exactly one live record per job id, CLI shared-helper parity | Real server, mocked LLM |

Related: `tests/test_version.py` checks the MCP server identity matches the package version;
`tests/test_cli.py::test_mcp_server_import_path` verifies the `gitreins mcp-server` CLI entry.

### Test Fixtures

Fixtures live under `tests/fixtures/` (`data_protection/`, `jevres_cases/`, `lsp/`,
`secrets/`); the MCP tests build throwaway git repos per test via pytest tmp fixtures
(`tmp_git_repo`, `bare_git_repo`, `tmp_workdir`) rather than committed repo fixtures.

---

## 13. Observability

### 13.1 Logging

- **Logger name:** `gitreins.mcp`
- **Format:** `%(asctime)s [%(name)s] %(levelname)s: %(message)s`
- **Destination:** stderr
- **Level:** INFO

### 13.2 Log Events

| Event | Level | Fields |
|-------|-------|--------|
| Server startup | INFO | `workdir` |
| Task created | INFO | `id`, `workdir` |
| Task started | INFO | `id`, `workdir` |
| Task completed | INFO | `id`, `workdir` |
| Task deleted | INFO | `id`, `workdir` |
| Request error | ERROR | `method`, exception traceback (internal only) |
| Evaluation failed | ERROR | `id`, exception traceback (internal only) |

### 13.3 Metrics (Future)

| Metric | Type | Description |
|--------|------|-------------|
| `gitreins_mcp_requests_total` | Counter | Total JSON-RPC requests by method |
| `gitreins_mcp_tool_calls_total` | Counter | Total tool calls by tool name |
| `gitreins_mcp_errors_total` | Counter | Total errors by error code |

---

## 14. Implementation Status

| Component | Status | Notes |
|-----------|--------|-------|
| JSON-RPC 2.0 dispatcher | ✅ Implemented | `handle_request()` method |
| stdio transport loop | ✅ Implemented | `run_stdio()` with brace counting |
| Multi-line JSON buffer | ✅ Implemented | Brace-depth parser with string/escape handling |
| Initialize handshake | ✅ Implemented | Negotiates: echoes a supported revision (`2025-11-25`, `2025-06-18`, `2025-03-26`, `2024-11-05`), else answers with `2025-11-25` and logs the mismatch on stderr |
| Tools/list endpoint | ✅ Implemented | Returns all 15 tool schemas (count generated from `_tool_schemas()`; see §8) |
| configure | ✅ Implemented | Hot-reload LLM config; no validation at set time |
| repo.init | ✅ Implemented | MCP-only init; idempotent; refuses non-git dirs |
| task.create | ✅ Implemented | With cross-repo workdir |
| task.start | ✅ Implemented | With cross-repo workdir |
| task.complete | ✅ Implemented | Async dispatch of evaluation job when LLM credential resolves; skip note otherwise |
| task.list | ✅ Implemented | Status filter optional |
| task.get | ✅ Implemented | Error on not found |
| task.delete | ✅ Implemented | Error on not found |
| commit | ✅ Implemented | Resolved-workdir in-progress gate + guard run + git commit (DF-GITREINS-POC-76) |
| guard.run | ✅ Implemented | Cross-repo config loading, result truncation, missing-config refusal |
| judge.evaluate | ✅ Implemented | Async by default (background job + judge.status polling), fresh Judge per call, cap priority chain |
| judge.status | ✅ Implemented | Disk-backed jobs, restart survival, auto-resume of orphaned running jobs |
| quality.status | ✅ Implemented | Read-only snapshot peek; never triggers the producer |
| propagate | ✅ Implemented | Recursive merge, target overrides win |
| context.resolve | ✅ Implemented | Jev resolution gate (JEVRES-001); disabled unless `resolution.enabled.mcp: true` |
| Error code mapping | ✅ Implemented | `-32600`, `-32601`, `-32000` + domain errors |
| SIGTERM handling | ⏳ Stub | Server exits on stdin EOF; explicit SIGTERM handler not implemented |

---

## 15. Verification Checklist

- [ ] `python -m gitreins_mcp.server` starts without error
- [ ] `echo '{"jsonrpc":"2.0","id":1,"method":"initialize"}' | python -m gitreins_mcp.server` returns protocol version
- [ ] `echo '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' | ...` returns the 15 live tool schemas (count generated from `tools/list`; see §8)
- [ ] `task.create` with valid args creates task in `.gitreins/tasks.yaml`
- [ ] `task.create` with duplicate ID returns error in result text
- [ ] `task.start` transitions status to `in_progress`
- [ ] `task.complete` without LLM key returns `"note": "LLM not configured..."`
- [ ] `task.list` with status filter returns filtered results
- [ ] `task.get` for missing ID returns `{"error": "Task not found..."}`
- [ ] `task.delete` removes task from store
- [ ] `commit` with in-progress tasks returns blocking error
- [ ] `commit` with failing guards returns blocking error
- [ ] `guard.run` loads config from target workdir
- [ ] `judge.evaluate` with individual caps overrides `eval_cap` string
- [ ] `judge.evaluate` with `eval_cap` string overrides config.yaml defaults
- [ ] Invalid `jsonrpc` field returns `-32600`
- [ ] Unknown method returns `-32601`
- [ ] Unknown tool name returns `-32601`
- [ ] Multi-line JSON request is parsed correctly

---

## 16. Example Outputs

> **Identity note (DF-GITREINS-POC-5):** the transcripts below were captured from the
> v0.1.0 PoC drive, so their `serverInfo.version` reads `"0.1.0"`. The live server
> reports the **installed** release in `serverInfo.version` (the same source
> `gitreins --version` reads) and logs a startup acknowledgement on stderr; read the
> field from the handshake rather than from these samples.

### 16.1 Happy Path — Create, Start, Complete, Commit

```
$ echo '{"jsonrpc":"2.0","id":1,"method":"initialize"}' | python -m gitreins_mcp.server
{"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2025-11-25", "capabilities": {"tools": {}}, "serverInfo": {"name": "gitreins", "version": "0.1.0"}}}

$ echo '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"task.create","arguments":{"id":"login","title":"Login endpoint","criteria":["POST /login 200","Hash password"]}}' | python -m gitreins_mcp.server
{"jsonrpc": "2.0", "id": 2, "result": {"content": [{"type": "text", "text": "{\"id\": \"login\", \"title\": \"Login endpoint\", \"criteria\": [\"POST /login 200\", \"Hash password\"], \"status\": \"pending\", \"created_at\": \"2026-06-20T14:32:00Z\"}"}]}}

$ echo '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"task.start","arguments":{"id":"login"}}}' | python -m gitreins_mcp.server
{"jsonrpc": "2.0", "id": 3, "result": {"content": [{"type": "text", "text": "{\"id\": \"login\", \"title\": \"Login endpoint\", \"criteria\": [...], \"status\": \"in_progress\", \"created_at\": \"...\"}"}]}}

$ echo '{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"task.complete","arguments":{"id":"login"}}}' | python -m gitreins_mcp.server
{"jsonrpc": "2.0", "id": 4, "result": {"content": [{"type": "text", "text": "{\"task\": {...}, \"note\": \"LLM not configured — skipping evaluation\"}"}]}}

$ echo '{"jsonrpc":"2.0","id":5,"method":"tools/call","params":{"name":"commit","arguments":{"message":"Implement login endpoint"}}}' | python -m gitreins_mcp.server
{"jsonrpc": "2.0", "id": 5, "result": {"content": [{"type": "text", "text": "{\"committed\": true, \"output\": \"[main abc1234] Implement login endpoint\\n 2 files changed, 45 insertions(+)\"}"}]}}
```

### 16.2 Error Path — Commit Blocked by In-Progress Task

```
$ echo '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"task.create","arguments":{"id":"api","title":"API","criteria":["Test"]}}}' | python -m gitreins_mcp.server
...task created...

$ echo '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"task.start","arguments":{"id":"api"}}}' | python -m gitreins_mcp.server
...task started...

$ echo '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"commit","arguments":{"message":"WIP"}}}' | python -m gitreins_mcp.server
{"jsonrpc": "2.0", "id": 3, "result": {"content": [{"type": "text", "text": "{\"error\": \"Tasks still in progress — complete or delete them first\", \"tasks\": [\"api\"]}"}]}}
```

### 16.3 Cross-Repo — Evaluate Task in Different Repository

```
$ echo '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"judge.evaluate","arguments":{"id":"auth","workdir":"/home/kara/other-project","max_iterations":50,"max_time":"10m"}}}' | python -m gitreins_mcp.server
{"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": "{\"task_id\": \"auth\", \"passed\": true, \"workdir\": \"/home/kara/other-project\", \"tier1_passed\": true, \"verdict\": \"PASS\", \"items\": [{\"criterion\": \"...\", \"status\": \"PASS\", \"detail\": \"...\"}], \"summary\": \"All criteria passed\"}"}]}}
```

---

## 17. Package Structure

```
gitreins/
├── gitreins_mcp/
│   ├── __init__.py
│   └── server.py              # GitReinsMCPServer class (~517 lines)
├── engine/
│   ├── task_manager.py        # TaskManager — create, start, complete, list, get, delete
│   ├── judge.py               # Judge — evaluate_task, guard_manager
│   ├── llm.py                 # LLMClient — LLM API wrapper
│   ├── guard_manager.py       # GuardManager — run_all, config loading
│   ├── eval_cap.py            # EvalCap, parse_eval_cap, eval_cap_from_config
│   └── config.py              # GitReinsDefaults, config overlay
├── .gitreins/
│   ├── tasks.yaml             # Task store (per-repo)
│   └── config.yaml            # Guard/judge config (per-repo)
├── specs/
│   └── 02-MCP-Protocol.md     # This document
└── tests/
    └── (test files per §12)
```

---

## 18. Document Status

- [x] Mission and scope defined
- [x] All inputs documented (env vars, CLI args, JSON-RPC schema)
- [x] Operating Contract specified (NEVER/ALWAYS rules)
- [x] Assumptions listed
- [x] Architecture diagram and layering rules
- [x] Protocol specified (transport, brace parser, lifecycle, response format)
- [x] Tool Catalog complete (all 15 tools with schemas, behavior, returns, errors — count generated from live `tools/list`, reverse parity test in `tests/test_mcp_protocol_spec.py`)
- [x] Cross-Repo Workdir documented
- [x] Evaluator Caps documented (params, priority chain, legacy format, accounting)
- [x] Error Taxonomy (JSON-RPC + domain + exit codes)
- [x] Test Strategy table with fixtures
- [x] Observability (logging, metrics)
- [x] Implementation Status table
- [x] Verification Checklist (18 items)
- [x] Example Outputs (3 scenarios)
- [x] Package Structure tree
- [x] Document Status checklist

---

*End of 02-MCP-Protocol.md — MCP Protocol Specification*
