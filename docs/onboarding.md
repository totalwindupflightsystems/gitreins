# GitReins Onboarding Guide

Add GitReins to a new project: install, configure, run your first guard, and
drive the task → judge workflow. Written from the 2026-08-03 dogfood report
(`docs/dogfood/2026-08-03-integration.md`) — every troubleshooting section
below comes from a real failure we hit integrating GitReins into a fresh
project.

## 1. Install

```bash
pip install gitreins
```

Then, inside your project repo (must already be a git repository):

```bash
gitreins install
```

`gitreins install` creates:

- `.gitreins/config.yaml` — default config (skipped if already present)
- `.git/hooks/pre-commit` — runs `gitreins guard` on every commit
  (overwritten if a hook already exists)
- `.gitignore` — appends `.gitreins/tasks.yaml` (local task state is never
  committed; it is added to `.gitignore` automatically)

## 2. Smart init

```bash
gitreins init
```

`gitreins init` auto-detects the project language, size, and complexity, and
writes an optimized `.gitreins/config.yaml` (guard set, test mode, evaluator
budgets). Run it after `install` — `install` writes only the default config,
`init` tailors it to your repo.

## 3. Gitleaks allowlist (no action needed)

`gitreins init` generates a `.gitleaks.toml` allowlist with valid anchored
Go regexes. **Since v0.11.0 (DF-001, commit 9a54e79) the generated entries
are valid — fresh installs need no manual fix here.** Users carrying over a
`.gitleaks.toml` written by an older init (glob-style entries like `*.log`)
should see T1 in Troubleshooting below.

## 4. First guard run

```bash
gitreins guard
```

This runs Tier 1 static guards: secrets (gitleaks or built-in scanner),
lint (ruff), tests (pytest), static analysis (mypy and friends, if
configured), and LSP diagnostics (if configured). Each guard
reports PASS/FAIL. The secrets guard BLOCKS on failure — no exceptions.

The pre-commit hook runs the same guard automatically on `git commit`, so a
blocked commit and a blocked guard are the same failure.

## 5. Task workflow (create → work → judge)

```bash
# 1. Create a task with acceptance criteria
gitreins task create fix-auth "Fix authentication" \
  "Login accepts email+password and returns JWT" \
  "Invalid credentials return 401" \
  "Rate limiting works after 5 failed attempts"

# 2. Mark it in progress (optional but recommended)
gitreins task start fix-auth

# 3. Do the work, then complete — this triggers the LLM judge
gitreins task complete fix-auth
```

`task complete` runs Tier 1 guards, then the Tier 2 agentic evaluator, which
reads the code and issues a per-criterion PASS/FAIL verdict. Verdicts are
stored in `.gitreins/history/<date>/<hash>/verdict.json` and browsable with
`gitreins report`.

> **MCP commit rule:** the MCP `commit` tool refuses while any task is
> `in_progress`. Finish tasks (`task complete`, which judges them) or delete
> them (`task delete`) before committing via MCP.

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

**Symptom:** the tests guard shows FAIL but the summary line only shows the
first line of output, not the actual assertion error.

**Cause:** only the last ~2000 characters of guard output are kept, and the
summary prints the first line.

**Fix:** run the failing test command yourself to see the full traceback:

```bash
python3 -m pytest tests/ -x -q
```

## Checklist: done when

- [ ] `gitreins guard` passes (default guard set: secrets, lint, tests, and
  static analysis where detected; LSP is opt-in)
- [ ] `git commit` works without `--no-verify`
- [ ] A task with criteria gets a judge verdict in `.gitreins/history/`
- [ ] `.gitreins/tasks.yaml` is in `.gitignore`
