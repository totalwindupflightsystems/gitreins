# GitReins Onboarding Guide

**Version-stamped: verified against `gitreins 0.15.0` (2026-09-25).** The guide
started as the 2026-08-03 dogfood report (`docs/dogfood/2026-08-03-integration.md`)
and has been refreshed so every command matches the current CLI: `init`, `task`,
`guard`, `judge`, `serve` and `worktree` are all exercised as written.
`scripts/check_cli_examples.py` replays every `gitreins` example in this file and
README through the real argparse in CI, so a command line here cannot silently
rot again.

Add GitReins to a new project: install, configure, run your first guard, and
drive the task → judge workflow. Troubleshooting entries come from real
integration failures.

## 1. Install

```bash
pip install gitreins
```

On distros that enforce PEP 668 (Debian 13, Fedora, Ubuntu 23.04+), bare pip
exits 1 with `externally-managed-environment` — use a venv instead:

```bash
python3 -m venv .venv && .venv/bin/pip install gitreins
```

Downstream commands run via `.venv/bin/gitreins` (or an activated venv).

**uv is optional, not required.** `gitreins init` writes `uv run pytest` as the
test command only when uv is on PATH. On a machine without uv (pip-only), the
tests guard automatically falls back to `python -m pytest ...` — you get a
warning line in guard output, never a `uv: command not found` failure. The same
fallback covers `pipenv run` and `poetry run`.

**A missing pytest runner is a skip, not a blocked first commit.** On a bare
machine (`pip install gitreins` → `install` → `init`, nothing else) the tests
lane has no runner to grade with. When the configured
`guards.test_command` names a pytest runner this machine does not have — no
`pytest` on PATH and nothing importable, a pinned `.venv/bin/python` that was
never created, an interpreter that exists with pytest not installed in it, a
`.venv/bin/pytest` that does not exist — the lane is graded **skipped** with the
fix named in the reason (`pip install pytest`, `uv sync`, `pip install -e
.[dev]`), the same way a linter that is not on PATH is skipped. Because
`install` and `init` write `guards.allow_skips: true`, that first commit lands
as a DEGRADED pass naming the lane it did not grade (§4); with `allow_skips:
false` the run exits 2 instead. A pytest run that actually executes and fails
still blocks the commit — only a runner that never started is a skip.

**Running from a source checkout** (contributing to GitReins itself, no pip
install): the console script is only on PATH after activating the repo's
virtualenv — a fresh shell gets `command not found` otherwise.

```bash
cd gitreins
python3 -m venv .venv && .venv/bin/pip install -e .
source .venv/bin/activate        # or call .venv/bin/gitreins directly
gitreins --help
```

Then, inside your project repo (must already be a git repository):

```bash
gitreins install
```

`gitreins install` creates:

- `.gitreins/config.yaml` — default config (skipped if already present)
- `.git/hooks/pre-commit` — runs `gitreins guard` on every commit
  (overwritten if a hook already exists)
- `.gitignore` — appends exactly five local GitReins runtime exclusions:
  `.gitreins/tasks.yaml`, `.gitreins/config.yaml.bak`,
  `.gitreins/usage.jsonl`, `.gitreins/logs/` and
  `.gitreins/qa-ledger.jsonl`; Python projects also get `__pycache__/`
  (existing entries are preserved and never duplicated). Several other
  runtime files are **not** written by `install` — the commands that use
  them create them when they run: `worktrees.json`, `worktrees.lock`,
  `disposable.json`, `disposable.lock` and `tasks.yaml.lock`.

`install` writes the **pre-commit hook only**. The commit-message auditor
(`gitreins commit-audit`, which reads `.git/COMMIT_EDITMSG` when given no
argument) is shipped for the `commit-msg` slot but is not installed for you —
create the hook yourself if you want messages audited:

```bash
cat > .git/hooks/commit-msg <<'HOOK'
#!/usr/bin/env bash
exec gitreins commit-audit
HOOK
chmod +x .git/hooks/commit-msg
```

It needs an LLM credential and honours a `gitreins.skip-tier2` trailer in the
message. `install`/`init` declare no `commit_audit` stage, so the hook does
nothing until you add one:

```yaml
pipeline:
  stages:
    - id: commit_audit
      type: commit_audit
      on: [commit-msg]
      mode: block        # warn (default) | block | suggest
```

Without that stage the command prints
`commit audit: no pipeline stage with type commit_audit for trigger commit-msg — audit NOT run`
and exits 0. `mode` resolves stage level → `defaults.commit_audit` → top-level
`commit_audit` → `warn`; only `block` makes the hook fail the commit. See
`docs/cli-reference.md` §7 for the full table.

## 2. Smart init

```bash
gitreins init
```

`gitreins init` auto-detects the project language, size, and complexity, and
writes an optimized `.gitreins/config.yaml` (guard set, test mode, evaluator
budgets). Run it after `install` — `install` writes only the conservative
baseline config, while `init` tailors it to the repo. For detected Python and
other dynamic-language projects, smart init enables static analysis and records
its configured tools. Explicit static-analysis settings and custom test
commands are preserved on reruns; the detected test runner replaces only the
untouched `install` default. `gitreins init --reset` rewrites the smart defaults
from scratch when a config has drifted.

### Resolution-gate surfaces are off until you enable them

`gitreins init` also writes the `resolution:` block (JEVRES-006) — with **every surface
`false`**. `gitreins resolve` and the MCP `context.resolve` tool read that block, and only
a literal `true` opens a surface; a missing key, a missing block and a wrong-typed value
all read as OFF. Enabling is your explicit act, never an init side effect: the assembled
bundle then leaves the host for a third party (OpenRouter → TypeSafe), and the two
judge-adjacent surfaces additionally have no calibration numbers until JEVRES-005:

- `resolution.enabled.cli: true` — `gitreins resolve` runs for real
- `resolution.enabled.mcp: true` — the MCP `context.resolve` tool runs for real
- `resolution.enabled.predispatch` / `resolution.enabled.judge_prescreen` — leave `false`

Disabled, both surfaces fail closed with `abstain_reason: surface-disabled` (exit 1) and
print the enabling fix instead of guessing. The complete block, its defaults and the
calibration caveat are in [docs/jev-resolution-gate.md](jev-resolution-gate.md) §9.

## 3. Gitleaks allowlist (no action needed)

`gitreins init` generates a `.gitleaks.toml` allowlist with valid anchored Go
regexes. **Since v0.11.0 (DF-001, commit 9a54e79) the generated entries are
valid — fresh installs need no manual fix here.** Users carrying over a
`.gitleaks.toml` written by an older init (glob-style entries like `*.log`)
should see T1 in Troubleshooting below.

## 4. First guard run (Tier 1)

```bash
gitreins guard
```

This runs Tier 1 static guards: secrets (gitleaks or built-in scanner), lint
(ruff — checker plus `ruff format --check` over the same scope; a formatting
failure fails the lint lane and names the files to reformat), tests (pytest),
static analysis (mypy and friends, if configured), and
LSP diagnostics (if configured and the server is on PATH). Each guard reports
PASS/FAIL. The secrets guard BLOCKS on failure — no exceptions.

Flags:

```bash
gitreins guard --dead-code
gitreins guard --staged-only
gitreins guard --full
```

- `--dead-code` also runs opt-in Python dead-code detection (AST-based; same as
  `dead_code: true` in `.gitreins/config.yaml`).
- `--staged-only` forces diff mode (only packages with staged changes).
- `--full` against an empty index grades the whole tree: the tests lane runs and
  lint covers tracked+untracked Python files instead of skipping.

**Exit codes are the contract** (since v0.13.0, TRUST-001):

| Exit | Meaning |
|---|---|
| `0` | Every gate ran and passed |
| `1` | A gate genuinely failed |
| `2` | DEGRADED pass — a substantive gate (lint/tests/lsp) did no work, and the run is not accepted |

A DEGRADED run prints `Tier 1: DEGRADED PASS (skips: lint=no staged files, ...)`
naming the skipped lane, and marks it `~` in the summary — it is never a bare
green header. `guards.allow_skips: true` (what `gitreins init` writes) turns the
degraded run back into exit 0, which is what makes a docs-only commit possible
on a fresh repo; `false` keeps exit 2 so CI can never read a gate that never ran
as a gate that passed. The judge matches this: a Tier 1 record carrying skipped
steps cannot be merged back (`gitreins worktree merge` refuses it).

Full, untruncated guard output is persisted per run to `.gitreins/logs/guard-*.log`
(the console prints a bounded summary that names the first failing test id and
every secrets scanner that ran). The pre-commit hook runs the same guard on
`git commit`, so a blocked commit and a blocked guard are the same failure; the
one thing that legitimately differs is the change set the run grades — a hook
run grades the staged files, and with an empty index the lint/tests lanes skip by
scope (the situation T4 describes). A lane that could not run at all because its
runner is missing is a skip in both, with the fix in the reason (§1).

## 5. Task workflow (create → work → judge)

```bash
gitreins task create fix-auth "Fix authentication" \
  "Login accepts email+password and returns JWT" \
  "Invalid credentials return 401" \
  "Rate limiting works after 5 failed attempts"

gitreins task start fix-auth

export GITREINS_LLM_API_KEY="your-provider-key"
gitreins task complete fix-auth
```

Notes that match the current CLI:

- `task complete` refuses to change task state when Tier 2 has no credential.
  Use `gitreins task complete --skip-tier2 fix-auth` for an explicit Tier 1-only
  evaluation, or pass `--force` to skip dependency checks.
- Optional `GITREINS_LLM_BASE_URL` and `GITREINS_LLM_MODEL` select a
  non-default provider/model.
- Dependencies: `gitreins task create api-crud "CRUD endpoints" "POST /api/users
  creates a user" --depends-on build` — put `--depends-on` **after** the
  criteria, because the criteria are one repeated positional argument and
  argparse rejects criteria written after an option. `create` does not
  validate the referenced id: `--depends-on` is a naming convention, not a
  foreign key, and a dependency on a task that never exists simply never
  unblocks.
- `gitreins task list` filters with `--status pending|in_progress|complete`, and
  `gitreins task delete <id>` removes a task that was never attempted.

Standalone evaluation of an existing task:

```bash
gitreins judge fix-auth
gitreins judge fix-auth --skip-tier2
gitreins judge fix-auth --async
```

`--async` dispatches the evaluation as a detached background job that survives
the CLI exiting; poll it with `gitreins judge --status <job_id>` (the id is the
job id, not the task id). The Tier 2 evaluator reads code, can run your test
command, and issues a per-criterion PASS/FAIL verdict with evidence.

> **MCP commit rule:** the MCP `commit` tool refuses while any task is
> `in_progress`. Finish tasks (`task complete`, which judges them) or delete
> them (`task delete`) before committing via MCP.

## 6. Browsing verdicts: `report` and `serve`

```bash
gitreins report
gitreins report -n 20
gitreins report --interactive
gitreins serve --port 8616
```

Verdicts are persisted to `.gitreins/history/<date>/<hash>/verdict.json` and, with
the default `history.storage: "git"`, auto-committed to the orphan `gitreins`
branch (never to `main`). `gitreins report` reads local files first and falls back
to the branch, so a fresh clone can still browse history.

`gitreins serve` is the live judgment browser: a local web server (`--port`
default 8616, `--host` default 127.0.0.1) that renders the same verdict
directories, with `--project <name>` to label a scheduler tick ledger and `--open`
to launch a browser. Stop it with Ctrl-C.

## 7. Judge token telemetry

Every Tier 2 evaluation appends one JSON line per step to
`.gitreins/usage.jsonl` (`ts`, `tokens_in`, `tokens_out`, `cache_read`,
`cache_write`, `step`). It is best-effort (never fails an evaluation),
gitignored, and cumulative per context window — sum deltas, not the last line,
because counters reset when the evaluator compacts its context. See README
("Judge token usage") for the field table.

## 8. Isolated worktrees (parallel lanes, disposable verification)

```bash
gitreins task worktree fix-auth
gitreins worktree list
gitreins worktree doctor
gitreins worktree merge fix-auth
```

A task worktree is branch-backed under `../<repo>-wt/<task-id>`; `task worktree`
is idempotent (it reuses an existing tree for the task). `worktree list` shows
task, branch, state, phase, age and cap from the shared canonical registry, and
`worktree doctor` validates that registry resolution before you rely on it.
`worktree merge` is judge-gated: it refuses to merge a verdict carrying Tier 1
skips, and `--force` (with `--actor` and `--reason` recorded) bypasses only the
verdict gate — every Git safety check still applies.

For throwaway verification without a task:

```bash
gitreins worktree fresh --cmd "<shell command>"
gitreins worktree repro --cmd "<shell command>" -k 3
gitreins worktree dogfood --skip-judge
gitreins worktree clean
```

`fresh` runs one command in a clean detached tree, `repro` runs the same command
N times from one captured `HEAD` to measure flakiness (the JSON record carries
`pass_rate` and per-run exit codes), and `dogfood` exercises `init`, task
creation/start, `guard` and the judge inside a throwaway checkout. `clean` reaps
merged and failed worktrees immediately, and asks for confirmation before
touching stale/orphaned ones. Fleet lanes (`gitreins worktree fleet lanes.json`)
run an explicit manifest of independent tasks concurrently — see README's
"Parallel worktree fleet".

## 9. What else the CLI gives you

- `gitreins commit <message>` / `gitreins commit-audit` — commit through the
  harness, and validate a commit message against the staged diff.
- `gitreins mcp-server` — the MCP stdio server (13 tools) for AI agents, with
  `configure` for runtime LLM hot-reload and `propagate` to fan guard config out
  to sibling repos.
- `gitreins security-scan` — the opt-in Antares CVE-localization scanner.
- `gitreins setup-tools` — which static-analysis/LSP tools are available and how
  to install the missing ones.

## Troubleshooting

### T1. Secrets guard fails with a Go panic dump every run

**Status: FIXED in v0.11.0** (DF-001, commit 9a54e79) — `gitreins init` now
generates valid anchored regexes, so fresh installs never hit this. This
entry is kept for users with a `.gitleaks.toml` written by an older init.

**Symptom (pre-fix):** `✗ secrets — ○` plus a Go panic traceback on every
guard, even with no secrets present.

**Cause (pre-fix):** the generated `.gitleaks.toml` contained invalid regexes
(glob-style entries like `*.log`, `*.egg-info/`, `*.spec.md`, `*.md`).

**Fix:** upgrade gitreins and re-run `gitreins init` to regenerate
`.gitleaks.toml`; or rewrite each entry as an anchored regex (`.*\.log`); or
delete `.gitleaks.toml` entirely — the built-in scanner then runs instead of
gitleaks.

### T2. `✗ tests (full)` but `python3 -m pytest` passes locally

**Symptom:** the tests guard fails, but running pytest by hand is green.

**Cause:** the guard runs the bare configured test command, which can't
import the root package (no `pythonpath` configured). This is the default
failure for the most common Python layout.

**Fix:** add a `pytest.ini` (or `[tool.pytest.ini_options]` in
`pyproject.toml`):

```ini
[pytest]
pythonpath = .
```

### T3. The guard summary hides the real test failure

**Symptom:** the tests guard shows FAIL but the console summary does not carry
the assertion text.

**Cause:** the console output is bounded on purpose. Since v0.13.0 the console
names the first failing test id, and the complete output is persisted.

**Fix:** read the run log named at the end of the guard output
(`.gitreins/logs/guard-*.log`), or re-run the test command yourself:

```bash
python3 -m pytest tests/ -x -q
```

### T4. `gitreins guard` exits 2 (DEGRADED PASS) and blocks a docs-only commit

**Symptom:** the guard prints `Tier 1: DEGRADED PASS (skips: lint=no staged
files, tests=no staged files)` and the commit is refused although nothing is
broken. A clean-tree guard run (nothing staged) always looks like this.

**Cause:** nothing gradable was staged (or no linter/test runner was found), so
a substantive gate did no work. GitReins refuses to call that a pass unless the
repo accepts skips.

**Fix:** stage something gradable (`gitreins guard --full` grades the whole tree
without staging anything), set `guards.test_on_clean: true` if the suite must run
on clean-tree runs, or accept degraded runs for this repo with
`guards.allow_skips: true` — which is what `gitreins init` writes for new repos.
Never silence it globally in CI: exit 2 is the signal that a gate never ran.

### T5. `task complete` ends with a FAIL and the evaluator says the LLM call failed

**Symptom:** `gitreins task complete <id>` prints `Completed: <id> → complete`,
then a verdict whose Tier 2 line reads

```
Evaluator error: LLM call failed: LLM request failed after 3 attempts
(provider=openai model=... url=https://.../chat/completions key=<OPENAI_API_KEY (fallback)>)
```

**Cause:** Tier 2 could not reach the provider at all, so **nothing was
judged** — the FAIL is an infrastructure error, not a verdict on the work. The
resolved config is printed because the credential chain falls back through
`GITREINS_LLM_API_KEY` → `NEURALWATT_API_KEY` → `OPENAI_API_KEY` → … → `OPENROUTER_API_KEY`,
so "a key is set" says nothing about which provider it belongs to (the key
itself is never printed — only the env var that supplied it).

**Fix:** correct the credential or endpoint named in the line
(`GITREINS_LLM_API_KEY`, `GITREINS_LLM_BASE_URL`, `GITREINS_LLM_MODEL`), then
re-run the evaluation on the already-complete task:

```bash
gitreins task complete <id> --force      # re-judges; the task stays 'complete'
gitreins task complete <id> --skip-tier2 # Tier 1 only, no LLM needed
```

An unknown task id needs no credential: `task start` / `task complete` /
`task delete` print `Task not found: <id>` and exit 1, exactly like
`gitreins judge`.

### T6. `~ tests (full) — skipped (test runner 'pytest' not found …)`

**Symptom:** the guard (and the first `git commit`) reports the tests lane as
skipped with a reason naming a fix, e.g.

```
~ tests (full) — skipped (pytest is not installed in '.venv/bin/python' —
  run `.venv/bin/python -m pip install pytest` (or `uv sync`), or set
  guards.test_command)
```

**Cause:** the pytest runner named by `guards.test_command` is not on this
machine, so nothing was graded — the lane never started. This is the fresh-box
case (POC-51): `install` + `init` are green, pytest is not installed, and the
reason names which of the four forms is missing — no `pytest` on PATH and
nothing importable, a pinned `interpreter` that does not exist, an interpreter
that exists without pytest in it, or a `.venv/bin/pytest` that was never
created. It is deliberately NOT an `✗` (a missing runner is an environment
gap, not a finding about your code) and NOT a silent `✓`.

**Fix:** install the runner — `pip install pytest` / `uv sync` / the
interpreter named in the reason, or point `guards.test_command` at your own
interpreter. Until you do, the run is a DEGRADED pass: with
`guards.allow_skips: true` (what `install`/`init` write) it exits 0 and the
lane is named as skipped; with `false` it exits 2 (T4). A pytest run that
starts and fails still blocks the commit.

## Checklist: done when

- [ ] `gitreins guard` passes (default guard set: secrets, lint, tests, and
  static analysis where detected; LSP is opt-in)
- [ ] `git commit` works without `--no-verify`
- [ ] A task with criteria gets a judge verdict in `.gitreins/history/`
- [ ] `.gitreins/tasks.yaml` and `.gitreins/usage.jsonl` are in `.gitignore`
- [ ] You know what exit 2 from the guard means (DEGRADED — a gate did nothing)
