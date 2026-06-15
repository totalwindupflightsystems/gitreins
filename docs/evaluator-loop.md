# Agentic Evaluator — Implementation Guide

The evaluator is **not** a single LLM call. It's an agentic loop — the LLM iterates, calling tools and incorporating results, until it has enough evidence to deliver a verdict.

## Loop Architecture

```
LOAD CONTEXT → LLM CALL → TOOL CALL? → (yes) Execute → Back to LLM
                                     → (no) → VERDICT: COMPLETE / INCOMPLETE
```

The loop terminates when the LLM issues no tool calls — it has decided it has sufficient evidence.

## Max Iterations

Default: **15** (configurable via `config.yaml` → `evaluator.max_iterations`, max 20).

When the LLM exhausts all iterations without delivering a verdict, the evaluator appends a final forced prompt:

```
"You've reached the maximum number of tool calls. Deliver your final verdict NOW."
```

If the final call also fails, a default `INCOMPLETE` verdict is returned.

## Evaluation Tools (7)

All 7 tools are defined in `engine/evaluator.py` as OpenAI function-calling definitions. Each tool call returns a JSON dict.

### Repo Inspection (5 tools)

| Tool | Signature | Description |
|---|---|---|
| `read_file` | `(path: str, offset?: int, limit?: int) → dict` | Read any file in the working tree with optional line-range support |
| `run_command` | `(cmd: str) → dict` | Run a shell command (tests, lint, build) with 30s timeout |
| `search_pattern` | `(regex: str, file_glob?: str) → dict` | Search the codebase for a Python regex pattern |
| `read_diff` | `() → dict` | Show staged and unstaged git diff summaries |
| `get_task_item` | `(id: str) → dict` | Fetch a task's full definition and criteria |

### Scratch / Sandbox (2 tools)

| Tool | Signature | Description |
|---|---|---|
| `sandbox_write` | `(key: str, content: str) → dict` | Write to an in-memory scratch dict |
| `sandbox_read` | `(key: str) → dict` | Read from an in-memory scratch dict |

**`mcp_call` is NOT implemented.** The MCP allowlist exists in config but the evaluator does not expose an MCP bridge tool. Only these 7 tools are available.

---

### Tool Details

#### `read_file(path, offset?, limit?)`

Reads a file relative to the repo root.

- **Path safety**: Rejects paths that escape the working tree via `os.path.realpath` check.
- **Large files**: Auto-truncates to first 400 lines when total chars > 12KB and no range was requested. Shows a truncation notice with total_lines and total_chars.
- **offset** (1-indexed): Start from a specific line. If `offset > total_lines`, returns an error.
- **limit**: Max lines to return. 0 (default) = no limit.

```json
// Read a specific range
{"path": "src/routes.py", "content": "...", "total_lines": 340,
 "total_chars": 12300, "shown_lines": 50, "has_more": true}

// File not found
{"error": "File not found: missing.py"}

// Path escape attempt
{"error": "Path outside working tree: ../../etc/passwd"}
```

#### `run_command(cmd)`

Runs a shell command with `subprocess.run(shell=True)`.

- **Timeout**: 30 seconds. Returns error on expiry.
- **Output truncation**: Capped at 4KB. Shows `[truncated]` notice if exceeded.
- **Return fields**: `cmd`, `exit_code`, `output` (or `error` on failure).

```json
{"cmd": "pytest tests/", "exit_code": 0,
 "output": "===== 5 passed in 0.45s ====="}

{"cmd": "pytest tests/", "exit_code": 1,
 "output": "FAILED test_auth.py::test_login ... AssertionError"}

{"cmd": "sleep 60", "error": "Command timed out after 30s"}
```

#### `search_pattern(regex, file_glob?)`

Grep-style regex search using `os.walk`.

- **Skip dirs**: `.git`, `venv`, `.venv`, `node_modules`, `__pycache__`, `.gitreins-sandbox`, `.pytest_cache` (and any hidden `.` dirs).
- **File size skip**: Files > 500KB are silently skipped.
- **200-match cap**: Results truncated at 200 matches with a `[truncated]` notice.
- **file_glob**: Optional `fnmatch` filter (e.g., `"*.py"`).

```json
{"regex": "def handle_", "matches": [
  "src/handlers.py:12: def handle_login():",
  "src/handlers.py:45: def handle_logout():"
], "count": 2}

{"regex": "(invalid", "error": "Invalid regex: (invalid"}
```

#### `read_diff()`

Runs `git diff --cached --stat` (staged) and `git diff --stat` (unstaged). No parameters.

```json
{"staged": "src/routes.py | 3 ++-\n1 file changed, 2 insertions(+), 1 deletion(-)",
 "unstaged": "(no unstaged changes)"}
```

#### `get_task_item(id)`

Returns the full task dict from the in-memory task index. Tasks are registered at the start of `evaluate()`.

```json
{"id": "task-42", "title": "Add login endpoint",
 "criteria": ["POST /login returns 200 on valid credentials",
              "POST /login returns 401 on invalid credentials",
              "Password stored as bcrypt hash"]}
```

#### `sandbox_write(key, content)`

Writes to `self._sandbox: dict[str, str]` — a plain in-memory dict. Cleared at the start of every `evaluate()` call.

```json
{"key": "checked-ready", "written": 5}
```

#### `sandbox_read(key)`

Reads from `self._sandbox`. Values > 4KB are truncated with a notice.

```json
{"key": "checked-ready", "content": "... evidence ..."}
```

---

## Deduplication

The evaluator tracks calls to prevent the LLM from repeating itself:

| Tool | Dedup Key | Tracked In |
|---|---|---|
| `read_file` | `path` string | `self._files_read: set[str]` |
| `run_command` | `cmd` string | `self._commands_run: set[str]` |
| `search_pattern` | `regex` string | `self._searches_done: set[str]` |

When the LLM repeats a call, the tool **still executes**, but a `_dedup_warning` field is injected into the result dict:

```json
{"path": "src/routes.py", "content": "...",
 "_dedup_warning": "You already used read_file with these arguments. See previous result above. Move on to unchecked criteria."}
```

The system prompt also reinforces this:

> Do not re-read the same file twice. Do not re-run the same command. Do not search for the same pattern twice.

`read_diff`, `get_task_item`, and sandbox tools are **not** dedup-tracked (they are idempotent or cheap).

## Verdict Parsing

When the LLM stops making tool calls, its response is parsed via a 3-strategy fallback chain in `_parse_verdict()`:

### Strategy 1: Strip Markdown Fences

If the response starts with ` ``` `, remove the fence markers (optional `json` language tag):

```
```json
{"verdict":"COMPLETE",...}
```
```

Stripped to raw JSON.

### Strategy 2: JSON Boundaries

Find the first `{` and last `}` in the cleaned string. Attempt `json.loads()` on the extracted substring.

Validates:
- `verdict` must be `"COMPLETE"` or `"INCOMPLETE"` (defaults to `INCOMPLETE` if invalid)
- `items` must be present
- Each item's `status` must be `"PASS"` or `"FAIL"` (defaults to `FAIL`)

### Strategy 3: Keyword Fallback

If JSON parsing fails, search the raw text for:
- `"complete"` (case-insensitive) → `COMPLETE`
- `"all criteria"` + `"pass"` → `COMPLETE`
- Everything else → `INCOMPLETE`

The fallback logs a warning and includes the raw text in the summary.

### Verdict Schema

```json
{
  "verdict": "INCOMPLETE",
  "items": [
    {"criterion": "error-handling", "status": "FAIL",
     "detail": "No 401 for invalid credentials — routes.py:65 missing"},
    {"criterion": "tests", "status": "FAIL",
     "detail": "Missing 3 required tests — test_login.py only has happy-path"},
    {"criterion": "login-endpoint", "status": "PASS",
     "detail": "POST /login confirmed at routes.py:42-68"}
  ],
  "summary": "2 of 3 criteria fail"
}
```

## State Lifecycle

1. **`evaluate()` called** — `_sandbox`, `_files_read`, `_commands_run`, `_searches_done` are cleared.
2. **Task prompt built** — criteria are injected as numbered items. LLM is told to call `get_task_item()` first.
3. **Loop runs** — `self.max_iterations` turns. Each turn: LLM call → tool execution → append results.
4. **Verdict or exhaustion** — LLM stops tool calling → verdict parsed. Or max iterations hit → forced final prompt.
5. **Return** — `Verdict` dataclass with `verdict`, `items[]`, `summary`.

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
