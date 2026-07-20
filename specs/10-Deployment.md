# 10-Deployment.md — Deployment & Release

> **Document Status:** Draft | **Last Updated:** 2026-07-19 | **Author:** GitReins Deployment Spec

---

## 1. PyPI Distribution

GitReins is distributed as a pure-Python package via the Python Package Index (PyPI). The package manifest is declared in `pyproject.toml` at the repository root.

### 1.1 Package Manifest (`pyproject.toml`)

```toml
[build-system]
requires = ["setuptools>=68.0", "wheel"]
build-backend = "setuptools.build_meta"

[project]
name = "gitreins"
version = "0.6.0"                    # ← Must match engine/version.py
description = "Git-native AI agent co-harness — MCP server, static guards, and agentic evaluator for LLM-assisted coding"
readme = "README.md"
license = {text = "MIT"}
authors = [{name = "Bane"}]
keywords = ["git", "ai", "llm", "mcp", "code-review", "agent"]
classifiers = [
    "Development Status :: 3 - Alpha",
    "Intended Audience :: Developers",
    "License :: OSI Approved :: MIT License",
    "Programming Language :: Python :: 3",
    "Programming Language :: Python :: 3.10",
    "Programming Language :: Python :: 3.11",
    "Programming Language :: Python :: 3.12",
    "Topic :: Software Development :: Quality Assurance",
    "Topic :: Software Development :: Version Control :: Git",
]
requires-python = ">=3.10"
dependencies = [
    "mcp>=1.0.0",
    "pyyaml>=6.0",
    "requests>=2.28",
    "packaging>=21.0",
]

[project.optional-dependencies]
dev = [
    "pytest>=7.0",
    "twine>=5.0",
    "build>=1.0",
]

[project.scripts]
gitreins = "gitreins.cli:main"

[project.urls]
Homepage = "https://github.com/totalwindupflightsystems/gitreins"
Repository = "https://github.com/totalwindupflightsystems/gitreins"
Issues = "https://github.com/totalwindupflightsystems/gitreins/issues"

[tool.setuptools.packages.find]
include = ["engine*", "gitreins*", "gitreins_mcp*"]

[tool.pytest.ini_options]
testpaths = ["tests"]
pythonpath = ["."]
```

### 1.2 Version Synchronization

The version is maintained in **two locations** that must always match:

| File | Format | Example |
|------|--------|---------|
| `engine/version.py` | Python string | `__version__ = "0.6.0"` |
| `pyproject.toml` | TOML string | `version = "0.6.0"` |

Both files are read by different consumers:
- `engine/version.py` — runtime version string (CLI `--version`, MCP server `serverInfo`, update checker)
- `pyproject.toml` — packaging version (PyPI, `pip install`, `pip show`)

**Rule:** Any release that bumps one must bump the other. The release workflow enforces this by editing both files in the same commit.

### 1.3 Build Artifacts

The `python3 -m build --wheel` command produces:
- `dist/gitreins-X.Y.Z-py3-none-any.whl` — universal wheel (pure Python, no platform restriction)
- `dist/gitreins-X.Y.Z.tar.gz` — source distribution

Only the wheel is uploaded to PyPI; the sdist is kept as a backup artifact.

### 1.4 Entry Point

The `[project.scripts]` table registers `gitreins` as a console script. After `pip install gitreins`, the `gitreins` command is available on `$PATH` and resolves to `gitreins.cli:main()`.

---

## 2. Release Workflow

The release process is a manual, six-step sequence executed by a maintainer with PyPI credentials.

### 2.1 Step-by-Step Release

| Step | Command | Purpose |
|------|---------|---------|
| 1. Bump version | Edit `engine/version.py` and `pyproject.toml` | Update version string in both files |
| 2. Commit | `git add engine/version.py pyproject.toml && git commit -m "chore: bump version to vX.Y.Z"` | Record version change |
| 3. Tag | `git tag -a vX.Y.Z -m "Release vX.Y.Z"` | Annotated git tag for the release |
| 4. Build | `python3 -m build --wheel` | Produce wheel artifact |
| 5. Publish | `twine upload dist/*.whl` | Upload to PyPI (requires `TWINE_USERNAME` / `TWINE_PASSWORD` or API token) |
| 6. Push tags | `git push origin main && git push origin vX.Y.Z` | Push commit and tag to remote |

### 2.2 Pre-Release Checklist

Before tagging a release, verify:
- [ ] `engine/version.py` matches `pyproject.toml`
- [ ] All tests pass: `pytest tests/ -v`
- [ ] Guards pass: `gitreins guard`
- [ ] CHANGELOG updated (if maintained)
- [ ] README badges reflect new version
- [ ] No uncommitted changes: `git status` is clean

### 2.3 Versioning Scheme

GitReins follows [Semantic Versioning](https://semver.org/) (SemVer):
- **MAJOR** (X) — Breaking changes to CLI, MCP protocol, or config format
- **MINOR** (Y) — New features, new guards, new tools (backward compatible)
- **PATCH** (Z) — Bug fixes, performance improvements, documentation updates

---

## 3. GitHub Actions CI

The repository uses GitHub Actions for continuous integration. The workflow is defined in `.github/workflows/ci.yml`.

### 3.1 Current CI Workflow (`ci.yml`)

```yaml
name: CI
on:
  push:
    branches: [main]
  pull_request:
    branches: [main]
jobs:
  test:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        python-version: ["3.10", "3.11", "3.12"]
    steps:
      - uses: actions/checkout@v4
      - name: Set up Python ${{ matrix.python-version }}
        uses: actions/setup-python@v5
        with:
          python-version: ${{ matrix.python-version }}
      - name: Install dependencies
        run: |
          python -m pip install --upgrade pip
          pip install -e ".[dev]"
          # Optional Tier 1 diagnostics for repositories that enable them:
          python -m pip install mypy pyright
      - name: Run tests
        run: pytest tests/ -v --tb=short
      - name: Run guards
        run: gitreins guard
```

### 3.2 Planned CI Jobs

The current single-job CI is planned to split into two jobs with different cost and trigger profiles:

#### Job A: `gitreins-guard` (Fast, Free, Every Push)

| Property | Value |
|----------|-------|
| **Trigger** | Every `push` and `pull_request` to `main` |
| **Cost** | Free — no LLM tokens |
| **Runtime** | ~30-60 seconds |
| **Steps** | `pip install -e ".[dev]"` → `bash .gitreins/pre-commit` |

This job runs the Tier 1 static guards (secrets, lint, tests, plus any enabled dead-code, LSP, or static-analysis checks) on every commit. It does not invoke the agentic evaluator and therefore consumes no LLM API tokens. It is the fast path that blocks obviously broken changes before they reach the merge queue.

The `.gitreins/pre-commit` script (created by `gitreins install`) is equivalent to running `gitreins guard` but executed as a standalone bash script for CI portability.

#### Job B: `gitreins-eval` (LLM Tokens, Manual/Schedule/PR)

| Property | Value |
|----------|-------|
| **Trigger** | Manual (`workflow_dispatch`), scheduled (`cron`), or on PRs that modify `engine/evaluator.py` or `engine/judge.py` |
| **Cost** | LLM tokens — configurable cap per run |
| **Runtime** | 2-10 minutes depending on task complexity |
| **Steps** | Clone engine → `PYTHONPATH=. python3 eval-runner.py --all` |

This job runs the full evaluation pipeline (Tier 1 + Tier 2) against a benchmark suite. It requires a `GITREINS_LLM_API_KEY` secret configured in the repository settings. The `eval-runner.py` script is a standalone evaluation harness that exercises the agentic evaluator against a set of reference tasks.

**Environment variables required:**
- `GITREINS_LLM_API_KEY` — Repository secret (Settings → Secrets → Actions)
- `GITREINS_WORKDIR` — Set to the repository root

**Cap configuration:** The job should set conservative caps to limit token spend per CI run:
```yaml
env:
  GITREINS_LLM_API_KEY: ${{ secrets.GITREINS_LLM_API_KEY }}
  GITREINS_EVAL_CAP: "20/5m/50k/10k"
```

---

## 4. MCP Server Deployment

The GitReins MCP server enables AI agents (Hermes, Claude, Codex, Pi) to interact with GitReins through the Model Context Protocol.

### 4.1 Installation

```bash
pip install gitreins
```

The MCP server is included in the package as the `gitreins_mcp` module. No separate installation is required.

### 4.2 Hermes Agent Integration

To register the MCP server with Hermes Agent, add a wrapper script and configure the MCP server list:

**Step 1: Create wrapper script** (`~/.hermes/scripts/gitreins-mcp`)

```bash
#!/bin/bash
# GitReins MCP server wrapper for Hermes
# Usage: hermes mcp add gitreins /path/to/this/wrapper

export GITREINS_WORKDIR="${GITREINS_WORKDIR:-.}"
python3 -m gitreins_mcp.server "$GITREINS_WORKDIR"
```

Make it executable:
```bash
chmod +x ~/.hermes/scripts/gitreins-mcp
```

**Step 2: Register with Hermes**

```bash
hermes mcp add gitreins ~/.hermes/scripts/gitreins-mcp
```

**Step 3: Restart Hermes gateway**

```bash
hermes gateway restart
```

After restart, the `gitreins` MCP tools are available in Hermes sessions:
- `task.create`, `task.start`, `task.complete`, `task.list`, `task.get`, `task.delete`
- `guard.run`
- `judge.evaluate`
- `commit`

### 4.3 Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `GITREINS_WORKDIR` | No | `"."` | Default working directory for the MCP server. All tool calls resolve relative paths against this directory. |
| `GITREINS_LLM_API_KEY` | No | `""` | API key for LLM evaluation. If absent, `task.complete` skips Tier 2 evaluation and returns the task with a note. |

### 4.4 Cross-Repository Usage

Every MCP tool accepts an optional `workdir` parameter, enabling a single MCP server instance to manage tasks across multiple repositories:

```json
{
  "name": "task.create",
  "arguments": {
    "id": "fix-auth",
    "title": "Fix authentication",
    "criteria": ["..."],
    "workdir": "/home/user/projects/my-app"
  }
}
```

The `workdir` parameter overrides `GITREINS_WORKDIR` for that specific tool call.

---

## 5. GitLab Mirror Sync

The repository is mirrored to GitLab for redundancy and alternative CI pipelines. The sync process uses GitLab's API to temporarily unprotect the main branch, push with lease verification, and re-protect.

### 5.1 Sync Workflow

| Step | API Call | Purpose |
|------|----------|---------|
| 1. Unprotect | `PUT /api/v4/projects/:id/protected_branches/main` | Remove branch protection to allow force-push |
| 2. Push | `git push --force-with-lease origin main` | Push with lease verification (fails if remote has diverged) |
| 3. Re-protect | `POST /api/v4/projects/:id/protected_branches` | Restore branch protection with previous settings |

### 5.2 API Details

**Unprotect branch:**
```bash
curl --request DELETE \
  --header "PRIVATE-TOKEN: $GITLAB_TOKEN" \
  "https://gitlab.com/api/v4/projects/$PROJECT_ID/protected_branches/main"
```

**Re-protect branch (with previous settings):**
```bash
curl --request POST \
  --header "PRIVATE-TOKEN: $GITLAB_TOKEN" \
  --header "Content-Type: application/json" \
  --data '{
    "name": "main",
    "push_access_levels": [{"access_level": 40}],
    "merge_access_levels": [{"access_level": 30}]
  }' \
  "https://gitlab.com/api/v4/projects/$PROJECT_ID/protected_branches"
```

**Environment variables:**
- `GITLAB_TOKEN` — Personal access token with `api` scope
- `PROJECT_ID` — GitLab project numeric ID

### 5.3 Safety

- `--force-with-lease` ensures the push only succeeds if the remote ref matches the local expectation. If someone else pushed since the last fetch, the push fails safely.
- The unprotect/re-protect window should be as short as possible (ideally <5 seconds) to minimize exposure.
- This sync is typically run by a cron job or GitHub Action after successful CI on `main`.

---

## 6. Version History

GitReins has evolved through six minor releases, each adding significant capabilities to the harness.

### 6.1 Release Timeline

| Version | Date | Focus | Key Changes |
|---------|------|-------|-------------|
| **v0.1.0** | Initial | Proof of concept | Core engine, CLI, MCP server, basic guards |
| **v0.1.2** | Patch | Config loading fix | Fixed config.yaml loading edge cases; hardened default resolution |
| **v0.1.3** | Patch | Go project support | Added `go vet`, `go test`, `go build` guards; auto-detection via `go.mod` |
| **v0.2.0** | Minor | Evaluator caps | Introduced `eval_cap` combined string format (`"100/30m/200k/50k"`) for resource limiting |
| **v0.3.0** | Minor | Individual cap keys | Replaced combined string with individual keys (`max_iterations`, `max_time`, `max_input_tokens`, `max_output_tokens`, `tool_call_weight`) |
| **v0.4.0** | Minor | Unified defaults + cache token tracking | Single-source defaults in `engine/config.py`; added cache token tracking to `LLMUsage` |
| **v0.5.0** | Minor | Update checker | Background version check against PyPI; `check_for_updates` config flag; `update_check_ttl_hours` |
| **v0.6.0** | Minor | Diff-mode test selection | Smart test selection based on staged files; `guards.test_mode: "diff"` vs `"full"`; force-full triggers for config changes |

### 6.2 Version Milestones

**v0.1.2 — Config Loading Fix**
- Fixed race condition in config.yaml parsing when file was empty or malformed
- Hardened `overlay()` to handle missing keys gracefully
- Added fallback to hardcoded defaults when Layer 2 config is unreadable

**v0.1.3 — Go Project Support**
- Auto-detects Go projects via `go.mod` presence
- Adds three Go-specific guards: `go_lint` (`go vet`), `go_tests` (`go test`), `go_build`
- Go guards run in parallel with Python guards
- No additional configuration required — fully automatic

**v0.2.0 — Evaluator Caps (Combined String)**
- Introduced `eval_cap` field in config and `Judge` constructor
- Format: `"iterations/time/input_tokens/output_tokens"`
- Example: `"100/30m/200k/50k"` means 100 iterations, 30 minutes, 200K input tokens, 50K output tokens
- Caps checked before each LLM call; hard limits on time and tokens

**v0.3.0 — Individual Cap Keys + Tool-Call Discount**
- Replaced combined string with individual YAML keys for clarity
- Added `tool_call_weight: 0.1` — each tool call costs 0.1 iterations instead of 1.0
- Enables fine-grained control: `max_iterations: 100`, `max_time: "10m"`, `max_output_tokens: "50k"`
- Legacy combined string still supported for backward compatibility

**v0.4.0 — Unified Defaults + Cache Token Tracking**
- Centralized all defaults in `GitReinsDefaults` dataclass (`engine/config.py`)
- Config load order: hardcoded defaults → repo config overlay → explicit constructor params
- Added `cache_read_tokens` and `cache_write_tokens` to `LLMUsage` for cost optimization visibility

**v0.5.0 — Update Checker**
- Background check against PyPI API on first `gitreins` command (if `check_for_updates: true`)
- Respects `update_check_ttl_hours` (default 24) to avoid excessive API calls
- Warns user if installed version < latest PyPI version
- Can be disabled per-repo via `.gitreins/config.yaml`

**v0.6.0 — Diff-Mode Test Selection**
- `guards.test_mode: "diff"` runs only tests related to staged files
- Basename matching: `engine/foo.py` → `tests/test_foo.py`
- Force-full triggers: changes to `pyproject.toml`, `conftest.py`, `.gitreins/config.yaml`, CI workflows, or `Makefile` run the full suite
- Falls back to full suite if no test mapping found (safety default)

---

## 7. Installation

### 7.1 End-User Installation (PyPI)

```bash
# Install from PyPI
pip install gitreins

# Verify installation
gitreins --version
# Output: gitreins 0.6.0
```

### 7.2 Development Installation (Source)

```bash
# Clone the repository
git clone https://github.com/totalwindupflightsystems/gitreins.git
cd gitreins

# Create virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install in editable mode with dev dependencies
pip install -e ".[dev]"

# Verify
gitreins guard
```

### 7.3 Optional LSP and Static-Analysis Prerequisites

LSP and static-analysis guards are opt-in. Install only the servers and analyzers enabled in the target repository's `guards.lsp_tools` and `guards.static_analysis_tools` configuration; GitReins skips tools that are not on `PATH`.

| Language | LSP server install | Static-analysis install |
|----------|--------------------|-------------------------|
| Python | `python -m pip install python-lsp-server pyflakes pycodestyle` | `python -m pip install mypy pyright` |
| Go | `go install golang.org/x/tools/gopls@latest` | `go install honnef.co/go/tools/cmd/staticcheck@latest` |
| Rust | `rustup component add rust-analyzer` | `rustup component add clippy` |
| TypeScript/JavaScript | `npm install -g typescript typescript-language-server` | `npm install -g eslint` |
| C/C++ | Install `clangd` through the system package manager | `sudo apt install cppcheck` (or `brew install cppcheck`) |
| Java/Kotlin | Install `jdtls` or `kotlin-language-server` through the system package manager | Configure a language-native analyzer as needed |
| Ruby | `gem install ruby-lsp` or `gem install solargraph` | `gem install sorbet && srb init` |

**CI example:**

```yaml
- name: Install configured diagnostic tools
  run: |
    python -m pip install python-lsp-server pyflakes pycodestyle mypy pyright
    go install golang.org/x/tools/gopls@latest
    go install honnef.co/go/tools/cmd/staticcheck@latest
    npm install -g typescript typescript-language-server eslint
```

**Extras note:** the current package manifest declares the `dev` extra, which includes `python-lsp-server` and `mypy`; use `pip install -e ".[dev]"` for the supported development bundle. `pip install "gitreins[full]"` is not currently a supported installation because no `full` extra is declared; consumers requiring a full diagnostics profile must install the selected tools explicitly as shown above.

### 7.4 Per-Project Activation

After installing the package, activate GitReins in any repository:

```bash
cd /path/to/your-project
gitreins install
```

This creates:
- `.gitreins/config.yaml` — default configuration with guards enabled
- `.gitreins/tasks.yaml` — empty task store (created on first task operation)
- `.git/hooks/pre-commit` — executable hook that runs `gitreins guard`
- `.gitignore` entry for `.gitreins/tasks.yaml` (if `.gitignore` exists)

### 7.5 Verify Installation

```bash
# Check guards run correctly
gitreins guard

# Expected output (example):
# ✓ secrets  — passed
# ✓ lint     — passed (2 warnings)
# ✓ tests    — passed (322 tests)
# Tier 1: PASSED
```

---

## 8. Per-Project Setup

Each repository that uses GitReins requires local configuration files. These are created by `gitreins install` and can be customized per project.

### 8.1 `.gitreins/config.yaml`

The repository-specific configuration overlay. Created by `gitreins install` with sensible defaults.

```yaml
guards:
  test_mode: full          # "full" or "diff"
  secrets: true
  lint: true
  tests: true
  test_command: pytest -x --tb=short

pipeline:
  stages:
  - id: tier1
    parallel: true
    true:
    - pre-commit
    - pre-eval
    steps:
    - id: secrets
      type: script
      run: gitleaks detect --source . --no-git 2>/dev/null || python3 -c "from engine.guard_manager import GuardManager; gm = GuardManager('.'); r = gm._check_secrets(); import sys; sys.exit(0 if r.passed else 1)"
      on_fail: continue
    - id: lint
      type: script
      run: ruff check . --quiet 2>/dev/null || python3 -c "from engine.guard_manager import GuardManager; gm = GuardManager('.'); r = gm._check_lint(); import sys; sys.exit(0 if r.passed else 1)"
      on_fail: continue
    - id: tests
      type: script
      run: pytest -x --tb=short 2>/dev/null || true
      on_fail: continue
  - id: tier2
    type: ai_eval
    true:
    - pre-eval
    condition: 'true'
    max_iterations: 16
    tools:
    - read_file
    - run_command
    - search_pattern
    - read_diff
    - sandbox

evaluator:
  max_iterations: 40
  max_time: 10m
  max_output_tokens: 200k
  tool_call_weight: 0.1
  model: deepseek/deepseek-v4-pro

defaults:
  model: deepseek-v4-flash
  max_iterations: 100
  check_for_updates: true
  update_check_ttl: 24h

history:
  enabled: true
  storage: git
  max_verdicts: 1000
```

**Key sections:**
- `guards` — Enable/disable individual guards, set test mode and command
- `pipeline` — Define evaluation stages (Tier 1 static, Tier 2 agentic)
- `evaluator` — Cap settings for the agentic evaluator
- `defaults` — Default model and behavior settings
- `history` — Verdict persistence configuration

### 8.2 `.git/hooks/pre-commit`

The git pre-commit hook is installed by `gitreins install`. It runs `gitreins guard` before every commit and blocks the commit if any guard fails.

```bash
#!/bin/bash
# GitReins pre-commit hook
# Installed by: gitreins install
# Version: 0.6.0

set -e

gitreins guard
```

**Behavior:**
- If all guards pass: hook exits 0, commit proceeds
- If any guard fails: hook exits 1, commit is blocked, failure summary is printed

To temporarily bypass the hook (not recommended for routine use):
```bash
git commit --no-verify -m "emergency fix"
```

### 8.3 `AGENTS.md` Harness Block

Projects using GitReins with AI agents should include an `AGENTS.md` file in the repository root. This file documents the quality harness rules for the agent.

**Required sections:**

```markdown
# GitReins Agent Rules

## GitReins Quality Harness (MANDATORY)

This repo uses GitReins as its quality gate. Every commit runs static guards.
If guards fail, the commit is BLOCKED. You cannot skip this.

### Quick check before committing:

```bash
PATH="$HOME/go/bin:$HOME/.venv/bin:$PATH" gitreins guard
```

### What's checked:
- **secrets** — API keys, tokens, passwords (BLOCKS on fail — no exceptions)
- **lint** — ruff (WARNS on fail)
- **tests** — pytest for changed packages (BLOCKS on fail)

### Test mode: full
Only packages with staged changes are tested. Pre-existing failures in
untouched code will NOT block your commit. If you change pyproject.toml,
Makefile, .gitreins/config.yaml, or a config file, the full suite runs
as a safety net.

### Tasks and evaluation:

```bash
# Create a task with criteria
gitreins task create fix-auth "Fix authentication" \
  "Login accepts email+password and returns JWT" \
  "Invalid credentials return 401" \
  "Rate limiting works after 5 failed attempts"

# Do the work, then evaluate:
gitreins task start fix-auth
# ... implement ...
gitreins task complete fix-auth    # triggers LLM evaluation

# Or evaluate standalone:
gitreins judge fix-auth
```

### If guards fail:
1. READ the output — the guard tells you exactly what failed and where
2. Fix the issues. Do NOT commit with `--no-verify` unless it's a docs-only
   change or a GitReins self-upgrade.
3. Re-run `gitreins guard` until it passes
4. Then commit

### Never:
- Commit API keys or tokens — secrets guard catches these, and it's correct
- Skip guards with `--no-verify` for code changes
- Push if guards failed (let CI catch it if you must, but fix locally)
- Commit `.gitreins/tasks.yaml` — it's local task state
```

**Purpose:** The `AGENTS.md` file ensures that every AI agent working on the repository understands the quality gates and follows the correct workflow. It is not parsed by GitReins — it is documentation for the agent.

---

## Appendix A: File References

| File | Purpose | Lines |
|------|---------|-------|
| `pyproject.toml` | Package manifest, version, dependencies | 51 |
| `engine/version.py` | Runtime version string | 1 |
| `.github/workflows/ci.yml` | GitHub Actions CI definition | 26 |
| `CONTRIBUTING.md` | Contributor guide with release process | 69 |
| `.gitreins/config.yaml` | Per-repo configuration (created by install) | ~57 |
| `AGENTS.md` | Agent-facing quality harness documentation | 54 |

---

*End of 10-Deployment.md*
