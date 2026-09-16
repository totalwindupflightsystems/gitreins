# Agentic Evaluator — Implementation Guide

The evaluator is **not** a single LLM call. It's an agentic loop — the LLM iterates, calling tools and incorporating results, until it has enough evidence to deliver a verdict.

## Loop Architecture

```
LOAD CONTEXT → LLM CALL → TOOL CALL? → (yes) Execute → Back to LLM
                                     → (no) → VERDICT: COMPLETE / INCOMPLETE
```

The loop terminates when the LLM issues no tool calls — it has decided it has sufficient evidence.

## Caps: iterations, time, tokens

Caps come from `engine/config.py` (`GitReinsDefaults`) and are overridable per
repo in `.gitreins/config.yaml` under `evaluator:` — or per call for MCP
`judge.evaluate`. They are checked **before** each call, so a run can end
slightly above its cap (the final call is allowed through).

| Cap | Default | Meaning |
|---|---|---|
| `max_iterations` | `100` | LLM reasoning turns; every tool call additionally costs `tool_call_weight` |
| `max_input_tokens` | `10_000_000` (10M) | prompt budget per context window; `"200k"` / `"1.5M"` forms accepted |
| `max_output_tokens` | `131_072` (128K) | output-token budget |
| `max_time` | unset | wall-clock cap (`30s`, `5m`, `2h`); unset = unlimited |
| `max_tokens_per_call` | `16384` | per-request output cap (separate from the session budget) |
| `tool_call_weight` | `0.1` | iterations charged per tool call |
| `compaction_threshold` | `0.90` | compact the conversation once 90% of the prompt budget is used |

### When a cap is hit

There is no forced "deliver your verdict now" prompt. On cap exhaustion the
evaluator makes a best-effort recovery instead:

1. **Partial verdict from the sandbox.** If the LLM recorded criterion
   evidence under the scratch keys `verified_<index>`, that evidence is turned
   into a verdict: `PASS…` evidence → PASS, anything else → FAIL, and criteria
   with no `verified_<index>` key become FAIL with *"Not verified — evaluation
   terminated before this criterion was checked"*. The run is COMPLETE only if
   **every** criterion came back PASS — one PASS plus a FAIL is INCOMPLETE, so
   a capped run can never pass a task it did not finish verifying.
2. **Otherwise INCOMPLETE** with `summary` = `Cap exceeded: <reason>`, which
   names the cap and how much was used.

### Context compaction

Compaction is proactive: when the prompt reaches `compaction_threshold` of
`max_input_tokens`, the conversation is rebuilt (at most `MAX_COMPACTIONS = 3`
per evaluation — the same ceiling covers provider context-length errors). A
compaction resets the per-turn loop counter and the token counters
(`reset_context_tracking`), but **not** the iteration/time caps: those span the
whole evaluation, so a compacted run can issue more LLM calls than
`max_iterations` while still being capped overall. Judge token telemetry
(.gitreins/usage.jsonl, below) shows the reset as a counter that drops.

## Mandatory test verification (hard rule)

The judge is not allowed to PASS a criterion on code reading alone. For any
criterion that mentions tests, build, lint, type-checking, or "the code
works/runs", the prompt requires actual command output:

> You MUST NOT report PASS on any criterion that mentions tests, build, lint,
> type-checking, or "the code works/runs" unless you have ACTUAL command output
> proving it. Reasoning from the code alone is NOT sufficient — the suite can be
> red while the code looks fine (cached test results and pre-loaded context are
> not evidence of a passing run).

The prescribed sequence is: read `guards.test_command` from
`.gitreins/config.yaml` (falling back to a language default), run that command
**fresh** (cache-defeating flags such as `go test -count=1 ./...` where the
toolchain has one), then quote the `exit_code` and the decisive output line in
the criterion's `detail`. A detail of just "tests pass" with no command output
is a FAIL, and the rule is explicit that a criterion whose verification should
have run tests but whose detail shows no output **must be marked FAIL, not
PASS**. The only exception is a project with no test suite at all (docs-only
repo, `test_command: true`), which must be recorded as
*"no test suite — verified <command or none>"*.


## Evaluation Tools (12)

All 12 tools are defined in `engine/evaluator.py` (`EVALUATOR_TOOLS`) as OpenAI
function-calling definitions. Each tool call returns a JSON dict. The judge
advertises **11** of them by default: `read_static_analysis` is dropped from the
schema unless `evaluator.static_analysis_diagnostics: true` is set.

### Repo Inspection (5 tools)

| Tool | Signature | Description |
|---|---|---|
| `read_file` | `(path: str, offset?: int, limit?: int, byte_offset?: int, byte_limit?: int, mode?: str) → dict` | Read any file in the working tree; line-based ranges by default, byte-level access with `mode="bytes"` |
| `run_command` | `(cmd: str) → dict` | Run a shell command (tests, lint, build) with a 30s timeout |
| `search_pattern` | `(regex: str, file_glob?: str) → dict` | Search the codebase for a Python regex pattern |
| `read_diff` | `() → dict` | Show staged and unstaged git diff summaries |
| `get_task_item` | `(id: str) → dict` | Fetch a task's full definition and criteria |

### Diagnostics (4 tools)

| Tool | Signature | Description |
|---|---|---|
| `read_static_analysis` | `(path?: str) → dict` | Type errors and warnings from the configured analyzers (mypy and friends) |
| `read_lsp_diagnostics` | `() → dict` | LSP findings collected during the Tier 1 guard run — file, line, severity, message (undefined names, syntax errors, type mismatches, import errors) |
| `detect_dead_code` | `() → dict` | AST-based Python dead code: unreachable code, unused functions/imports, empty functions |
| `skylos_scan` | `() → dict` | Multi-language dead code / AI-mistake scan via the `skylos` binary (unused symbols, unreachable code), returned with a letter grade |

### Security / Sandbox (3 tools)

| Tool | Signature | Description |
|---|---|---|
| `scan_security` | `(path?: str) → dict` | Deterministic, syntax-aware ast-grep scan against the bundled CodeRabbit essential rules — hardcoded secrets, weak crypto, SQL injection, XSS, unsafe deserialization, and similar, without relying on the LLM |
| `sandbox_write` | `(key: str, content: str) → dict` | Write to an in-memory scratch dict |
| `sandbox_read` | `(key: str) → dict` | Read from an in-memory scratch dict |

**`mcp_call` is NOT implemented.** The MCP allowlist exists in config but the
evaluator does not expose an MCP bridge tool. Only these 12 tools (11 with
static analysis off) are available.

Tools backed by an external binary degrade with an `error` field rather than
skipping silently: `skylos_scan` returns `{"error": "skylos not installed — pip
install skylos"}`, and `scan_security` names either `ast-grep` or the missing
rule set (`~/.gitreins-rules/rules`, cloned from `coderabbitai/ast-grep-essentials`).
An error result is evidence the check did not run — never treat it as a clean
scan.

The scratch dict is also how a capped run recovers: an LLM that stores
`verified_<index>` entries there before the cap is hit gets a partial verdict
(see *When a cap is hit* above).


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

#### `read_static_analysis(path?)`

Runs the analyzers configured under `guards.static_analysis_tools` against a
directory (the given path's parent, or the repo root) and returns their
diagnostics.

```json
{"diagnostics": [{"tool": "mypy", "file": "src/x.py", "line": 12, "severity": "error",
                  "message": "Incompatible types", "code": ""}],
 "count": 1, "tools_used": ["mypy"]}
```

Two guards on this tool: it returns
`{"error": "static_analysis_diagnostics is not enabled in .gitreins/config.yaml"}`
when the evaluator toggle is off, and
`{"diagnostics": [], "note": "No static analysis tools configured"}` when no
tool is configured. A tool that crashes contributes a diagnostic with
`severity: "error"` rather than aborting the call.

#### `read_lsp_diagnostics()`

Returns the diagnostics LSP tools (`pylsp` and friends) produced during the
Tier 1 guard run — no new LSP check is triggered, this is the cached Tier 1
result.

```json
{"diagnostics": [{"file": "engine/x.py", "line": 9, "severity": "error",
                  "message": "Undefined name 'foo'"}], "count": 1}
```

An empty list means LSP did not run for this evaluation (no server on PATH, or
the lane was skipped) — absence of diagnostics is not evidence of clean code.

#### `detect_dead_code()`

AST-based Python dead-code scan (reuses `engine.dead_code.DeadCodeDetector`),
grouped by category with at most 20 findings per category:

```json
{"total_findings": 3, "passed": false,
 "by_category": {"unused_function": {"count": 2, "items": [...]}}}
```

#### `skylos_scan()`

Shells out to the `skylos` binary (120s timeout, `--no-grep-verify`) for a
multi-language dead-code / AI-mistake scan, returning a letter grade plus
unused functions, unused imports and dead symbols. Missing binary →
`{"error": "skylos not installed — pip install skylos"}`; a nonzero exit →
`{"error": "skylos exited <code>", "stderr": "..."}`.

#### `scan_security(path?)`

Deterministic ast-grep scan against the CodeRabbit essential rule set — one
ast-grep invocation per rule file, because bulk loading aborts on rules stock
ast-grep cannot parse (those are skipped; the rest are aggregated). Findings
are SARIF-derived (`file`, `path`, `line`, `message`, `rule`). Two
infrastructure errors are reported instead of an empty result:
`{"error": "ast-grep not installed — cargo install ast-grep"}` and
`{"error": "gitreins security rules not installed (~/.gitreins-rules/rules —
clone coderabbitai/ast-grep-essentials)"}`.

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

`read_diff`, `get_task_item`, and sandbox tools are **not** dedup-tracked (they are idempotent or cheap). The diagnostics and security tools
(`read_static_analysis`, `read_lsp_diagnostics`, `detect_dead_code`,
`skylos_scan`, `scan_security`) are not dedup-tracked either — they re-run on
every call, and `skylos_scan` / `scan_security` shell out, so a repeat costs
real time. Only the three tracked tools inject a `_dedup_warning`.

## Judge token telemetry (`.gitreins/usage.jsonl`)

The pipeline appends one JSON line per evaluation step to
`<workdir>/.gitreins/usage.jsonl` (best-effort — a write error is swallowed and
never fails the evaluation):

```json
{"ts": 1757973600.42, "tokens_in": 41250, "tokens_out": 1863, "cache_read": 0, "cache_write": 0, "step": "ai_eval"}
```

The counters come from the evaluation's `EvalCap` and are cumulative for the
current context window, so:

- sum line-to-line **deltas** to get spend — the last line is a run total only
  when no compaction reset the window;
- **counters reset on compaction** (`reset_context_tracking`), so a later line
  can be smaller than an earlier one;
- the file carries no task id, model, or credentials — join it with
  `.gitreins/history/<date>/<hash>/verdict.json` by timestamp when you need
  per-task attribution.

The file is runtime state (gitignored by `install`/`init`), and it exists
because GitReins calls its own LLM client: judge spend never appears in the
telemetry of the agent that invoked the judge.

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
3. **Loop runs** — one LLM call per turn, each turn appending tool results; iterations and tool calls are charged against `EvalCap` (see *Caps* above).
4. **Verdict or exhaustion** — the LLM stops calling tools → verdict parsed. Or a cap is hit → partial verdict from the sandbox if any criterion was recorded, otherwise INCOMPLETE with `Cap exceeded:`.
5. **Return** — `Verdict` dataclass with `verdict`, `items[]`, `summary`.

## Tier System

```
Tier 1: Static Guards (no LLM)
  ├── secrets (gitleaks and/or the built-in scanner)
  ├── lint
  ├── tests (full or diff mode)
  └── static analysis / LSP (only when configured and the tool is on PATH)
      ↓ PASS (DEGRADED skips are named; they block unless guards.allow_skips)
Tier 2: Agentic Evaluator (LLM)
  ├── reads code
  ├── runs tests
  ├── searches patterns
  └── delivers verdict
      ↓ PASS
git commit ✓
```

Tier 1 runs first because it's fast and free. Tier 2 only fires if Tier 1 passes.

## Tier 1 / Guard Parity Contract (DF-GITREINS-POC-16)

A green Tier 1 must mean the same thing the repo's own gate means:

> **`gitreins judge` Tier 1 grades the same check set, with the same commands,
> that `gitreins guard` grades on the identical tree.** If the guard would run
> lint and tests for this repo, Tier 1 runs lint and tests.

How it is enforced:

1. **One language-detection source of truth — `engine/lang_detect.py`.** The
   signature-file table, the source-extension fallback and the
   `language -> (lint_command, test_command)` map live there and nowhere else.
   Consumers: the judge's `tier1_plan` (`engine/pipeline.py`), the guard
   (`engine/guard_manager.py`) and `gitreins init`/`install`
   (`gitreins/cli.py`). Three independent detectors used to disagree — a plain
   `.py` repo was Python to `init` and language-less (secrets-only) to the judge.
2. **Detection order:** signature file → source-extension fallback (tracked +
   untracked-not-ignored files via `git ls-files`, else a bounded `os.walk`) →
   nothing. Test directories never satisfy the fallback (a tree whose only
   sources are tests has no product code to grade); tool/build dirs and
   dot-directories are pruned.
3. **Commands:** the tests step runs `guards.test_command` when configured,
   otherwise the detected language's default, resolved through the guard's own
   `_resolve_test_command` (so a configured `uv run pytest` on a machine
   without `uv` degrades to `python -m pytest` in both engines). `guards.lint:
   false` and `guards.test_timeout` are honoured exactly as before.
4. **Verdict semantics match the guard:** a missing linter binary is a SKIP
   (guard: "No linter found — skipped"), not a failure; a missing test runner is
   a FAILURE (non-zero exit).

### Loud degradation

When Tier 1 ends up *narrower* than the guard gate — genuinely nothing
detectable — the run is marked degraded instead of passing silently:

* the CLI prints a warning naming the skipped checks and the reason:

```
WARNING: coverage is secrets-only — lint, tests did not run (no language
detected in <workdir>); run `gitreins guard` for the full gate
```

* `verdict.json` carries the machine-readable marker on the tier1 stage:

```json
"tier1": {
  "passed": true,
  "coverage": "secrets-only",
  "degraded": true,
  "skipped_steps": ["lint", "tests"],
  "degradation_reason": "no language detected in /path/to/repo"
}
```

A non-degraded stage carries `"coverage": "secrets+lint+tests"` (or
`"secrets+tests"` when `guards.lint: false`) and no `degraded` key. The marker is
**additive**: a degraded stage may still be `passed: true` — that combination is
exactly what the marker is for.

### Known residual divergence

`pytest` exit 5 ("no tests collected") is a pass-with-warning in the guard
(`_pytest_no_tests_benign`) but a failure in the judge's tests step, which
grades the raw exit code. Tier 1 is therefore *stricter* than the guard on a
test-less Python repo — never more permissive, which is the direction that
matters for a green verdict.
