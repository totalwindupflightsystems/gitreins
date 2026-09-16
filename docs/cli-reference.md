# GitReins CLI Reference

User-facing reference for the `gitreins` command-line interface. This
covers every subcommand, its options, and its exit codes. For the MCP
server surface, see [mcp-api.md](mcp-api.md). For installation and
general usage, see the README.

## Global

```
gitreins [--version] <command> [options]
```

| Flag | Description |
|------|-------------|
| `--version` | Print the installed GitReins version and exit 0 |
| `-h` / `--help` | Print help for the command (argparse standard) |

Running `gitreins` with no command prints the top-level help and exits
**0**. An unknown command exits **2** (argparse behavior for
unrecognized arguments).

There are **13 top-level subcommands**:

| # | Command | Purpose |
|---|---------|---------|
| 1 | `install` | Install hooks and config in the current repo |
| 2 | `init` | Smart init — detect language, size, optimal config |
| 3 | `task` | Task management (create / start / complete / list / delete / worktree) |
| 4 | `worktree` | Task worktrees, disposal, repro, dogfood, fleet, merge |
| 5 | `guard` | Run Tier 1 guards (secrets, lint, tests, static analysis) |
| 6 | `judge` | Evaluate a task (Tier 1 + Tier 2 LLM judge) |
| 7 | `commit` | Commit with guard checks |
| 8 | `commit-audit` | Validate commit message against staged diff (commit-msg hook) |
| 9 | `mcp-server` | Run the MCP stdio server |
| 10 | `security-scan` | Run the Antares CVE localization scanner (opt-in) |
| 11 | `setup-tools` | Show available static analysis tools and install instructions |
| 12 | `report` | Show verdict history |
| 13 | `serve` | Live judgment browser (local web server) |

## 1. `gitreins install`

One-command GitReins activation for the current repo. Creates
`.gitreins/config.yaml` (if missing), installs the `pre-commit` hook,
and adds `.gitreins/tasks.yaml` to `.gitignore`.

```
gitreins install
```

No options.

**Exit codes**

| Code | Meaning |
|------|---------|
| 0 | Installed (files created or skipped) |
| 1 | Current directory is not a git repository |

## 2. `gitreins init`

Smart project initialization — detects language, test command, project
size, and available static-analysis tools, then writes or merges
`.gitreins/config.yaml`. Re-runnable: never overwrites existing config
values, only adds missing sections.

```
gitreins init [--reset]
```

| Option | Description |
|--------|-------------|
| `--reset` | Reset config to smart defaults (discards existing config) |

**Exit codes**

| Code | Meaning |
|------|---------|
| 0 | Config written/merged successfully |
| 1 | Config file exists but could not be parsed (fix YAML, or use `--reset`) |

## 3. `gitreins task`

Task management. Subcommands:

```
gitreins task create <id> <title> [criteria...] [--depends-on <id>]...
gitreins task start <id>
gitreins task complete <id> [--force]
gitreins task list [--status <status>]
gitreins task delete <id>
```

### `task create`

| Argument | Description |
|----------|-------------|
| `id` | Task ID (required, positional) |
| `title` | Task title (required, positional) |
| `criteria` | One or more acceptance criteria (variadic positional) |
| `--depends-on <id>` | Task ID that must complete first; repeatable |

Exit **0** on success.

Put `--depends-on` **after** the criteria: `criteria` is a variadic positional
argument, so argparse rejects criteria written after an option (the error is
`unrecognized arguments: <criterion text>`). The flag is repeatable —
`--depends-on build --depends-on lint` — and dependency checks are enforced at
`task complete` (bypass with `--force`).

### `task start`

| Argument | Description |
|----------|-------------|
| `id` | Task ID (required, positional) |

Exit **0** on success.

### `task complete`

Marks the task complete and runs the Tier 2 LLM judge, then persists
the verdict.

| Argument | Description |
|----------|-------------|
| `id` | Task ID (required, positional) |
| `-f`, `--force` | Skip dependency checks |

**Exit codes**

| Code | Meaning |
|------|---------|
| 0 | Task completed and judged |
| 1 | Blocked by incomplete dependencies (unless `--force`) |

### `task list`

| Option | Description |
|--------|-------------|
| `--status <status>` | Filter by status: `pending`, `in_progress`, or `complete` |

Exit **0** (prints "No tasks found." when the list is empty).

### `task delete`

| Argument | Description |
|----------|-------------|
| `id` | Task ID (required, positional) |

Exit **0** on success.

## 4. `gitreins guard`

Run the Tier 1 guards (secrets, lint, tests, static analysis). This is
the quality gate enforced by the pre-commit hook; it can also be run
manually at any time.

```
gitreins guard [--dead-code] [--staged-only] [--full]
```

| Option | Description |
|--------|-------------|
| `--dead-code` | Enable Python dead-code detection (overrides config) |
| `--staged-only` | Run tests in diff mode — only packages with staged changes (overrides `guards.test_mode`) |
| `--full` | Grade the whole tree even with an empty index: the tests lane runs and lint covers tracked+untracked Python files instead of skipping |

**Exit codes**

| Code | Meaning |
|------|---------|
| 0 | All guards PASS (or a DEGRADED pass with `guards.allow_skips: true`) |
| 1 | One or more guards FAIL (fix issues and re-run) |
| 2 | DEGRADED PASS — a substantive gate (lint/tests/lsp) did no work and `guards.allow_skips` is false |

Warnings are printed to stderr and do not affect the exit code. The
output includes the active test mode (`diff` or `full`) and the tested
targets.

**Degraded pass (`guards.allow_skips`, TRUST-001)**

A guard that does no work — nothing staged, no linter on PATH, zero tests
collected — is not a gate that passed. Those runs print
`Tier 1: DEGRADED PASS (skips: lint=no staged files, tests=no staged files)`,
mark the skipped steps with `~`, and exit **2** unless the repo sets
`guards.allow_skips: true`:

```yaml
guards:
  allow_skips: true   # accept zero-work skips (gitreins init writes this)
```

A degraded run never prints `Tier 1 Guards: PASS`, so CI and merge-back —
which both consume the exit code as truth — can tell a gate that never ran
from one that passed. The skipped steps are also persisted in the guard run
log (`.gitreins/logs/`), in the judge's Tier 1 stage, and in
`verdict.json` (`stages.tier1.skipped_steps`); `gitreins worktree merge`
refuses a verdict whose Tier 1 carries skips.

**`--full` grades the whole tree (empty index included).** A `--full` run
does not produce skips on a clean tree: the tests lane runs the configured
`guards.test_command`, and lint covers tracked + untracked-but-not-ignored
Python files (the lint line names the scope, `ruff: clean (N tracked
files)`). The mode note reads `(test mode: full, whole tree)` so a
whole-tree run is distinguishable from a staged run, and the plain PASS
header is only printed when the gates actually ran. `--staged-only` and a
bare `gitreins guard` keep the degraded-pass semantics above; when the
index is non-empty, staged files are graded rather than the whole tree.

## 5. `gitreins judge`

Evaluate a task: runs Tier 1 guards, then the Tier 2 LLM judge
(unless skipped), and persists the verdict.

```
gitreins judge <id> [--skip-tier2] [--async] [--status <job_id>]
```

| Option | Description |
|--------|-------------|
| `id` | Task ID (or job ID with `--status`) |
| `--skip-tier2` | Skip Tier 2 LLM evaluation; Tier 1 guards only |
| `--async` | Dispatch evaluation as a detached background job; returns a job ID |
| `--status <job_id>` | Show status/result of a background job (id = job id, not task id) |

**Exit codes (sync mode)**

| Code | Meaning |
|------|---------|
| 0 | Evaluation complete and the verdict PASSED (persisted) |
| 1 | Task not found, or the evaluation verdict FAILED |

Sync `judge` propagates the verdict to the shell (DF-GITREINS-POC-16): a FAIL
verdict exits 1, matching `gitreins guard` on the same tree, so a red gate can
never be read as success by a script. Tier 1 grades the same check set the
guard grades — see [the Tier 1 / guard parity contract](evaluator-loop.md#tier-1--guard-parity-contract-df-gitreins-poc-16)
— and a tier1 narrower than the guard gate (nothing detectable) is marked
degraded in `verdict.json` and warned about on the CLI.

**Exit codes (`--status` mode)**

| Code | Meaning |
|------|---------|
| 0 | Job complete (result printed) |
| 1 | Job errored, or job not found |
| 2 | Job still running (poll again later) |

## 6. `gitreins commit`

Run Tier 1 guards, then commit with the given message. If the guards
fail, the commit is aborted.

```
gitreins commit <message> [--skip-tier2]
```

| Argument/Option | Description |
|-----------------|-------------|
| `message` | Commit message (required, positional) |
| `--skip-tier2` | Skip any Tier 2 processing; Tier 1 guards only |

**Exit codes**

| Code | Meaning |
|------|---------|
| 0 | Guards passed and commit created |
| 1 | Tier 1 guards FAILED — nothing committed |

## 7. `gitreins commit-audit`

Validate a commit message against the staged diff (used by the
commit-msg hook). Reads the message from the argument, or falls back
to `.git/COMMIT_EDITMSG` when omitted.

```
gitreins commit-audit [message]
```

| Argument | Description |
|----------|-------------|
| `message` | Commit message; omitted → read from `COMMIT_EDITMSG` |

The audit is skipped (exit 0) when a `gitreins.skip-tier2` trailer is
present in the message.

**Exit codes**

| Code | Meaning |
|------|---------|
| 0 | Message OK, audit skipped, or no message to audit |
| 1 | Commit message rejected by the audit stage |

## 8. `gitreins mcp-server`

Run the MCP stdio server. No flags required; configuration is via
environment variables:

| Env var | Description |
|---------|-------------|
| `GITREINS_LLM_API_KEY` | API key for the LLM provider |
| `GITREINS_LLM_BASE_URL` | Base URL of the LLM API (default `https://api.openai.com/v1`) |
| `GITREINS_LLM_MODEL` | Model name (default varies by provider) |
| `GITREINS_LLM_REASONING` | Reasoning mode: `enabled` or `disabled` (default `disabled`) |

The MCP tool `mcp_gitreins_configure` can hot-reload the LLM config at
runtime. Exits **0** on clean shutdown; **1** on fatal errors.

## 9. `gitreins security-scan`

Run the Antares CVE localization scanner (opt-in) over staged files or
a directory.

```
gitreins security-scan [-d <dir>] [--output text|json] [--force-ml]
```

| Option | Description |
|--------|-------------|
| `-d`, `--directory <dir>` | Scan a directory recursively instead of staged files |
| `--output <fmt>` | Output format: `text` (default) or `json` |
| `--force-ml` | Require ML inference (fails if `huggingface_hub`/`transformers` missing) |

**Exit codes**

| Code | Meaning |
|------|---------|
| 0 | Scan clean — no findings |
| 1 | Findings reported (or scan failed) |
| 2 | Required ML dependencies missing (with `--force-ml`) |

## 10. `gitreins setup-tools`

Show available static analysis tools for the detected language and
print install instructions for missing ones.

```
gitreins setup-tools
```

No options. Exits **0** when all tracked tools for the detected
language are installed; **1** when tools are missing (and lists the
install instructions).

## 11. `gitreins report`

Show recent verdict history.

```
gitreins report [-n <count>] [--interactive]
```

| Option | Description |
|--------|-------------|
| `-n <count>` | Number of recent verdicts to show (default 10) |
| `-i`, `--interactive` | Interactive TUI mode (requires `textual`; falls back to text) |

Exit **0** on success.

## 12. `gitreins worktree`

Worktree lifecycle and disposable verification commands. Task worktrees are
branch-backed; disposable worktrees are detached and live under a separate
`.disposable` directory.

| Subcommand | Purpose |
|---|---|
| `doctor` | Validate the shared canonical registry resolution before trusting it |
| `list` | List registered task worktrees (task, branch, state, phase, age, cap) |
| `fleet <manifest>` | Run explicit task lanes concurrently in isolated worktrees (JSON/YAML manifest; `--merge` applies successful lanes) |
| `fresh` | Run one command in a fresh detached worktree |
| `repro` | Run a command repeatedly in fresh detached worktrees (flakiness measurement) |
| `dogfood` | Exercise `init`, task, `guard`, and `judge` in a throwaway tree |
| `clean` | Reap merged worktrees immediately; stale/orphan only with confirmation |
| `merge <id>` | Judge-gated fast-forward merge of a task worktree into canonical main |

Task worktrees themselves are created with `gitreins task worktree <id>`
(idempotent: an existing tree for the task is reused).

### `worktree fresh`

```bash
gitreins worktree fresh --cmd "<shell command>" [--json <path>] [--keep]
  [--timeout <seconds>]
```

Runs the command with `sh -c` in one clean tree. Exit 0 means the command
passed; a nonzero command exit is propagated unchanged; exit 2 means GitReins
could not create/reap the tree or write evidence. `--keep` retains the tree
and `--json` writes a machine-readable run record.

### `worktree repro`

```bash
gitreins worktree repro --cmd "<shell command>" -k <N>
  [--concurrency <C>] [--timeout <seconds>] [--keep-failures] [--json <path>]
```

Runs N copies from the same captured `HEAD`. Concurrency defaults to
`worktree_fleet.max_concurrent_worktrees`. Exit 0 means all passed, exit 1
means one or more command failures, and exit 2 means infrastructure failure.
The JSON record has this shape:

```json
{"command":"<cmd>","k":3,"concurrency":2,"head":"<sha>",
 "passes":3,"failures":0,"pass_rate":1.0,
 "runs":[{"index":1,"exit_code":0,"duration_s":0.1,
           "tree":"<path>","kept":false}],
 "started_at":0.0,"finished_at":0.0}
```

`--keep-failures` keeps failed trees only; successful trees are always reaped.

### `worktree dogfood`

```bash
gitreins worktree dogfood [--keep] [--skip-judge]
  [--test-command "<cmd>"] [--timeout <seconds>] [--json <path>]
```

Runs `init`, task creation/start, `guard`, and the Tier 2 judge flow in a
throwaway checkout. `--skip-judge` skips Tier 2 deterministically. If no LLM
key is configured, the judge is recorded as skipped rather than passed.
`--test-command` overrides only the guard test command inside the tree. Exit
0 means executed steps passed or judge was skipped, exit 1 means a step
failed, and exit 2 means GitReins infrastructure failed. Evidence contains
`steps`, a `judge` object, timestamps, the tree path, and keep status.

## 13. `gitreins serve`

Live judgment browser — a local web server that renders the verdict history
(the same `.gitreins/history/<date>/<hash>/verdict.json` directories and the
`gitreins` branch fallback that `report` reads). Ctrl-C stops it.

```
gitreins serve [--port <port>] [--host <host>] [--project <name>] [--open]
```

| Option | Description |
|--------|-------------|
| `--port` | Port to bind (default `8616`) |
| `--host` | Bind address (default `127.0.0.1` — local-only unless you change it) |
| `--project` | Scheduler project name for the tick ledger (e.g. `gitreins-poc`) |
| `--open` | Open the browser automatically |

A static variant for publishing history without a server is
`scripts/judgment_viewer.py`.

## Hooks

- **pre-commit**: installed by `gitreins install`; runs `gitreins guard` on
  staged changes. A guard failure blocks the commit (exit 1); a DEGRADED pass
  (exit 2) also blocks unless `guards.allow_skips: true` — stage a gradable file
  or accept skips in config.
- **commit-msg**: **not installed by `gitreins install`.** The CLI ships
  `gitreins commit-audit` for that slot (no argument needed — it reads
  `.git/COMMIT_EDITMSG`), but you must create the hook yourself:

  ```bash
  cat > .git/hooks/commit-msg <<'HOOK'
  #!/usr/bin/env bash
  exec gitreins commit-audit
  HOOK
  chmod +x .git/hooks/commit-msg
  ```

  It needs an LLM credential, skips on a `gitreins.skip-tier2` trailer, and
  blocks the commit only when the config's pipeline includes a `commit_audit`
  stage with `commit_audit.mode: block` (`warn` is the default) — the command
  exits 0 with "No commit message to audit." when there is nothing to read.

## Configuration

Runtime behavior is controlled by `.gitreins/config.yaml` in the repo
root (created by `gitreins install` / `gitreins init`). Key settings:
`test_command`, `test_mode`, `test_on_clean`, `allow_skips`,
`max_input_tokens`, guard enable/disable toggles, and
history persistence. See `docs/architecture.md` for the config schema.

### `guards.test_on_clean`

```yaml
guards:
  test_on_clean: false   # default
```

Runs the configured `test_command` even when the index is empty (nothing
staged). The default `false` means the tests lane is a SKIP with the named
reason `no staged files` — under `guards.allow_skips: false` the whole run is
then a DEGRADED pass (exit 2). Set it to `true` when the suite must run on
clean-tree guard runs too: chained suites where a clean tree still needs
executing, post-hoc audits, or repos whose commits land through another tool.
`gitreins guard --full` is the per-run override and additionally lints
tracked+untracked Python files.

### `guards.allow_skips`

Accepts a DEGRADED run as exit 0 (see the guard exit-code table). `gitreins init`
writes `true` for new repos; CI should normally keep `false` so a gate that did
no work can never read as a gate that passed.

### `evaluator.static_analysis_diagnostics`

Advertises `read_static_analysis` to the Tier 2 judge and lets it return the
configured analyzers' diagnostics. Off by default; `gitreins init` enables it
for detected dynamic-language projects.

### Judge token telemetry

`.gitreins/usage.jsonl` — one JSON line per evaluation step (`ts`, `tokens_in`,
`tokens_out`, `cache_read`, `cache_write`, `step`), appended best-effort and
gitignored by `install`/`init`. Counters are cumulative per context window and
reset on compaction, so sum deltas rather than reading the last line. See
README ("Judge token usage") and `docs/evaluator-loop.md` for consumers.

