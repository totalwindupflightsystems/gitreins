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

There are **11 top-level subcommands**:

| # | Command | Purpose |
|---|---------|---------|
| 1 | `install` | Install hooks and config in the current repo |
| 2 | `init` | Smart init — detect language, size, optimal config |
| 3 | `task` | Task management (create / start / complete / list / delete) |
| 4 | `guard` | Run Tier 1 guards (secrets, lint, tests, static analysis) |
| 5 | `judge` | Evaluate a task (Tier 1 + Tier 2 LLM judge) |
| 6 | `commit` | Commit with guard checks |
| 7 | `commit-audit` | Validate commit message against staged diff (commit-msg hook) |
| 8 | `mcp-server` | Run the MCP stdio server |
| 9 | `security-scan` | Run the Antares CVE localization scanner (opt-in) |
| 10 | `setup-tools` | Show available static analysis tools and install instructions |
| 11 | `report` | Show verdict history |

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
gitreins guard [--dead-code]
```

| Option | Description |
|--------|-------------|
| `--dead-code` | Enable Python dead-code detection (overrides config) |

**Exit codes**

| Code | Meaning |
|------|---------|
| 0 | All guards PASS |
| 1 | One or more guards FAIL (fix issues and re-run) |

Warnings are printed to stderr and do not affect the exit code. The
output includes the active test mode (`diff` or `full`) and the tested
targets.

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
| 0 | Evaluation complete (verdict persisted) |
| 1 | Task not found |

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

## Hooks

- **pre-commit**: runs `gitreins guard` on staged changes. A guard
  failure blocks the commit (exit 1).
- **commit-msg**: runs `gitreins commit-audit`; a rejected message
  blocks the commit.

## Configuration

Runtime behavior is controlled by `.gitreins/config.yaml` in the repo
root (created by `gitreins install` / `gitreins init`). Key settings:
`test_command`, `max_input_tokens`, guard enable/disable toggles, and
history persistence. See `docs/architecture.md` for the config schema.
