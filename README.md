# GitReins

**Git-Native Agent Co-Harness — static guards + agentic evaluator for AI-assisted code**

[![CI](https://github.com/totalwindupflightsystems/gitreins/actions/workflows/ci.yml/badge.svg)](https://github.com/totalwindupflightsystems/gitreins/actions/workflows/ci.yml)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![PyPI](https://img.shields.io/pypi/v/gitreins)](https://pypi.org/project/gitreins/)

![GitReins Banner](https://raw.githubusercontent.com/totalwindupflightsystems/gitreins/main/assets/banner-dark.jpg)

GitReins lives inside your git repository as a quality harness. It provides MCP tools for task lifecycle management, an agentic evaluator that judges code completeness against task definitions, and git hooks that ensure nothing bypasses the quality gates.

> ✅ **v0.10.2** — LSP diagnostics (14 languages), static analysis (9 tools), commit audit with CVE-scored severity, DeepSeek prompt caching, large-repo hardening, 1088 tests pass.

---

## Quick Start

```bash
pip install gitreins
cd /path/to/your-project
gitreins install        # creates .gitreins/config.yaml + pre-commit hook
gitreins init           # smart init — detects language, size, optimal config
```

## How It Works

1. **Create tasks** — Define criteria via CLI or MCP tools
2. **Work with your AI agent** — Claude, Hermes, Codex, or Pi does code generation
3. **Complete tasks** — `gitreins task complete <id>` triggers automatic evaluation
4. **Tier 1: Static guards** — secrets, build, lint, tests (configurable)
5. **Tier 2: Agentic evaluator** — LLM loop reads files, runs tests, delivers per-criterion PASS/FAIL
6. **Verdicts persisted** — stored in `.gitreins/history/`, browsable via `gitreins report`
7. **Commit through harness** — pre-commit hook runs guards, blocks if checks fail

## Commands

```
gitreins install                      # Install hooks + config
gitreins init                         # Smart init (language, size, optimal config)
gitreins guard                        # Run Tier 1 static checks
gitreins report [-n N] [--interactive]  # Browse verdict history
gitreins task create <id> <title> [criteria...] [--depends-on ...]
gitreins task start <id>
gitreins task complete <id> [--force]
gitreins task list [--status pending|in_progress|complete]
gitreins task delete <id>
gitreins judge <id>                   # Evaluate a task
gitreins commit <message>             # Commit with guard checks
gitreins mcp-server                   # Run MCP stdio server (for AI agents)
```

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
  ✓ secrets — clean
  ✓ lint — ok
  ✓ tests — passed
```

**Diff mode (targeted):**
```
Tier 1 Guards: PASS  (test mode: diff, 3 test file(s))
  ✓ secrets — clean
  ✓ tests — passed
```

**Diff mode (safety trigger — full suite):**
```
Tier 1 Guards: PASS  (test mode: diff, full suite — safety trigger)
  ✓ secrets — clean
  ✓ tests — passed
```

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

## Task Dependencies

Tasks can depend on other tasks. Evaluation is blocked until dependencies pass:

```bash
gitreins task create build "Project builds" \
  "CGO_ENABLED=0 go build ./cmd/server exits 0"

gitreins task create api-crud "CRUD endpoints" --depends-on build \
  "POST /api/users creates a user" \
  "GET /api/users lists users"

gitreins task complete api-crud
# → "Cannot complete 'api-crud' — depends on: build"

gitreins task complete build      # complete the dependency first
gitreins task complete api-crud   # now this works

# Or force-skip dependency checks:
gitreins task complete api-crud --force
```

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
  test_command: "pytest -x --tb=short"

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
- **MCP Transport:** stdio (26 tools)
- **Config:** YAML in `.gitreins/` directory
- **Evaluator Default Model:** DeepSeek V4 Flash (~$0.01/eval)
- **Test suite:** ~410 tests, real LLM integration tests included

## Architecture & Docs

| Document | What it covers |
|---|---|
| [Full Architecture](docs/architecture.md) | System design and data flow |
| [Component Map](docs/component-map.md) | Module inventory with paths and line counts |
| [Agentic Evaluator Design](docs/evaluator-loop.md) | How the evaluator loop works |

## License

MIT
