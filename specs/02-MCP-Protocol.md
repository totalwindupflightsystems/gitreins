# 02-MCP-Protocol.md — MCP Protocol Specification

Document Status: Draft v0.1 — Specification only, no code.

---

## 1. Mission

Define the Model Context Protocol (MCP) interface exposed by GitReins' stdio JSON-RPC 2.0 server. Primary AI coding agents (Pi, Claude, Hermes, Codex) connect via stdio and use these 9 tools to manage tasks, run guards, evaluate work, and commit code through the harness. This specification covers the wire protocol, tool catalog, cross-repository semantics, evaluator caps, error taxonomy, server lifecycle, and security model.

---

## 2. Scope

### In scope (v1)

- JSON-RPC 2.0 framing over line-delimited stdio
- Multi-line JSON buffering with brace-count parsing
- Initialize → tools/list → tools/call lifecycle
- 9 exposed tools: task.create, task.start, task.complete, task.list, task.get, task.delete, commit, guard.run, judge.evaluate
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
- The client speaks proper JSON-RPC 2.0 and MCP protocol version `2024-11-05`.
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
│  │             │  │  (9 tools)  │  │  Resolution         │  │
│  └─────────────┘  └─────────────┘  └─────────────────────┘  │
│         │                  │                  │               │
│         ▼                  ▼                  ▼               │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────────────┐  │
│  │  initialize │  │  task.*     │  │  GuardManager       │  │
│  │  tools/list │  │  commit     │  │  (per-workdir)      │  │
│  │  tools/call │  │  guard.run  │  │                     │  │
│  │             │  │  judge.eval │  │  Judge (per-workdir)│  │
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
3. **Tool Layer** — 9 tool handlers, each validates arguments, delegates to engine, formats result.
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
  │                   protocolVersion: "2024-11-05",
  │                   capabilities: {tools:{}},
  │                   serverInfo: {name:"gitreins",version:"0.1.0"}}
  │                               │
  ├─ tools/list ───────────────►  │
  │                               │
  │◄─────────────── {result:     │
  │                   tools: [ ... 9 schemas ... ]}
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

### 8.1 task.create

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

### 8.2 task.start

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

### 8.3 task.complete

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
- If `GITREINS_LLM_API_KEY` is set:
  - Creates a `Judge` for the target workdir (if cross-repo).
  - Calls `Judge.evaluate_task(task)`.
  - Returns combined result with task + verdict.
- If no LLM key: returns task with `"note": "LLM not configured — skipping evaluation"`.

**Return shape (with evaluation):**

```json
{
  "task": {
    "id": "login-endpoint",
    "title": "Implement login endpoint",
    "criteria": [...],
    "status": "complete",
    "created_at": "..."
  },
  "verdict": {
    "passed": true,
    "tier1_passed": true,
    "tier2_verdict": "PASS",
    "details": [
      {"criterion": "POST /login returns 200", "status": "PASS", "detail": "..."}
    ]
  }
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
- Evaluation failure → returns `{"task": {...}, "verdict": {"error": "<exception message>"}}` (non-fatal).

---

### 8.4 task.list

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

### 8.5 task.get

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

### 8.6 task.delete

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

### 8.7 commit

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
    }
  },
  "required": ["message"]
}
```

**Behavior:**
1. Check for in-progress tasks in the **server's default workdir** (not cross-repo).
2. If any in-progress tasks exist, reject with error listing their IDs.
3. Run Tier 1 guards via `GuardManager.run_all()`.
4. If guards fail, reject with error and guard summary.
5. Execute `git commit -m <message>` in server workdir.
6. Return commit result (success flag + output).

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
  "error": "Tasks still in progress — complete or delete them first",
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

### 8.8 guard.run

Run Tier 1 static guards (secrets, lint, tests). Optional workdir for cross-repo use.

| Property | Value |
|----------|-------|
| **Name** | `guard.run` |
| **Description** | Run Tier 1 static guards (secrets, lint, tests). Optional workdir for cross-repo use. |

**inputSchema:**

```json
{
  "type": "object",
  "properties": {
    "workdir": {
      "type": "string",
      "description": "Absolute path to the repo to guard. Defaults to the MCP server's workdir."
    }
  }
}
```

**Behavior:**
- Resolves `workdir` (default: server workdir).
- Loads config from `<workdir>/.gitreins/config.yaml` (if exists).
- Creates `GuardManager(wd, config=config)`.
- Runs `gm.run_all()`.
- Returns pass/fail status, workdir, and truncated results (output capped at 500 chars per guard).

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
    }
  ]
}
```

**Error conditions:**
- Config file unreadable → silently ignored, guards run with empty config.
- Guard execution exception → bubbles up as JSON-RPC `-32000` server error.

---

### 8.9 judge.evaluate

Run full evaluation pipeline (Tier 1 + Tier 2) on a task. Caps can be set individually or via legacy `eval_cap` string.

| Property | Value |
|----------|-------|
| **Name** | `judge.evaluate` |
| **Description** | Run full evaluation pipeline (Tier 1 + Tier 2) on a task. Caps can be set individually or via legacy eval_cap string. |

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

## 9. Cross-Repo Workdir

### 9.1 Resolution Pattern

All task tools and `guard.run` / `judge.evaluate` accept an optional `workdir` parameter. The resolution follows this pattern:

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

`guard.run` loads guard configuration from `<workdir>/.gitreins/config.yaml`. If the file is missing or unreadable, guards run with an empty config (defaults).

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
| In-progress tasks blocking commit | commit | `{"error": "Tasks still in progress — complete or delete them first", "tasks": [...]}` |
| Tier 1 guards failed | commit | `{"error": "Tier 1 guards failed — commit blocked", "details": "..."}` |
| Evaluation failure | task.complete | `{"task": {...}, "verdict": {"error": "..."}}` |
| LLM not configured | task.complete | `{"task": {...}, "note": "LLM not configured — skipping evaluation"}` |

### 11.3 Server Exit Codes

| Code | Condition |
|------|-----------|
| `0` | Clean shutdown (SIGTERM or stdin EOF) |
| `1` | Uncaught exception during startup |

---

## 12. Test Strategy

| Layer | File | What it tests | Mock/Real |
|-------|------|---------------|-----------|
| Transport | `tests/test_stdio_transport.py` | Line-delimited JSON, multi-line buffering, brace counting | Mock stdin/stdout |
| Protocol | `tests/test_jsonrpc_dispatch.py` | Method routing, error codes, initialize handshake | Mock handlers |
| Tool | `tests/test_task_tools.py` | task.create/start/complete/list/get/delete with temp workdir | Real TaskManager |
| Tool | `tests/test_commit_tool.py` | commit guard blocking, git execution, error paths | Mock subprocess + real git |
| Tool | `tests/test_guard_tool.py` | guard.run cross-repo config loading, result truncation | Mock GuardManager |
| Tool | `tests/test_judge_tool.py` | judge.evaluate cap priority, cross-repo, error paths | Mock Judge |
| Integration | `tests/test_mcp_lifecycle.py` | Full initialize → tools/list → tools/call → shutdown | Real server in subprocess |

### Test Fixtures

- `fixtures/repo-a/` — git repo with `.gitreins/config.yaml` and tasks
- `fixtures/repo-b/` — empty git repo (no config)
- `fixtures/bad-config/` — git repo with malformed `.gitreins/config.yaml`

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
| Initialize handshake | ✅ Implemented | Returns protocolVersion `2024-11-05` |
| Tools/list endpoint | ✅ Implemented | Returns all 9 tool schemas |
| task.create | ✅ Implemented | With cross-repo workdir |
| task.start | ✅ Implemented | With cross-repo workdir |
| task.complete | ✅ Implemented | Auto-evaluation with LLM fallback |
| task.list | ✅ Implemented | Status filter optional |
| task.get | ✅ Implemented | Error on not found |
| task.delete | ✅ Implemented | Error on not found |
| commit | ✅ Implemented | In-progress check + guard run + git commit |
| guard.run | ✅ Implemented | Cross-repo config loading, result truncation |
| judge.evaluate | ✅ Implemented | Fresh Judge per call, cap priority chain |
| Error code mapping | ✅ Implemented | `-32600`, `-32601`, `-32000` + domain errors |
| SIGTERM handling | ⏳ Stub | Server exits on stdin EOF; explicit SIGTERM handler not implemented |

---

## 15. Verification Checklist

- [ ] `python -m gitreins_mcp.server` starts without error
- [ ] `echo '{"jsonrpc":"2.0","id":1,"method":"initialize"}' | python -m gitreins_mcp.server` returns protocol version
- [ ] `echo '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' | ...` returns 9 tool schemas
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

### 16.1 Happy Path — Create, Start, Complete, Commit

```
$ echo '{"jsonrpc":"2.0","id":1,"method":"initialize"}' | python -m gitreins_mcp.server
{"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}}, "serverInfo": {"name": "gitreins", "version": "0.1.0"}}}

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
gitreins-poc/
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
- [x] Tool Catalog complete (all 9 tools with schemas, behavior, returns, errors)
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
