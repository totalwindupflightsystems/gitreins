---
name: gitreins-usage
description: >-
  How to use the GitReins quality harness in this repo (and any repo it's
  installed in): task lifecycle, guards, LLM judge, MCP tools, and the known
  pitfalls that will bite you. Load this before committing or creating tasks.
version: 1.0.0
category: software-development
---

# GitReins Usage — Task Lifecycle, Guards, Judge, MCP

GitReins is a git-native quality harness: tasks with completion criteria, Tier-1
static guards (secrets/lint/tests) on every commit, and an agentic LLM evaluator
that judges task completion per-criterion. This repo (gitreins-poc) both BUILDS
GitReins and uses it as its own quality gate (dogfood).

## Entry points

| Entry | What it is | Use when |
|---|---|---|
| `gitreins` CLI (`.venv/bin/gitreins`) | full harness | scripts, humans, non-MCP agents |
| `gitreins mcp-server` | MCP stdio server | AI agents with MCP tool access (`task_create`, `task_start`, `task_complete`, `judge_evaluate`, `guard_run`, `commit`, `configure`, `propagate`) |
| pre-commit hook (`.git/hooks/pre-commit`) | runs `gitreins guard` | every `git commit` — cannot be skipped via `--no-verify` for code changes (AGENTS.md) |
| `gitreins report` | verdict history browser | after judge runs |

## Quick workflow (the right way)

```bash
# 1. Create a task with explicit, verifiable criteria
gitreins task create my-task "Do the thing" \
  "file X exists with feature Y" "tests pass"

# 2. Mark in progress, do the work
gitreins task start my-task
# ... implement ...

# 3. Run the judge — agentic LLM evaluates each criterion
gitreins task complete my-task        # triggers evaluation

# 4. Commit — the hook runs Tier 1 guards and BLOCKS on failures
git commit -m "feat: done"
# or: gitreins commit "feat: done"    # same guard, explicit

# 5. Browse verdicts
gitreins report
```

MCP agents: `task_create` → `task_start` → (work) → `task_complete` → `commit`.
The MCP `commit` tool runs guards first and rejects the commit if they fail.

## Guards (Tier 1) — what blocks, what warns

- **secrets** (BLOCKS): gitleaks first, built-in regex scanner fallback. Covers
  sk-/ghp_/glpat-/AKIA/AIza/slack/JWT/password patterns; whitelists common
  false positives (os.getenv, ${VAR}, placeholders).
- **lint** (WARNS): ruff. `*.md` docs-only changes are exempt in practice.
- **tests** (BLOCKS): full or diff mode. Diff mode maps changed files to test
  files by basename; config changes / unmapped files → full suite fallback.
- Config: `.gitreins/config.yaml` → `guards:` (secrets/lint/tests/test_mode/
  test_command/test_timeout). Test command here: `uv run pytest -x --tb=short`,
  timeout 900s (full suite takes ~11 min — don't lower the timeout).

## Judge (Tier 2) — agentic evaluator

- `task complete` runs the evaluator automatically; `gitreins judge <id>` standalone.
- Reads files, runs tests, delivers per-criterion PASS/FAIL, persists verdicts to
  `.gitreins/history/` (git-stored).
- LLM config: `GITREINS_LLM_BASE_URL` / `GITREINS_LLM_API_KEY` / `GITREINS_LLM_MODEL`
  (fallback: OPENAI/ANTHROPIC/DEEPSEEK keys). Model default: deepseek-v4-flash.
- Caps: `evaluator:` config or `GITREINS_MAX_ITERATIONS`, `GITREINS_MAX_TIME`,
  `GITREINS_MAX_INPUT_TOKENS`, `GITREINS_MAX_OUTPUT_TOKENS` env (highest priority).
  For quick tasks bound them (e.g. `GITREINS_MAX_ITERATIONS=12 GITREINS_MAX_TIME=8m`).
- `judge_evaluate` MCP tool accepts eval_cap like `"20/5m/200k/50k"`.

## Pitfalls (learned the hard way — 2026-08-03 dogfood run)

1. **`gitreins init` writes a broken `.gitleaks.toml`** — allowlist contains invalid
   regexes (`*.log`, `*.egg-info/`, `*.spec.md`, `*.md`). If gitleaks is installed,
   it PANICS on every run → `✗ secrets — ○` → commits blocked. Fix: edit those
   entries to `.*\.log` etc., or delete the file (built-in scanner runs). Generator
   fix tracked as task (see board / findings in tasks).
2. **Bare `pytest` may not import your root package** (pytest 9 importlib mode).
   If `gitreins guard` says `✗ tests` but `python3 -m pytest` passes, add
   `[pytest] pythonpath = .` to `pytest.ini`/`pyproject.toml`, or change
   `test_command` to `python3 -m pytest -x --tb=short`.
3. **Guard summaries truncate failure output** (last 2000 chars, summary shows the
   FIRST line). When a guard fails, re-run the failing command yourself to see the
   real error.
4. **`gitreins init` reports `Language: unknown` for plain-Python repos** without
   pyproject.toml — add one first so Python exclusions/tuning apply.
5. **Never commit `.gitreins/tasks.yaml`** — local state. Add `__pycache__/` and
   `.venv/` to your own `.gitignore` before the first `git add -A`.
6. **Never use `os.kill()`/`os.killpg()` without PID validation** in this codebase
   (`int(mock.pid) == 1` kills init — see AGENTS.md, engine/lsp.py:408-428).
7. **MCP `judge_evaluate`/`task_complete` calls time out at 300 s** while the
   server-side evaluation keeps running (tier-1 runs the full suite; this repo's
   suite is ~11 min). For slow-suite repos: run the judge via CLI (`gitreins task
   complete <id>`) or in the background and poll `.gitreins/tasks.yaml` / history.
8. **MCP `commit` refuses while any task is `in_progress`** — "Tasks still in
   progress — complete or delete them first". Complete (judge) the task first, then
   commit. The pre-commit hook does not have this rule.
9. **`gitreins report` only sees verdicts on the `gitreins` branch** — with
   `history.storage: git`, verdicts commit to a separate branch; on `main` the
   report says "No verdict history found". Check out the branch or read the verdict
   files via `git show gitreins:.gitreins/history/...`.

## Config reference (quick)

```yaml
guards: { secrets: true, lint: true, tests: true, test_mode: diff,
          test_command: "uv run pytest -x --tb=short", test_timeout: 900 }
evaluator: { max_iterations: 200, max_time: 45m, max_input_tokens: 10M,
             max_output_tokens: 1M, tool_call_weight: 0.1, fast_track: auto }
defaults: { model: deepseek-v4-flash }
```

## Verifying a run is healthy

```bash
PATH="$HOME/go/bin:$HOME/gitreins-poc/.venv/bin:$PATH" gitreins guard   # must PASS
gitreins task list                                                      # board state
gitreins report -n 3                                                    # recent verdicts
```

More detail: `docs/dogfood/2026-08-03-integration.md` (real-use report),
`docs/dogfood/diagnostics.md` (build/error trail).
