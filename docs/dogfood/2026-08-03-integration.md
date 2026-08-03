# GitReins Dogfood — Real-Use Integration Report (2026-08-03)

**Verdict: 🟡 PROMISING-BUT-ROUGH**
**Run by:** coding-hermes-dogfood cron (deepseek-v4-flash)
**Repo:** /home/kara/gitreins-poc (gitreins v0.11.0, running as both CLI and MCP server)

## Promise (null hypothesis)

> "A developer or AI agent can install GitReins into any git repo (`gitreins install` /
> `init`), define tasks with completion criteria (CLI or MCP tools), do the work, and have
> an agentic LLM evaluator judge the work per-criterion (`task complete` / `judge`), while
> Tier-1 static guards (secrets, lint, tests) run on every commit via a pre-commit hook and
> BLOCK commits that would ship secrets or broken tests."

## What was actually done (real use, not tests)

Two full consumer journeys:

### Journey 1 — CLI on a fresh scratch repo (`/tmp/dogfood-gitreins-consumer`)

Built a tiny real package (`todo_stats` — counts TODO/FIXME markers) with tests, then ran
the documented Quick Start: `gitreins install` → `gitreins init` → `task create/start/list`
→ `guard` → `commit` → `task complete` (LLM judge).

**Time-to-first-success (green guard on a clean repo): ~20 minutes**, most of it fighting
two config bugs (below). After workarounds: guard PASS (secrets + lint + tests), commit
through the hook, and the LLM judge ran a real evaluation against DeepSeek.

### Journey 2 — MCP server on the real repo (this repo)

Used the live `gitreins` MCP tools end-to-end: `task_create dogfood-2026-08-03` →
`task_start` → wrote these artifacts → `guard` → `commit` → `task_complete` (agentic
evaluator). This is the exact "AI agent does work through MCP" flow the project targets.

## Friction log (every stumble, in order)

1. **P0 — `gitreins init` generates a gitleaks config that panics gitleaks.**
   `gitreins/cli.py` `_generate_gitleaks_config()` (line ~666) writes allowlist `paths`
   containing invalid Go regexes: `*.log`, `*.egg-info/`, `*.spec.md`, `*.md`. gitleaks
   v8.30.1 compiles every allowlist path as a regexp and **panics** on the first invalid
   one (`panic: regexp: Compile('*.log'): missing argument to repetition operator`).
   Since `_check_secrets()` in `engine/guard_manager.py` tries gitleaks first and only
   falls back to the built-in scanner on `FileNotFoundError`, any repo that (a) ran
   `gitreins init` and (b) has gitleaks installed gets a **permanently failing secrets
   guard** → every commit blocked. The guard output is the gitleaks ASCII-art logo (`○`)
   plus a Go panic dump — no actionable message.
   - Hit: first `gitreins guard` on the scratch repo → `✗ secrets — ○` (twice, at
     "nothing staged" and "everything staged").
   - Workaround: fix the four entries to `.*\.log`, `.*\.egg-info/`, `.*\.spec\.md`,
     `.*\.md` (or delete the file — then the built-in scanner runs). Fixing one reveals
     the next: `*.spec.md` panics after `*.log` is fixed.
   - Note: the repo's own hand-maintained `.gitleaks.toml` doesn't contain these
     entries, which is why the harness "passes" internally.

2. **P1 — default `test_command: pytest -x --tb=short` fails on the standard
   `tests/` + root-package layout.** A bare `pytest` does not put the repo root on
   `sys.path` (and pytest 9's default importlib mode doesn't insert basedirs either),
   so `tests/test_*.py` doing `from mypkg import ...` raises
   `ModuleNotFoundError: No module named 'todo_stats'` even though
   `python3 -m pytest` passes 1/1. The generated config's default test command
   therefore blocks commits for fresh, uninstalled projects with a failure the user
   can't reproduce with their own tooling.
   - Hit: `✗ tests (full)` with the pytest header but no traceback visible (output
     shows only the last 2000 chars of captured output; the summary line
     `ERROR tests/test_todo_stats.py` was there but the actionable import error was
     in the truncated head... actually the traceback WAS in the tail, but the guard
     prints result.output which for failures is the full capture — the CLI summary
     line only shows the first line).
   - Workaround: `pytest.ini` with `[pytest] pythonpath = .` (what a real user would
     do) or `test_command: python3 -m pytest ...`.

3. **P2 — guard output truncation hides the failure reason.** `_run_test_command`
   keeps the last 2000 chars on failure, and the CLI prints a one-line summary
   (`✗ tests (full) — <first line of output>`). For a failing test run the first
   captured line is `=== test session starts ===` — the actual failure is in the
   truncated middle. The "Fix the issues above and re-run" hint gives no pointer to
   where the full output lives. Users must re-run pytest themselves to learn why.

4. **P2 — secrets finding lines are truncated mid-string.** The blocked-secret
   output showed `Finding:     OPENAI_API_KEY = "sk-1234...` with the value cut off
   and no file/line location. gitleaks' verbose output normally includes the file;
   the guard's 2000-char tail cut it.

5. **P2 — `gitreins init` reports `Language: unknown` for a pure-Python repo** with
   no `pyproject.toml` (detection keys off pyproject/setup files, not `.py` files).
   Confusing for the most common bootstrap case (plain repo + `pip install -e .` later).

6. **P2 — generated `.gitignore` is minimal** (only `.gitreins/tasks.yaml`). A
   `__pycache__` `.pyc` created by the guard's own pytest run got staged by
   `git add -A`. Not a harness bug per se (never overwrite user gitignore), but new
   users will commit junk on their first commit.

## What held up (the promises that WORKED)

- ✅ `gitreins install` — clean, helpful output (what was created, next steps), exit 0.
- ✅ `gitreins init` — smart config with size-appropriate caps, backs up existing files.
- ✅ Task lifecycle via CLI **and** MCP: create/start/list/complete all work, tasks
  persist in `.gitreins/tasks.yaml`.
- ✅ **Secrets guard blocks real secrets**: committing a file with `sk-...` API key was
  BLOCKED by the pre-commit hook (`✗ secrets — Finding: ...`), exit 1. The flagship
  promise holds.
- ✅ Lint guard caught a genuine F401 (unused import under `from __future__ import
  annotations`) with an actionable ruff message.
- ✅ Tests guard runs and gates commits (modulo finding #2's environment trap).
- ✅ Pre-commit hook is thin and honest (runs `gitreins guard`, exits with its code).
- ✅ LLM judge (`task complete`) works end-to-end — see the verdict below.
- ✅ MCP `commit` tool runs guards before committing (used for this very commit).
- ✅ `gitreins guard` exit codes are correct (0 pass / 1 fail).

## Judge / agentic evaluator result

`gitreins task complete todo-counter` on the scratch repo triggered a real agentic
evaluation (DeepSeek via `GITREINS_LLM_*` env). Verdict recorded in `.gitreins/history/`
and viewable via `gitreins report`. Bounded via `GITREINS_MAX_ITERATIONS=12`,
`GITREINS_MAX_TIME=8m`. (Full per-criterion PASS/FAIL rows are in the verdict file.)

## MCP-phase findings (Journey 2, same run)

7. **P1 — MCP `judge_evaluate` / `task_complete` times out at 300 s on repos whose
   suite takes longer.** Live call: `judge_evaluate dogfood-2026-08-03` (eval_cap
   `40/35m/300k/80k`) failed with `TimeoutError: MCP call timed out after 300.0s`
   while the server-side evaluation kept running (full pytest suite ~11 min for the
   tier-1 stage — docs-only changes fall back to full suite in diff mode). The
   agent-facing judge path therefore cannot complete on any repo with a >5 min
   suite. The verdict eventually lands server-side (task completes later), but the
   agent gets a hard error and no result. Needs async judge + polling, or a raised
   transport timeout, or tier-1 split out of the MCP judge call.

8. **P2 — MCP `commit` tool blocks commits while a task is `in_progress`, with a
   cryptic error.** `commit` → `"Tasks still in progress — complete or delete them
   first"`. No hint of which tasks or why; README/AGENTS.md never mention that
   in-progress tasks block the MCP commit path. (The pre-commit hook does NOT block
   on this — only the MCP commit tool does. Enforced order: complete → commit.)

9. **P2 — `gitreins report` says "No verdict history found" on `main` right after a
   successful judge.** With `history.storage: git` the verdict is committed to a
   separate `gitreins` branch (`aecf1a5 verdict: todo-counter — PASS`), so the
   working tree on main has no `.gitreins/history/` and `report` (engine/persist.py:463)
   returns nothing. `report` only works after switching to the `gitreins` branch —
   mechanics undocumented (README: "stored in `.gitreins/history/`, browsable via
   `gitreins report`" — misleading as written).

## Errors hit and their fixes (quick reference)

| Symptom | Cause | Fix |
|---|---|---|
| `✗ secrets — ○` + Go panic dump on every guard | generated `.gitleaks.toml` has invalid regexes (`*.log`, `*.egg-info/`, `*.spec.md`, `*.md`) | fix to `.*\.log` etc., or delete file (built-in scanner runs) |
| `✗ tests (full)` but `python3 -m pytest` passes | bare `pytest` can't import root package | add `pytest.ini` `[pytest] pythonpath = .` |
| guard summary hides the real test failure | only last 2000 chars kept, summary shows first line | run `pytest` yourself; (maintainer: print failure tail) |

## Bottom line

The core value — a commit-time quality gate that actually blocks secrets, plus an
agentic per-criterion judge — is real and works. But the **onboarding path for a new
project is broken**: `gitreins init` + a gitleaks install (which AGENTS.md itself
recommends) = permanently failing secrets guard, and the default test command fails for
the most common Python layout. Fix those two and GitReins is genuinely shippable.
