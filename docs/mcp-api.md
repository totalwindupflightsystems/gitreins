# GitReins MCP API Reference

The GitReins MCP server (`gitreins mcp-server`, source `gitreins_mcp/server.py`) exposes a
**12-tool** surface over JSON-RPC 2.0 on stdio. This reference documents every tool with its
input schema, return shapes, the async judge-job lifecycle, and the error taxonomy.

For the full wire-protocol specification (transport framing, lifecycle, security model) see
[`specs/02-MCP-Protocol.md`](../specs/02-MCP-Protocol.md).

## Transport

- **Protocol:** JSON-RPC 2.0, line-delimited JSON over stdin/stdout (`Content-Type`-free,
  one response per line). Multi-line JSON requests are buffered with a brace-count parser.
- **Handshake:** `initialize` → `notifications/initialized` → `tools/list` → `tools/call`.
- **Protocol version (negotiated):** `initialize` answers with the client's requested
  revision when this server implements it, and otherwise with the newest revision it
  does implement — `2025-11-25` — plus one explanatory line on stderr (stdout stays
  protocol-pure). The supported set, newest first, is `2025-11-25`, `2025-06-18`,
  `2025-03-26`, `2024-11-05`: all four share the session handshake and the tools-only
  capability surface this server offers (`initialize` result carries
  `capabilities: {"tools": {}}`), while the additions each revision brought are
  optional for a tools-only server. The current spec revision, **`2026-07-28`, is
  deliberately not advertised**: it removed the `initialize` handshake, made MCP
  stateless, moved the version into each request's `_meta`, and made `server/discover`
  mandatory. A `2026-07-28` client probes with `server/discover` (answered `-32601`
  here, as any unknown method is) and should then fall back to `initialize` with a
  revision from the list above. Invalid or absent `protocolVersion` values never fail
  the handshake — they get the newest implemented revision and a stderr note naming
  what was requested.
- **Notifications are never answered:** a JSON-RPC message without an `id` gets no
  response, including revision-specific notifications this server does not implement
  (`notifications/cancelled`, `notifications/progress`,
  `notifications/roots/list_changed`). An unknown *method* with an `id` is still a
  `-32601` error.
- **Server info:** `{"name": "gitreins", "version": "<installed version>"}` — the
  `initialize` result reports the **installed release** (`engine.version`, the same
  source `gitreins --version` reads), so a client can trust `serverInfo.version` when
  reasoning about the tool surface. It is not a frozen constant; on a source checkout
  with no installed metadata it falls back to the version declared in `pyproject.toml`.
- **Startup acknowledgement (stderr):** the server writes exactly one line when it
  starts and one when it stops, to **stderr** — stdout stays protocol-pure, because an
  unsolicited line there would corrupt the JSON-RPC stream. The protocol it names is
  the newest revision it implements, not the one a given client negotiated. Shown
  indented (raw output, not an invocation):

      gitreins MCP server <version> — stdio, protocol 2025-11-25 (negotiated per client request), 12 tools, workdir=/path/to/repo
      gitreins MCP server <version> — stdin closed (EOF), exiting 0

  `<version>` is the installed release, so a client log is self-identifying without
  the doc pinning a number that goes stale. A client that sends a request and reads
  nothing back can tell "still starting" from "died before reading stdin" by reading
  its server log. Ask the version without opening the transport with
  `python -m gitreins_mcp.server --version`.
- **Capability discovery:** `tools/list` returns the 12 schemas below. Tool names use
  dotted notation (`task.create`, `guard.run`, `judge.evaluate`).

## Client Quick Start (raw JSON-RPC, no MCP SDK)

The transport is plain line-delimited JSON, so any shell or script can drive it. Three
requests in sequence, one process, with the server's stderr kept visible:

```bash
$ cd /path/to/repo
$ { echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-25","capabilities":{},"clientInfo":{"name":"demo","version":"1.0"}}}';
    echo '{"jsonrpc":"2.0","method":"notifications/initialized"}';
    echo '{"jsonrpc":"2.0","id":2,"method":"tools/list"}';
    echo '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"task.list","arguments":{"status":"pending"}}}';
  } | python -m gitreins_mcp.server
```

A minimal client in Python (same three calls, plus the -32601 check):

```python
import json, subprocess

proc = subprocess.Popen(
    ["python", "-m", "gitreins_mcp.server"],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
)

def call(method, **params):
    req = {"jsonrpc": "2.0", "id": next(counter), "method": method}
    if params:
        req["params"] = params
    proc.stdin.write(json.dumps(req) + "\n")
    proc.stdin.flush()
    return json.loads(proc.stdout.readline())   # one response per line

counter = iter(range(1, 100))
call("initialize", protocolVersion="2025-11-25", capabilities={})
call("notifications/initialized")
print(call("tools/list")["result"]["tools"][0]["name"])
print(call("tools/call", name="task.list", arguments={})["result"]["content"][0]["text"])

proc.stdin.close()          # EOF → the server logs its exit line and exits 0
print(proc.stderr.read())   # startup + exit acknowledgement lines
```

Notes that save a debugging session:

- **One response per line, in order.** Do not read more than one line per request.
- **`notifications/*` never answers.** `notifications/initialized` returns no response
  (`None` internally), so a client that waits for it hangs.
- **Unknown method or tool → `-32601`**, as a JSON-RPC error object (not a tool result);
  see the error taxonomy below.
- **stdout is protocol-only.** Logs, warnings and the startup acknowledgement go to
  stderr; a client that merges the two streams will try to parse prose as JSON.
- **Read `result.protocolVersion` from `initialize`, don't assume it.** A request this
  server does not implement (for example `2026-07-28`) is answered with `2025-11-25`
  and the mismatch is logged on stderr, so a client that hardcodes a revision it asked
  for will disagree with the wire. Send `notifications/initialized` only after reading
  the answer, and treat a notification as fire-and-forget: nothing comes back, by
  design.

## Tool Catalog

### 1. `configure` — hot-reload LLM config at runtime

Sets environment variables and recreates the LLM client + Judge so all subsequent tool
calls use the new config. No config file editing or server restart needed.

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `env` | object | no | Dict of env vars to set, e.g. `{"DEEPSEEK_API_KEY": "sk-..."}` (pushed into `os.environ`) |
| `model` | string | no | Override default model (sets `GITREINS_LLM_MODEL`) |
| `base_url` | string | no | Override API base URL (sets `GITREINS_LLM_BASE_URL`) |
| `provider` | string (`openai`\|`anthropic`) | no | Override provider detection (sets `GITREINS_LLM_PROVIDER`) |

**Returns:** `{"configured": true, "previous": {...}, "current": {...}, "note": "..."}` where
each config snapshot is `{model, provider, api_key_configured, api_key_prefix, base_url, env_keys}`.

### 2. `task.create` — create a task with criteria

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `id` | string | yes | Unique task ID (e.g. `login-endpoint`) |
| `title` | string | yes | Human-readable title |
| `criteria` | array<string> | yes | Completion criteria — each must be verified |
| `workdir` | string | no | Absolute repo path; tasks live in `<workdir>/.gitreins/tasks.yaml`. Defaults to server workdir |

**Returns:** the task dict (`id`, `title`, `status`, `criteria`, timestamps).

### 3. `task.start` — mark a task in-progress

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `id` | string | yes | Task ID to start |
| `workdir` | string | no | Repo containing the task |

**Returns:** the updated task dict.

### 4. `task.complete` — complete a task, trigger evaluation

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `id` | string | yes | Task ID to complete |
| `workdir` | string | no | Repo containing the task |

**Returns:**
- With LLM configured: `{"task": {...}, "job_id": "<id>", "status": "running", "note": "evaluation running in background — poll judge.status"}`
- Without LLM: `{"task": {...}, "note": "LLM not configured — skipping evaluation"}`
- Task not found: `{"error": "Task not found: <id>"}`

### 5. `task.list` — list tasks, optionally filtered

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `status` | string (`pending`\|`in_progress`\|`complete`) | no | Status filter |
| `workdir` | string | no | Repo to list |

**Returns:** `{"tasks": [...]}`.

### 6. `task.get` — fetch one task

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `id` | string | yes | Task ID |
| `workdir` | string | no | Repo containing the task |

**Returns:** the MCP result envelope — `{"content": [{"type": "text", "text": "<json>"}]}` — whose
`text` is the task dict serialized as a JSON string (indent 2). Clients must parse `text`; the
envelope is the wire shape for **every** tool, not just this one. Task not found → the same
envelope with `{"error": "Task not found: <id>"}` as the payload.

### 7. `task.delete` — delete a task

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `id` | string | yes | Task ID to delete |
| `workdir` | string | no | Repo containing the task |

**Returns:** `{"deleted": "<id>"}`, or `{"error": "Task not found: <id>"}`.

### 8. `commit` — the only git commit path

Runs Tier 1 guards first; rejects the commit if guards fail or any task is in-progress.

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `message` | string | yes | Commit message |

**Returns:** `{"committed": true, "output": "..."}` on success.
Blocked by in-progress tasks: `{"error": "Tasks still in progress: <ids> — ...", "tasks": [...]}`.
Blocked by guard failure: `{"error": "Tier 1 guards failed — commit blocked", "details": "..."}`.

### 9. `guard.run` — run Tier 1 static guards

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `workdir` | string | no | Repo to guard (cross-repo supported) |
| `dead_code` | boolean | no | Force dead-code detection (Python AST), overrides config. Default `false` |

**Returns:** `{"passed": bool, "workdir": "...", "results": [{"name", "passed", "output"}]}`
(output truncated to 500 chars per guard).

### 10. `judge.evaluate` — run the full evaluation pipeline (Tier 1 + Tier 2)

**Async by default.** With `wait=false` (default) the evaluation is dispatched to a background
job and the call returns immediately; poll `judge.status` with the returned `job_id`. Pass
`wait=true` for the legacy blocking behavior (full result in the response — risks MCP client
tool-call timeouts on slow suites).

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `id` | string | yes | Task ID to evaluate |
| `workdir` | string | no | Repo containing the task |
| `wait` | boolean | no | Block until done (legacy sync). Default `false` |
| `max_iterations` | number | no | Max LLM reasoning turns (`-1` = unlimited). Tool calls cost `tool_call_weight` each |
| `max_time` | string | no | Wall-clock cap, e.g. `"30s"`, `"5m"`, `"2h"` |
| `max_input_tokens` | string | no | Input token budget, e.g. `"200k"`, `"0.1M"` |
| `max_output_tokens` | string | no | Output token budget, e.g. `"50k"`, `"0.05M"` |
| `tool_call_weight` | number | no | Fraction of an iteration per tool call (default `0.1`) |
| `eval_cap` | string | no | Legacy combined cap `"<iter>/<time>/<in>/<out>"`; individual params take priority |

**Async returns:** `{"job_id": "<id>", "status": "running", "task_id": "<id>", "workdir": "..."}`.
**Sync returns:** full result dict (see `judge.status` below).
**Errors:** `{"error": "LLM not configured — set GITREINS_LLM_API_KEY"}`, or
`{"error": "Task not found: <id>"}` / `{"error": "Task not found: <id> in <workdir>"}`.

### 11. `judge.status` — poll a background evaluation job

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `job_id` | string | yes | Job ID from `judge.evaluate` or `task.complete` |

**Returns:**
- `{"job_id": ..., "status": "running"}` — still evaluating
- `{"job_id": ..., "status": "complete", "result": {"task_id", "passed", "workdir", "tier1_passed", "verdict", "items", "summary"}}`
- `{"job_id": ..., "status": "error", "error": "..."}`

**Durability:** jobs are disk-backed (`~/.local/share/gitreins/jobs/`, override
`GITREINS_JOB_DIR`). They survive MCP server restarts; a `running` job whose process died is
auto-resumed on the next poll. CLI `gitreins judge <id> --async` dispatches are visible here.

### 12. `propagate` — propagate guard config to sibling repos

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `source` | string | no | Source repo path (defaults to server workdir) |
| `targets` | array<string> | yes | Target repo paths to propagate config to |

**Returns:** `{"source": "...", "results": [...]}`.

## Async Judge-Job Lifecycle

```
judge.evaluate(id=..., wait=false)
        │
        ▼
{"job_id": "J-…", "status": "running", "task_id": …, "workdir": …}
        │
        ▼  (background: Tier 1 re-run + Tier 2 LLM evaluation; ~14 min on large suites)
judge.status(job_id=…)  ──▶  {"status": "running"}  (poll until complete/error)
        │
        ▼
{"status": "complete", "result": {task_id, passed, workdir,
  tier1_passed, verdict, items, summary}}
```

- **Serialization:** evaluation jobs run ONE at a time per server instance — concurrent
  judges contend on ports/tmp (LSP integration tests run real servers) and on the shared
  `.gitreins/history` git storage.
- **`task.complete` with an LLM key** also dispatches a background job and returns `job_id`.
- **Job store:** disk-backed (survives restarts), auto-resume of orphaned `running` jobs,
  shared with the CLI (`gitreins judge --async` / `gitreins judge <job_id> --status`).

## Error Taxonomy

### JSON-RPC standard errors

| Code | Name | Condition | Example |
|------|------|-----------|---------|
| `-32600` | Invalid Request | `jsonrpc` field missing or not `"2.0"` | `"Invalid Request: jsonrpc field must be '2.0'"` |
| `-32601` | Method Not Found | Unknown `method` **with an `id`**, or unknown `tool` name | `"Unknown method: foo"` / `"Unknown tool: foo"` |
| `-32000` | Server Error | Unhandled exception in handler | sanitized `<exception message>` |

### Domain errors (in tool result text, NOT JSON-RPC errors)

Returned as successful JSON-RPC responses with `{"error": "..."}` in the result text:

| Condition | Tool(s) | Result shape |
|-----------|---------|--------------|
| Task not found | task.start, task.complete, task.get, task.delete, judge.evaluate | `{"error": "Task not found: <id>"}` |
| Task not found (cross-repo) | judge.evaluate | `{"error": "Task not found: <id> in <workdir>"}` |
| In-progress tasks blocking commit | commit | `{"error": "Tasks still in progress: <ids> — commits are blocked while a task is in_progress because task.complete runs the quality judge against the committed state. Complete them via task.complete, or delete them via task.delete, then retry commit.", "tasks": [...]}` |
| Tier 1 guards failed | commit | `{"error": "Tier 1 guards failed — commit blocked", "details": "..."}` |
| Evaluation failure | task.complete | `{"task": {...}, "verdict": {"error": "..."}}` |
| LLM not configured | task.complete | `{"task": {...}, "note": "LLM not configured — skipping evaluation"}` |
| LLM not configured | judge.evaluate | `{"error": "LLM not configured — set GITREINS_LLM_API_KEY"}` |

### Server exit codes

| Code | Condition |
|------|-----------|
| `0` | Clean shutdown (SIGTERM or stdin EOF) |
| `1` | Uncaught exception during startup |

## Cross-Repo Semantics

Every tool that touches a repo accepts an optional `workdir` (absolute path) and defaults to
the server's workdir. Tasks are stored in `<workdir>/.gitreins/tasks.yaml`; guard config is
loaded from `<workdir>/.gitreins/config.yaml`; `judge.evaluate` on a non-default workdir
builds a fresh `Judge` with a captured `EvalCap`.
