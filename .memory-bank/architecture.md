# GitReins Architecture

> **What this doc is:** A walk-through of the running system, grounded
> in the actual source. Every claim is followed by the file:line that
> proves it. When the design doc and the code disagree, the code wins.

**Repository:** `~/gitreins-poc/`
**Version:** v0.1.0 (PoC, all 12 work items complete)
**Stack:** Python 3.10+, `mcp`, `pyyaml`, `requests` — 3 deps total.

---

## 1. The big picture

```
PRIMARY AI AGENT (Pi / Claude / Hermes / Codex)
        │  MCP stdio (JSON-RPC 2.0)
        ▼
MCP SERVER (gitreins_mcp/server.py)
        │   9 tools exposed
        ▼
GITREINS ENGINE
  ┌──────────┬──────────┬──────────┬──────────┬──────────┬──────────┐
  │  llm.py  │evaluator │ guard    │ pipeline │  task    │  judge   │
  │          │   .py    │ _manager │   .py    │ _manager │   .py    │
  │ HTTP to  │ agentic  │   .py    │ stage    │   .py    │ Tier1→2  │
  │  any     │  loop    │ Tier 1   │ runner   │ YAML     │ orchestr │
  │  OpenAI- │ +9 tools │ static   │ + templ  │ backing  │          │
  │  compat  │          │ checks   │          │          │          │
  └──────────┴──────────┴──────────┴──────────┴──────────┴──────────┘
        │                                       │
        ▼                                       ▼
   LLM API (any)                        .gitreins/  ←→  .git
                                  (config.yaml, tasks.yaml,
                                   history/<date>/<hash>/)
                                        ▲
                                        │
                              gitreins/install writes
                              .git/hooks/pre-commit
```

A primary coding agent (Pi, Claude Code, Hermes, Codex) talks to the
MCP server over stdio using JSON-RPC 2.0. The MCP server delegates to
the engine. The engine reads its config and tasks from `.gitreins/`,
calls the LLM over HTTP, and (optionally) writes a verdict to
`.gitreins/history/`. Git hooks installed by `gitreins/install`
provide a back-stop against direct `git commit` bypass.

---

## 2. The evaluator loop (the only part that uses the LLM)

**File:** `engine/evaluator.py` — 663 lines.

The evaluator is **not** a single LLM call. It's a tool-calling loop.
The system prompt is in `EVALUATOR_SYSTEM_PROMPT` (lines 31-62) and
the 9 tool definitions are in `EVALUATOR_TOOLS` (lines 64-177).

### Loop mechanics (`evaluate()`, lines 212-323)

```
for iteration in range(self.max_iterations):  # default 15, max 20
    response = self.llm.chat(messages, tools=EVALUATOR_TOOLS)

    if not response.tool_calls:
        return self._parse_verdict(response.content)   # LLM done

    for tc in response.tool_calls:
        result, was_dup = self._execute_tool_with_dedup(tc)
        messages.append({role: tool, ...})

# exhausted — force final verdict
```

The loop terminates when the LLM stops making tool calls (i.e., it
decides it has enough evidence). There is no "looks good, done"
heuristic — the LLM has to deliver the JSON verdict itself.

### 9 tools (engine/evaluator.py:64-177)

| # | Tool | Signature | What it does |
|---|------|-----------|--------------|
| 1 | `read_file` | `(path, offset?, limit?)` | Read any file. Path-escape protected (`os.path.realpath` check, line 390). Large files auto-truncate to first 400 lines (line 415). |
| 2 | `run_command` | `(cmd)` | Run shell cmd. 30s timeout. Output truncated to 4KB. |
| 3 | `search_pattern` | `(regex, file_glob?)` | Grep via `os.walk`. 200-match cap. Skips `.git`, `venv`, `node_modules`, `__pycache__`, `.pytest_cache`. |
| 4 | `read_diff` | `()` | `git diff --cached --stat` + `git diff --stat`. |
| 5 | `get_task_item` | `(id)` | Fetch full task from in-memory index (line 245). |
| 6 | `sandbox_write` | `(key, content)` | Write to `self._sandbox: dict[str, str]`. |
| 7 | `sandbox_read` | `(key)` | Read from sandbox. |
| 8 | `detect_dead_code` | `()` | AST-based Python dead code scan (delegates to `engine/dead_code.py`). |
| 9 | `skylos_scan` | `()` | Multi-language dead code + AI-mistake scan via `skylos` CLI (JSON output, opt-in). |

> The architecture doc (`docs/evaluator-loop.md`) lists 7 tools because
> it predates the skylos/dead-code additions. The real tool count is 9.

### Dedup (lines 325-349)

The evaluator tracks per-call to stop the LLM from repeating itself:

- `read_file` → dedup key = `path`
- `run_command` → dedup key = `cmd`
- `search_pattern` → dedup key = `regex`

On a repeat, the tool **still executes** (so the LLM gets the answer
back) but a `_dedup_warning` field is injected into the result:
```
"_dedup_warning": "You already used read_file with these arguments.
 See previous result above. Move on to unchecked criteria."
```

### Verdict parsing (lines 530-589)

3-strategy fallback chain:

1. Strip markdown fences (```...```) if present.
2. Find first `{` and last `}` — `json.loads` the substring.
3. Keyword fallback: `"complete"` or `"all criteria" + "pass"` → COMPLETE; else INCOMPLETE.

Required fields validated: `verdict` ∈ {COMPLETE, INCOMPLETE}, each
`item.status` ∈ {PASS, FAIL}, every task criterion must have an
item. Defaults on failure: verdict → INCOMPLETE, status → FAIL.

### Sandbox

`self._sandbox: dict[str, str]` — in-memory, cleared at the start of
every `evaluate()` call. NOT a filesystem directory. The
`docs/sandbox.md` design (which talks about `.git/gitreins-sandbox/`
and an auto-commit to a `gitreins` branch) is **superseded** — the
real implementation is an in-memory dict, as flagged in the
implementation note at the top of that file.

---

## 3. The guard pipeline (Tier 1, no LLM)

**File:** `engine/guard_manager.py` — 343 lines.

```
run_all()                    — engine/guard_manager.py:58
   ├── _check_secrets()      — :80  (gitleaks or built-in)
   ├── _check_lint()         — :194 (ruff or flake8)
   ├── _check_tests()        — :227 (pytest)
   ├── _check_dead_code()    — :252 (engine/dead_code.py)
   └── _check_skylos()       — :278 (opt-in: needs pip install skylos)
```

Each guard returns a `GuardResult(name, passed, output, error)`. The
final `Tier1Result.passed = all(r.passed for r in results)`.

### Guard toggles (lines 50-56)

```python
self._enabled = {
    "secrets":    self.config.get("guards", {}).get("secrets", True),
    "lint":       self.config.get("guards", {}).get("lint", True),
    "tests":      self.config.get("guards", {}).get("tests", True),
    "dead_code":  self.config.get("guards", {}).get("dead_code", True),
    "skylos":     self.config.get("guards", {}).get("skylos", False),  # opt-in
}
```

The defaults: secrets/lint/tests/dead_code ON, skylos OFF (because
Skylos requires `pip install skylos` and we don't want to fail
Tier 1 just because Skylos isn't there).

### Secrets scanner (lines 80-192)

Two layers:

1. **gitleaks** if installed (`gitleaks detect --source . --no-git --verbose`).
2. **Built-in fallback** when gitleaks is not on `$PATH`. The built-in
   scanner uses **danger patterns** (high-confidence secrets) and
   **whitelist patterns** (common false positives).

Danger patterns include (line 109-129):
- `api_key = "abc123..."` (literal assignment with 20+ char value)
- `-----BEGIN ... PRIVATE KEY-----`
- `ghp_<36+ chars>` (GitHub PAT)
- `glpat-<20+ chars>` (GitLab PAT)
- `sk-<32+ chars>` (OpenAI key)
- `AKIA<16 chars>` (AWS access key)
- hardcoded JWTs, passwords, secrets

Whitelist patterns (line 132-140) ignore:
- `os.getenv`, `os.environ[...]`, `request.form`, `request.args` (env/form access)
- `${VAR}` shell substitution
- `{{ var }}` template variables
- `PASSWORD = ""` (empty password)
- `EXAMPLE`, `PLACEHOLDER`, `TODO`, `FIXME` markers
- `jwt.encode`, `jwt.decode`, `b64encode` (JWT construction, not hardcoded)
- `generate`, `random`, `uuid`, `hash` (value-generation calls)

The whitelist is what prevents the false-positive class of bugs.
See `findings/finding-004-precommit-guard-false-positives.md` for
the specific incident.

### Lint (lines 194-225)

Prefers `ruff`, falls back to `flake8`. Operates on staged Python
files only (`git diff --cached --name-only --diff-filter=ACM`).
Skips cleanly if no linter is installed.

### Tests (lines 227-250)

Runs the command in `config.guards.test_command` (default:
`pytest -x --tb=short`). Skips cleanly if pytest isn't installed.
Output capped at 2KB; on failure keeps the **last** 2KB (tail
truncation) so the error context survives.

### Dead code (lines 252-276)

Delegates to `engine/dead_code.py:48-280` — an AST-based detector
that finds:

- **Unreachable code** — statements after `return`/`raise`/`break`/`continue`
  in the same function body (skipping docstrings).
- **Empty functions** — body is just `pass`/docstring.
- **Unused imports** — module-level imports that no `Name` or
  `Attribute.value` references.
- **Unused functions** — functions defined but never called (skips
  dunders, `_*` private, `test_*` tests, and an explicit
  `WHITELIST_FUNCTIONS` set in `dead_code.py:51-63`).

### Skylos (lines 278-343)

`skylos <workdir> --format json --no-grep-verify`, then parses
`unused_functions`, `unused_imports`, `unused_classes`, and
`definitions` (anything with `dead: true`). The grade letter
(A/B/C/D/F) is surfaced in the output but doesn't gate the commit —
only the `findings` count does.

All "soft errors" (skylos not installed, output unparseable, timeout)
return `passed=True` so the guard never blocks when Skylos itself
fails.

---

## 4. Task lifecycle

**File:** `engine/task_manager.py` — 148 lines.

Tasks live in `.gitreins/tasks.yaml` as a YAML list of records:

```yaml
tasks:
  - id: "login-endpoint"
    title: "Implement POST /login endpoint"
    criteria:
      - "Accepts email+password as JSON body"
      - "Returns JWT token on success"
      - "Returns 401 on invalid credentials"
    status: pending    # pending | in_progress | complete
    created_at: "2026-06-12T04:40:58+00:00"
    completed_at: null  # set on complete()
```

State machine: `pending → in_progress → complete`. The `Task` dataclass
(lines 26-33) has `id`, `title`, `criteria: list[str]`, `status`,
`created_at`, `completed_at`.

`TaskManager` is the only writer of `tasks.yaml`. `_save()` (line 66)
sorts keys with `sort_keys=False` and uses `default_flow_style=False`
to preserve human-readable formatting.

The `criteria: list[str]` is the contract that the agentic evaluator
checks against. **The criteria ARE the guardrails** — there is no
separate rubric or score.

---

## 5. Judge orchestrator (the Tier 1 → Tier 2 glue)

**File:** `engine/judge.py` — 134 lines.

```python
def evaluate_task(self, task: Task) -> JudgeResult:
    config = load_pipeline_config(self.workdir)
    if config.get("pipeline", {}).get("stages"):
        return self._run_pipeline(task, config)
    else:
        return self._run_legacy(task)
```

`_run_legacy` (lines 69-94) is the simple "Tier 1 first, Tier 2 only
if Tier 1 passes" path. `_run_pipeline` is the YAML-driven path
(used when `.gitreins/config.yaml` has a `pipeline:` section).

For pre-commit specifically, there's a `run_precommit()` method
(line 96) that runs the pipeline with `trigger="pre-commit"` — this
filters out the Tier 2 `ai_eval` stage, which is `on: [pre-eval]`
only by default.

---

## 6. The pipeline engine (configurable evaluation)

**File:** `engine/pipeline.py` — 428 lines.

Pipelines are defined in `.gitreins/config.yaml`:

```yaml
pipeline:
  stages:
    - id: tier1
      parallel: true               # steps run concurrently
      on: [pre-commit, pre-eval]   # triggers
      steps:
        - id: secrets
          type: script
          run: "gitleaks detect --source . --no-git"
          on_fail: continue
    - id: tier2
      type: ai_eval
      on: [pre-eval]
      condition: "true"            # see _check_condition()
      max_iterations: 16
```

### Key concepts

- **Sequential vs parallel**: a list with no `parallel: true` is
  sequential. A stage with `parallel: true` runs all its `steps` in a
  `ThreadPoolExecutor` (line 193-205).
- **Triggers**: `on: [pre-commit, pre-eval]` filters which stages
  run for a given call. A `pre-commit` run skips Tier 2.
- **Conditions**: `condition: "stage.tier1.any_failed"`, `"task.has_criteria"`,
  `"true"`, with `" and "` / `" or "` support (line 160-167).
- **Templates**: `{{ task.id }}`, `{{ task.criteria }}`,
  `{{ stage.tier1.passed }}`, `{{ stages }}` (full JSON dump) — line 327-356.
- **on_fail modes**: `block` (default, stops the pipeline), `continue`
  (mark step failed, keep going, feed into AI), `skip_remaining`.

### Step types (line 227-239)

- `script` — run a shell command (with template substitution)
- `ai_eval` — run `AgenticEvaluator`
- `output` — compile a final output from `{{ stages }}` template

If `config.yaml` is missing, `load_pipeline_config` (line 381-428)
returns a default 2-stage pipeline (tier1 parallel: secrets+lint+tests,
tier2 ai_eval).

---

## 7. The LLM client (multi-provider via 3 env vars)

**File:** `engine/llm.py` — 298 lines.

### Provider detection (lines 40-83)

```python
def _is_anthropic(url: str) -> bool:
    return "anthropic.com" in url.lower() or "claude" in url.lower()
```

- Anthropic → `POST {base_url}/messages`, `x-api-key` header,
  `anthropic-version: 2023-06-01` (configurable).
- Everything else (OpenAI, DeepSeek, OpenRouter, Groq, Ollama, LM
  Studio, etc.) → `POST {base_url}/chat/completions`, `Authorization:
  Bearer ...`.

### Fallback env-var chain (lines 58-64)

If `GITREINS_LLM_API_KEY` is empty, the client tries in order:
`NEURALWATT_API_KEY`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`,
`DEEPSEEK_API_KEY`. First non-empty wins.

### Retry (lines 95-114)

`max_retries=3`, exponential backoff `2 ** attempt` seconds between
retries. 4xx (except 429) is **not** retried — it's a client error.

### Message conversion (lines 240-298)

Internal format is OpenAI (`role`/`content`/`tool_calls`); for
Anthropic the converter:
- Strips `system` (Anthropic takes it as a top-level `system` field).
- Converts `role: tool` results into Anthropic `user` messages with
  `tool_result` content blocks.
- Converts assistant `tool_calls` into Anthropic `tool_use` content
  blocks.

See `provider-integration.md` for how to wire a specific provider.

---

## 8. The MCP server (the only public surface)

**File:** `gitreins_mcp/server.py` — 417 lines.

### Wire format

JSON-RPC 2.0 over line-delimited stdio. Multi-line JSON is handled by
a manual brace-counting loop in `run_stdio()` (lines 335-400) that
buffers until it can extract a balanced JSON object.

`initialize` returns:
```json
{
  "protocolVersion": "2024-11-05",
  "capabilities": {"tools": {}},
  "serverInfo": {"name": "gitreins", "version": "0.1.0"}
}
```

### 9 tools (server.py:34-44)

| Tool | Args | Behavior |
|------|------|----------|
| `task.create` | id, title, criteria[] | Append to `tasks.yaml` |
| `task.start` | id | Set status `in_progress` |
| `task.complete` | id | Set status `complete`, stamp `completed_at`, **auto-evaluate if LLM configured** (line 169) |
| `task.list` | status? | Filter tasks |
| `task.get` | id | Single task dict |
| `task.delete` | id | Remove from `tasks.yaml` |
| `commit` | message | In-progress check → guard.run → `git commit -m` (line 213-240) |
| `guard.run` | — | Tier 1 only |
| `judge.evaluate` | id | Full Tier 1 + Tier 2 |

The `commit` tool is the **only** path to a real commit that runs the
guards — but the pre-commit hook (installed by `gitreins/install`)
provides a backstop against direct `git commit` bypass.

---

## 9. The CLI

**File:** `gitreins/cli.py` — 218 lines.

5 top-level commands:

```
gitreins task create <id> <title> [criteria...]
gitreins task start <id>
gitreins task complete <id>   # completes AND evaluates
gitreins task list [--status ...]
gitreins task delete <id>
gitreins guard
gitreins judge <id>
gitreins commit <message>
gitreins mcp-server
```

`task complete` is interesting (line 53-67) — it calls
`judge.evaluate_task()` after marking complete, so the CLI is also
an end-to-end harness. `commit` (line 115-134) runs guards first, then
`git commit` — but does NOT run the Tier 2 evaluator (that's the
MCP server's job for task.complete).

`get_workdir()` (line 23-33) shells out to `git rev-parse
--show-toplevel`, so the CLI always operates on the repo root, not
the cwd.

---

## 10. .gitreins/ storage layout

**Current repo state (verified by `ls -laR .gitreins/`):**

```
.gitreins/
├── config.yaml                 # 63 lines — pipeline, guards, evaluator, mcp_allowlist
├── tasks.yaml                  # 26 lines — pending + in_progress tasks
└── history/
    └── 2026-06-11/
        └── 058d55a4/           # short SHA prefix
            ├── verdict.json    # full pipeline + per-criterion results
            └── summary.md      # human-readable summary
```

### What's written by the engine

- `config.yaml` — **read** at startup. Created on first install
  by `gitreins/install` (line 20-36).
- `tasks.yaml` — read+write by `TaskManager._load()` / `_save()`.
- `history/<date>/<hash>/verdict.json` + `summary.md` — written
  by the historical `engine/persist.py` (commit 966ae79, AC-010).
  This module is **not currently in the working tree** — it was
  shipped in the v0.1.0 drive and the history record
  (`058d55a4/`) was created from it. Re-importing the module
  if needed is straightforward (see ADR-001 for context).

### What is NOT in `.gitreins/`

- The `gitreins` **branch** (a parallel git branch for storage) does
  NOT exist in the current implementation. The original design
  proposed it; the implementation chose the directory. See
  `adr/adr-001-gitreins-dir-storage.md`.
- `prompts/` and `guardrails/` subdirectories — referenced in
  `docs/architecture.md:78-79` as "optional", not present in the
  current repo.

---

## 11. Git hooks and the install path

**File:** `gitreins/install` — 76 lines. Bash.

```
$ ./gitreins/install
GitReins Install
================
Repo: /home/kara/gitreins-poc

✓ Created .gitreins/config.yaml
✓ Installed .git/hooks/pre-commit

GitReins installed. Create your first task:
  ...
```

Two effects:

1. `mkdir -p .gitreins` + write a default `config.yaml` (line 20-36).
2. Write `.git/hooks/pre-commit` (line 40-64) — a bash script that
   inlines a Python one-liner calling `GuardManager.run_all()` and
   `sys.exit(1)` if any guard fails.

The hook runs **only Tier 1** — there's no evaluator in the hook,
so pre-commit is fast (<100ms for the typical secrets+lint+tests
path).

The pre-existing `.gitreins-hooks/` directory in the repo root is
**empty** — it's a vestige of an earlier design where hooks were
shipped in a sibling directory. The current install path writes
directly to `.git/hooks/`.

---

## 12. Test surface

**Directory:** `tests/` — 8 test files, 322 tests (221 unit + 101
integration), all passing as of 2026-06-15.

| File | Tests | What it covers |
|------|-------|----------------|
| `test_llm.py` | 45 | Multi-provider, retry, env-var fallback, message conversion |
| `test_evaluator.py` | 55 | Agentic loop, tool execution, dedup, verdict parsing, sandbox |
| `test_guard_manager.py` | 41 | Each guard, whitelist/danger patterns, toggling |
| `test_task_manager.py` | 32 | CRUD, persistence, error cases |
| `test_pipeline.py` | 36 | Stages, conditions, templates, pre-commit trigger |
| `test_judge.py` | 19 | Pipeline + legacy paths, precommit |
| `test_cli.py` | 26 | Help, errors, lifecycle, guard/commit, judge |
| `test_mcp_server.py` | 29 | JSON-RPC framing, 9 tools, edge cases, multi-request session |
| `test_judge.py:run_precommit_*` | — | Confirms `trigger="pre-commit"` skips Tier 2 |
| `conftest.py` | — | 110 lines of shared fixtures |

Engine core coverage (per AC-019): evaluator 94%, llm 95%, judge 95%,
task_manager 100%, pipeline 88%, guard_manager 89%. CLI + MCP server
are exercised via subprocess integration tests (pytest-cov can't
measure subprocess coverage).

---

## 13. Quick "where is X" lookup

| Question | File |
|----------|------|
| How do I add a new evaluator tool? | `engine/evaluator.py:64-177` (add to `EVALUATOR_TOOLS`) + `_execute_tool` (line 351) |
| How do I add a new guard? | `engine/guard_manager.py:50-56` (toggle) + new `_check_*` method + add to `run_all` (line 58-78) |
| How do I add a new MCP tool? | `gitreins_mcp/server.py:34-44` (handler map) + `_tool_schemas()` (line 46-150) + new method |
| How do I add a new pipeline stage? | Edit `.gitreins/config.yaml` |
| How do I add a new LLM provider? | `engine/llm.py` — usually no change needed if it's OpenAI-compatible. Just set the 3 env vars. See `provider-integration.md`. |
| Where are the work item plan files? | They don't exist in this repo. `axiom:trace` comments reference `.memory-bank/work-items/GR-XXX/plan.yaml` paths but those are in the external Axiom orchestrator. The work itself is documented in `work-items/STATUS.md`. |
