# GitReins

**Git-Native Agent Co-Harness — static guards + agentic evaluator for AI-assisted code**

[![CI](https://github.com/totalwindupflightsystems/gitreins/actions/workflows/ci.yml/badge.svg)](https://github.com/totalwindupflightsystems/gitreins/actions/workflows/ci.yml)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![PyPI](https://img.shields.io/pypi/v/gitreins)](https://pypi.org/project/gitreins/)

![GitReins Banner](https://raw.githubusercontent.com/totalwindupflightsystems/gitreins/main/assets/banner-dark.jpg)

GitReins lives inside your git repository as a quality harness. It provides MCP tools for task lifecycle management, an agentic evaluator that judges code completeness against task definitions, and git hooks that ensure nothing bypasses the quality gates.

> ✅ **v0.14.0** — the release that puts the post-0.13.0 fixes in front of users. An executable `python -m gitreins` entry point for the interpreter form installed pre-commit hooks pin (that form died with "No module named gitreins.__main__" and blocked every commit instead of running the guard); a truncated `.gitreins/tasks.yaml` is reported loudly with its raw bytes preserved instead of loading as a silent partial list the next write destroys; a zero-work `guard` is a `Tier 1: DEGRADED PASS` with named skip reasons and exit 2 unless `guards.allow_skips` is true (a vacuous green is gone from CI and pre-commit, and `worktree merge` refuses a PASS that carries skips); and an MCP `initialize` that reports the installed release and negotiates one of the four protocol revisions the server implements instead of a frozen `0.1.0` / `2024-11-05`. Also new since 0.13.0: per-verdict evidence recorded next to `verdict.json` (worker brief, driver-log tail, the landed commit patch and the graded working-tree diff), judge token/cost telemetry joined to each verdict and a QA run ledger (`gitreins qa record|list`, `gitreins report`) browsable in `gitreins serve` and the static judgment page, Tier 1 evidence bounded on LINE boundaries with a cap that is a real bound, whole-tree lint that obeys the repo's own ruff configuration (a config-excluded file is never graded; an all-excluded list is a named skip), a board id gate so one id means one finding (`scripts/check_board_ids.py`), and a test suite that is hermetic and load-independent (no ambient provider call, no detached-job or reap race, staticcheck pinned in CI). Standing surface unchanged: LSP diagnostics (14 languages), opt-in static analysis (9 analyzers), commit audit with CVE-scored severity, Anthropic Messages API support, DeepSeek prompt caching telemetry, MCP `propagate`, guard run logs, `gitreins serve`. 2121 tests pass / 58 test files, verified by collection (optional-tool skips vary).

---

## Quick Start

```bash
pip install gitreins
cd /path/to/your-project
gitreins install        # baseline config + pre-commit hook
gitreins init           # smart init — detects language, size, optimal config
```

`install` is the conservative baseline: its config leaves static analysis off.
`init` is the smart opt-in path and enables static analysis for detected
projects such as Python, recording the configured analyzers in the config. An
explicit `static_analysis` setting is preserved on reruns, as is a custom
`guards.test_command`; only the untouched install baseline may be upgraded to
the detected test runner.

**Running from a source checkout** (no pip install): the `gitreins` console
script is not on your PATH — it lives in the repo's virtualenv. Activate it
first in each shell, then the commands above work as written:

```bash
source .venv/bin/activate        # or call .venv/bin/gitreins directly
gitreins --help
```

New to GitReins? Read the [Onboarding Guide](docs/onboarding.md) — full
install → init → first guard run → task workflow, plus troubleshooting for
the most common first-run failures (gitleaks regex config, Python import
setup).

> **uv is optional.** `gitreins init` prefers `uv run pytest` as the test
> command when uv is installed, but a machine without uv still passes the
> tests guard: when the configured `test_command` starts with a runner
> (`uv run` / `pipenv run` / `poetry run`) whose binary is missing from
> PATH, the guard automatically falls back to `python -m pytest ...` and
> prints a warning line. pip-only users never see `uv: command not found`.

## How It Works

1. **Create tasks** — Define criteria via CLI or MCP tools
2. **Work with your AI agent** — Claude, Hermes, Codex, or Pi does code generation
3. **Complete tasks** — `gitreins task complete <id>` triggers automatic evaluation. Tier 2 needs an LLM credential; configure `GITREINS_LLM_API_KEY` (plus optional `GITREINS_LLM_BASE_URL` and `GITREINS_LLM_MODEL`) first. For an explicit Tier-1-only run, use `gitreins task complete --skip-tier2 <id>`.
4. **Tier 1: Static guards** — secrets, build, lint, tests (configurable)
5. **Tier 2: Agentic evaluator** — LLM loop reads files, runs tests, delivers per-criterion PASS/FAIL
6. **Verdicts persisted** — stored in `.gitreins/history/`, browsable via `gitreins report` or the live judgment browser `gitreins serve`. Each verdict keeps the run's own evidence next to it (`worker-brief.md`, `driver-log.tail.txt`, `commit.patch` and, when the tree was dirty, `worktree.patch` — see `GITREINS_WORKER_BRIEF` / `GITREINS_DRIVER_LOG`), so the browser's detail pane shows the brief, the log tail and the patch the judge graded. QA runs (`worktree fresh|repro|dogfood`) record their own verdict in the QA ledger — `gitreins qa list`, and `gitreins qa record` for a run produced outside the harness (a fleet lane, a bunker battery)
7. **Commit through harness** — pre-commit hook runs guards, blocks if checks fail

> **MCP commit rule:** the MCP `commit` tool refuses while any task is
> `in_progress` — completed work must be judged against the task's criteria
> first. Finish tasks with `task.complete` (which runs the quality judge) or
> remove them with `task.delete`, then retry the commit.

## Commands

```
gitreins install                      # Install hooks + config
gitreins init                         # Smart init (language, size, optimal config)
gitreins guard [--dead-code]          # Run Tier 1 static checks (--dead-code: opt-in Python dead-code detection)
gitreins security-scan [-d DIR] [--output text|json] [--force-ml]
                                       # Run the Antares CVE localization scanner
gitreins report [-n N] [--interactive]  # Browse verdict history
gitreins task create <id> <title> [criteria...] [--depends-on ...]
gitreins task start <id>
gitreins task complete <id> [--force] [--skip-tier2]
# Tier 2 requires GITREINS_LLM_API_KEY; optionally set GITREINS_LLM_BASE_URL and GITREINS_LLM_MODEL.
# Use --skip-tier2 for an explicit Tier 1-only evaluation without an LLM key.
gitreins task list [--status pending|in_progress|complete]
gitreins task delete <id>
gitreins judge <id>                   # Evaluate a task
gitreins commit <message>             # Commit with guard checks
gitreins commit-audit [message]       # Validate commit message against staged diff (commit-msg hook)
gitreins setup-tools                  # Show available static analysis tools and install instructions
gitreins mcp-server                   # Run MCP stdio server (for AI agents)
gitreins serve [--repo <path>] [--port <port>] [--project <name>]
                                      # Live judgment browser (local web server)
gitreins qa list [--json]             # QA run ledger (fresh/repro/dogfood verdicts)
gitreins qa record --project <name> [--verdict PASS|FAIL --cell <name>=<status>]
                                      # Record a QA run produced outside the harness
```

### Parallel worktree fleet

A foreman or scheduler can run an explicit JSON/YAML manifest without coupling
to a scheduler implementation. Each lane gets one `../<repo>-wt/<task-id>` tree;
commands run with argv arrays in that tree, bounded by the configured cap.
Optional `guard` and `judge` argv arrays record those phases in the shared
canonical registry.

```bash
gitreins worktree fleet lanes.json
gitreins worktree fleet lanes.json --max-concurrent-worktrees 3
gitreins worktree fleet lanes.json --merge --force-merge --actor release-bot
# Inspect cap, phase, exit status, and retained evidence:
gitreins worktree list
```

Manifest example:

```json
{
  "lanes": [
    {"task_id": "API-1", "priority": 10, "command": ["./worker", "API-1"],
     "guard": ["gitreins", "guard", "--full"]},
    {"task_id": "UI-1", "priority": 20, "command": ["./worker", "UI-1"]}
  ]
}
```

The default cap is 2 and can be overridden in `.gitreins/config.yaml`:

```yaml
worktree_fleet:
  max_concurrent_worktrees: 2
  venv:
    source: .venv
    name: .venv
```

When the configured source exists, every new task tree symlinks it rather than
installing dependencies. A missing source is allowed for non-Python projects;
existing destination files are never replaced. The shared environment is
intentionally not mutated by GitReins, and concurrent dependency installs are
not lane-safe. Successful `--merge` lanes are applied in priority/task-id order
under an advisory lock; normal judge verdict gates remain in force.

---

## Security Scan (optional)

GitReins ships an **opt-in** Tier 1 security guard that localizes
known CVEs against your staged Python code. It is built on the
[Antares CVE localization framework](https://huggingface.co/fdtn-ai/antares-1b)
(FDTN-AI's 1B-parameter model fine-tuned for code-level vulnerability
localization). Until the optional ML stack is installed, the guard
falls back to a keyword-based heuristic that produces
`CVE-SIMULATED` findings so the wiring can be exercised end-to-end.

### CLI

```bash
# Scan staged Python files (default; used by `gitreins guard`).
gitreins security-scan

# Recursively scan a directory instead of staged files.
gitreins security-scan --directory engine/

# Machine-readable output for piping into other tools.
gitreins security-scan --output json

# Require real ML inference — fail if huggingface_hub/transformers
# are not installed (exit code 2). Without this flag the heuristic
# fallback is used.
gitreins security-scan --force-ml
```

Exit codes:

| Code | Meaning |
|---|---|
| 0 | Clean — no findings |
| 1 | One or more findings produced |
| 2 | `--force-ml` requested but ML dependencies are missing |

### Install requirements

The heuristic scanner has no extra dependencies. Real ML inference
requires the optional ML stack:

```bash
pip install huggingface_hub transformers
# Optional, for GPU inference:
pip install torch        # or onnxruntime
```

The model is downloaded on first use into
`~/.cache/gitreins/antares-1b/` and reused on subsequent runs.

### Configuration

Enable the guard in `.gitreins/config.yaml`:

```yaml
defaults:
  security_scan:
    enabled: true              # opt-in: default false
    model: antares-1b          # "antares-1b" | "antares-350m"
    min_confidence: 0.7        # filter by CVSS severity score
    cve_source: nvd            # "nvd" | "github" | "both"
```

| Key | Default | Notes |
|---|---|---|
| `enabled` | `false` | When `true`, the security_scan guard runs alongside other Tier 1 checks |
| `model` | `antares-1b` | HuggingFace model id; `antares-350m` is a smaller variant |
| `min_confidence` | `0.7` | Drop entries whose CVSS score is below this. Severity→score: CRITICAL=1.0, HIGH=0.85, MEDIUM=0.6, LOW=0.3 |
| `cve_source` | `nvd` | `nvd` uses the NVD REST API, `github` uses the GitHub Advisory Database, `both` merges the two |

The CVE feed is cached at `~/.cache/gitreins/cve_feed/` with a
24-hour TTL. When the network is unreachable the feed serves stale
cache; when both cache and network are unavailable the feed returns
an empty list and the guard exits clean (it is **opt-in** and must
never block a commit on missing infrastructure).

---

## Test Modes: `full` vs `diff`

GitReins supports two strategies for when tests run on commit, controlled by `test_mode` in `.gitreins/config.yaml`.

### `test_mode: "full"` (default for new projects)

The entire test suite runs on every commit. Safe and thorough.

**Best for:**
- New projects with a small, fast test suite
- Projects where all tests pass reliably
- When you want maximum safety on every commit

**Tradeoff:** Slow on large projects. Pre-existing failures in untouched code block unrelated commits.

```yaml
guards:
  test_mode: "full"
```

### `test_mode: "diff"` (recommended for mature projects)

Only tests for packages you actually changed. Uses basename mapping:

| Changed file | Test run |
|---|---|
| `engine/guard_manager.py` | `tests/test_guard_manager.py` |
| `gitreins/cli.py` | `tests/test_cli.py` |
| `gitreins_mcp/server.py` | `tests/test_mcp_server.py` |

**Best for:**
- Projects with 5+ packages where full suite is slow
- Projects with pre-existing test failures in untouched code
- When you want fast feedback on the code you actually changed

**Safety nets — diff mode falls back to full suite when:**
- `pyproject.toml`, `.gitreins/config.yaml`, `Makefile`, or `setup.cfg` changed
- A test file itself changed (always included, plus its source-mapped siblings)
- Changed files don't map to any known test files (unknown file = safety)
- No staged files at all
- Test command isn't `pytest` (custom runners can't be narrowed)

**Tradeoff:** Less safety on cross-cutting changes. Config changes always trigger full suite.

```yaml
guards:
  test_mode: "diff"
```

### Which mode should I use?

| Project state | Recommended mode |
|---|---|
| Brand new, <5 packages | `full` |
| Mature, 5+ packages, tests pass | `diff` |
| Mature, pre-existing test failures | `diff` |
| Refactoring across packages | `full` (temporarily) |
| CI / PR checks | `full` (safety over speed) |

### Output examples

**Full mode:**
```
Tier 1 Guards: PASS  (test mode: full)
  ✓ secrets — clean (gitleaks + builtin cross-check)
  ✓ lint — ok
  ✓ tests — passed
```

**Diff mode (targeted):**
```
Tier 1 Guards: PASS  (test mode: diff, 3 test file(s))
  ✓ secrets — clean (gitleaks + builtin cross-check)
  ✓ tests — passed
```

**Diff mode (safety trigger — full suite):**
```
Tier 1 Guards: PASS  (test mode: diff, full suite — safety trigger)
  ✓ secrets — clean (gitleaks + builtin cross-check)
  ✓ tests — passed
```

**Degraded pass (a gate did no work — `guards.allow_skips` decides the exit code):**
```
Tier 1: DEGRADED PASS (skips: lint=no staged files, tests=no staged files)  (test mode: diff)
  ✓ secrets — clean (gitleaks + builtin cross-check)
  ~ lint — skipped (no staged files)
  ~ tests — skipped (no staged files)
```

**Failure diagnostics (named, not just counted):**
```
Tier 1 Guards: FAIL  (test mode: full)
  ✗ secrets — FAIL (builtin cross-check: 2 findings; gitleaks: clean)
      findings: 2 finding(s): .env:1, src/db.py:7
  ✗ tests (full) — FAIL (tests/test_probe.py::test_boom [first failing id]; 3 failure(s))
  guard log: .gitreins/logs/guard-20260916T200106.123456Z.log
```

`~` marks a step that was skipped, and a degraded run never prints the green
`Tier 1 Guards: PASS` header — so grepping that string is proof the gates
actually ran. With `guards.allow_skips: false` (code default) it exits **2**;
`gitreins init` writes `allow_skips: true` for ergonomic first commits.

**Full mode on a clean tree (`gitreins guard --full`):**
```
Tier 1 Guards: PASS  (test mode: full, whole tree)
  ✓ secrets — clean (gitleaks + builtin cross-check)
  ✓ lint — ok (42 tracked files)
  ✓ tests (full)
```

`--full` grades the whole tree even with an empty index: the tests lane runs
the configured `guards.test_command`, and lint covers tracked +
untracked-but-not-ignored Python files (`ruff: clean (N tracked files)`
names the graded scope) instead of returning the `no staged files` skip.
The plain PASS header is the tell — a whole-tree run prints no `skips:`
clause. `--staged-only` and a bare `gitreins guard` keep the degraded-pass
skip semantics above; staged files always take precedence over the tree
when the index is non-empty.

A failure line names what broke instead of only counting it: the tests line
carries the **first failing test id** parsed from the pytest output (with the
failure count), and the secrets line names **every scanner that ran** plus each
one's outcome — `gitleaks`, the built-in low-entropy cross-check, or both —
because a finding raised only by the cross-check is a different problem from one
gitleaks reported. Both facts are also recorded in the persisted run log
(`.gitreins/logs/guard-*.log`) under `diagnostics:`, so a post-mortem does not
need to re-run pytest or the scanners.

---

## Verdict History

Every `gitreins task complete` and `gitreins judge` saves a verdict to `.gitreins/history/`. Configure in `.gitreins/config.yaml`:

```yaml
history:
  enabled: true              # false = don't save verdicts
  storage: "git"             # "git" = auto-commit to gitreins branch
                             # "filesystem" = write files only, no git commits
  max_verdicts: 1000         # auto-prune old entries
```

Browse history:

```bash
gitreins report              # last 10 evaluations
gitreins report -n 20        # last 20
gitreins report --interactive  # TUI with arrow-key navigation (requires textual)
```

### Branch mechanics (git storage)

With `storage: "git"` (the default), every verdict is auto-committed to a
dedicated orphan `gitreins` branch — never to `main`. The branch is only
checked out transiently (or updated via a temporary worktree), so your
working tree is never disturbed. `.gitreins/history/` is intentionally
gitignored: the verdict files are runtime artifacts whose canonical home is
the `gitreins` branch, and a fresh clone therefore has no local
`.gitreins/history/` directory.

`gitreins report` reads verdicts in this order:

1. **Local filesystem** — `.gitreins/history/` in the working tree (used
   when present, e.g. right after a judge run in the same checkout).
2. **`gitreins` branch fallback** — when the local directory is missing or
   empty and storage is `"git"`, verdicts are read straight from the branch
   (`git ls-tree` / `git show`), so a fresh clone can still browse the full
   verdict history.

To inspect the branch directly:

```bash
git log --oneline gitreins                                            # verdict commits
git ls-tree -r --name-only gitreins -- .gitreins/history              # stored files
git show gitreins:.gitreins/history/<date>/<hash>/verdict.json        # one verdict
```

With `storage: "filesystem"`, verdicts are written locally only — no branch
is created and the fallback is skipped.

### Judge token usage (`.gitreins/usage.jsonl`)

Every Tier 2 evaluation appends one telemetry line to
`<workdir>/.gitreins/usage.jsonl` as its evidence step completes, so judge
token spend can be summed by external tooling (fleet dashboards, cost
reports). GitReins uses its own LLM client, so this usage never appears in the
telemetry of the agent that invoked it — this file is the only record.

```json
{"ts": 1757973600.42, "tokens_in": 41250, "tokens_out": 1863, "cache_read": 0, "cache_write": 0, "step": "ai_eval"}
```

| Field | Meaning |
|---|---|
| `ts` | Unix epoch seconds of the write |
| `tokens_in` | Cumulative input tokens for the evaluation so far (cache reads included) |
| `tokens_out` | Cumulative output tokens for the evaluation so far |
| `cache_read` / `cache_write` | Cumulative cached-prompt tokens (0 for providers without prompt caching) |
| `step` | Pipeline step id that wrote the line (`ai_eval` for the judge step) |

Three details matter when you consume it:

- **Cumulative, not per-call.** Each line reports the evaluation's running
  totals. Sum line-to-line deltas; the final line alone is the run total only
  when nothing reset the window.
- **Counters reset on compaction.** When the evaluator compacts its context,
  the token counters restart, so a later line can be numerically smaller than
  an earlier one. A consumer that assumes monotonic growth undercounts.
- **Best-effort, never fatal.** A write failure (permissions, full disk) is
  swallowed and the evaluation continues; absence of a line is not evidence
  that the judge did not run — read the verdict for that.

The file is runtime state: `gitreins install` / `gitreins init` add
`.gitreins/usage.jsonl` to `.gitignore`, and it is never auto-committed. It
carries no task id, model name, or credentials — correlate it with
`.gitreins/history/<date>/<hash>/verdict.json` by timestamp when you need
per-task attribution. `gitreins serve` does exactly that join for you: each
verdict's detail pane shows the tokens it spent (and a cost when the checkout
configures `usage.price_per_1m_input/_output`), and the stats header shows the
aggregate — see [Judgment Viewer](docs/judgment-viewer.md#judge-telemetry-tokens-and-cost-per-judgment).

## Task Dependencies

Tasks can depend on other tasks. Evaluation is blocked until dependencies pass:

```bash
gitreins task create build "Project builds" \
  "CGO_ENABLED=0 go build ./cmd/server exits 0"

gitreins task create api-crud "CRUD endpoints" \
  "POST /api/users creates a user" \
  "GET /api/users lists users" \
  --depends-on build

gitreins task complete api-crud
# → "Cannot complete 'api-crud' — depends on incomplete tasks: build"

gitreins task complete build      # complete the dependency first
gitreins task complete api-crud   # now this works

# Or force-skip dependency checks:
gitreins task complete api-crud --force
```

**Flag placement matters.** A task's criteria are one repeated positional
argument, so argparse cannot interleave them with an option: put
`--depends-on` (or `--depends-on <id>` repeated) *after* the criteria. Writing
criteria after the flag fails with `unrecognized arguments`:

```bash
# REJECTED — the criteria after the option are not parsed as criteria:
#   gitreins task create api-crud "CRUD endpoints" --depends-on build \
#     "POST /api/users creates a user"
#   gitreins: error: unrecognized arguments: POST /api/users creates a user
```

`scripts/check_cli_examples.py` replays every documented example through the
real CLI parser in CI, so this class of broken example cannot ship again.

## Configuration

Full `.gitreins/config.yaml` reference:

```yaml
# ── Global defaults ──────────────────────────────────
defaults:
  model: deepseek-v4-flash
  max_iterations: 100
  check_for_updates: true

# ── Tier 1 guards ────────────────────────────────────
guards:
  secrets: true
  lint: true
  tests: true
  test_mode: "full"          # "full" or "diff"
  # uv/pipenv/poetry are OPTIONAL — if the runner prefix's binary is not on
  # PATH, the guard falls back to `python -m pytest ...` with a warning.
  test_command: "uv run pytest -x --tb=short"
  # Run test_command even with an empty index (nothing staged). Default false
  # means the tests lane is a SKIP with a named reason ("no staged files") —
  # under allow_skips:false that makes the whole run a DEGRADED pass (exit 2).
  # Set true when the suite must run on clean-tree guard runs too (chained
  # suites, audits, or commits that land through another tool).
  test_on_clean: false
  # A run where a substantive gate (lint/tests/lsp) did NO work — nothing
  # staged, no linter on PATH, zero tests collected — is a DEGRADED PASS.
  # true  = degraded runs still exit 0 (gitreins init writes this default)
  # false = degraded runs exit 2, so CI can never read a gate that never ran
  #         as a gate that passed
  allow_skips: true

  # Go projects (auto-detected via go.mod):
  go:
    build: true
    lint: true
    tests: true

# ── Tier 2 evaluator caps ────────────────────────────
evaluator:
  max_iterations: 25         # LLM reasoning turns
  max_time: "5m"             # wall clock cap
  max_input_tokens: "200k"
  max_output_tokens: "50k"
  tool_call_weight: 0.1      # tool calls cost 0.1 iterations

# ── Verdict history ──────────────────────────────────
history:
  enabled: true
  storage: "git"
  max_verdicts: 1000
```

---

## Tech Stack

- **Language:** Python 3.10+
- **Dependencies:** mcp, pyyaml, requests, packaging (4 packages)
- **MCP Transport:** stdio (12 tools)
- **Config:** YAML in `.gitreins/` directory
- **Evaluator Default Model:** DeepSeek V4 Flash (~$0.01/eval)
- **Test suite:** 2121 tests across 58 test files (collection total; optional-tool skips vary)

## Architecture & Docs

- [Disposable verification](docs/disposable-verification.md) — run QA batteries, dogfood, and repro farms in throwaway worktrees without a bunker. Every QA run records its outcome in the QA ledger (`gitreins qa list`), including runs produced outside the harness (`gitreins qa record`).

| Document | What it covers |
|---|---|
| [Full Architecture](docs/architecture.md) | System design and data flow |
| [Component Map](docs/component-map.md) | Module inventory with paths and line counts |
| [Agentic Evaluator Design](docs/evaluator-loop.md) | How the evaluator loop works |
| [Judgment Viewer](docs/judgment-viewer.md) | Verdict browser: API contract, data sources, security model, `--repo` |

## License

MIT
