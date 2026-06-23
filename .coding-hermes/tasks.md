# GitReins Improvement Tasks

## [x] GR-020: Add `gitreins install` command
- **Priority:** high
- **Commit:** `015a0c1`

## [x] GR-021: Fix YAML `on:` key parsing bug
- **Priority:** medium
- **Commit:** `ae1d308`

## [x] GR-022: Go project support
- **Priority:** medium

## [x] GR-023: Update gitreins-workflow skill
- **Priority:** medium

## [x] GR-024: Pre-commit hook for Go repos
- **Priority:** low

## [x] GR-025: ASCE integration test
- **Priority:** low

## [x] GR-026: Skip Python guards on Go projects
- **Priority:** medium

## [x] GR-027: Mock LLM calls in hanging CLI judge test
- **Priority:** high

## [x] GR-028: Unit tests for dead_code detector
- **Priority:** medium

## [x] GR-029: Commit pending AGENTS.md + AC changes
- **Priority:** low

## [x] GR-030: Fix hanging test_judge_existing_task_exits_0
- **Priority:** high

## [x] GR-031: Speed up test_run_command_timeout
- **Priority:** low

## [x] GR-032: Fix gitleaks UTF-8 decode error
- **Priority:** medium

## [x] GR-033: Add .gitleaksignore to suppress false positives
- **Priority:** medium

## [x] GR-034: Fix hanging test_judge_requires_api_key
- **Priority:** high

## [x] GR-035: Clean working tree — remove untracked cruft
- **Priority:** low

## [x] GR-036: Add uv.lock to repo for deterministic builds
- **Priority:** low

## [x] GR-037: Fix hanging CLI and MCP integration tests
- **Priority:** high

## [x] GR-038: Fix F841 unused variable in dead_code.py
- **Priority:** low

## [x] GR-039: Fix F401 unused import in engine/__init__.py
- **Priority:** low

## [x] GR-040: Fix gitleaks false positive on synthetic AWS test key
- **Priority:** high

## [x] GR-041: Fix 218 ruff lint errors
- **Priority:** medium

## [x] GR-042: Fix guard `--source` absolute path bug
- **Priority:** medium

## [x] GR-043: Add `.gitreins/history/` to `.gitignore`
- **Priority:** low

## [x] GR-044: Fix TestEvalCapRealEvaluator — skip on invalid API key
- **Priority:** medium

## [x] GR-045: Commit pending import cleanup and evaluator.py param fix
- **Priority:** low

## [x] GR-046: Suppress synthetic key false positives in .gitleaksignore
- **Priority:** low

## [x] GR-047: Add MIT LICENSE file
- **Priority:** high

## [x] GR-048: Remove stale xfail marker from test_hook_blocks_commit_with_secret
- **Priority:** low
- **Commit:** `f8745e7`
- **Result:** 514 passed, 6 skipped, 0 xpassed.

## [x] GR-049: Track bin/, LSP test fixtures, update .gitignore
- **Priority:** low
- **Commit:** `fb8de2c`
- **Result:** 21 files tracked (bin/, tests/fixtures/lsp/). Cross-project artifacts gitignored. Demo projects remain untracked (nested .git repos prevent tracking in cron mode).

## [x] GR-050: Commit LSP guard core runner
- **Priority:** high
- **Commit:** `e037edc`
- **Result:** engine/lsp.py (337 lines), tests/test_lsp.py (208 lines, 22 tests), guard_manager.py _check_lsp(), config. AC-080 verified. 537 passed.

## [x] GR-051: LSP guard Tier 1 — enable and test with real LSP servers
- **Priority:** high
- **Commit:** `fa921c9`
- **Result:** Rewritten _lsp_read_response (os.read bypasses BufferedReader), 29 LSP tests (22 unit + 7 integration with live pylsp), pyflakes+pycodestyle installed. Full suite: 544 passed, 5 skipped.
- **Model:** deepseek-v4-flash
- **Provider:** deepseek
- **Files:** `engine/lsp.py`, `tests/test_lsp.py`, `.gitreins/config.yaml`
- **AC:**
  - Start pylsp server (pip install python-lsp-server) and run guard with lsp:true
  - Guard detects real diagnostics from staged Python files
  - Test with a known-bad file (undefined variable, syntax error) — guard FAILS
  - Test with clean files — guard PASSES
  - Non-zero exit on lsp error
  - Handle missing LSP server gracefully (skip, not crash)

## [x] GR-052: LSP guard Tier 2 evaluator integration
- **Priority:** medium
- **Commit:** `73a38fb`
- **Result:** Judge extracts LSP diagnostics from Tier 1 GuardResult, passes to evaluator. Evaluator injects diagnostics into prompt + provides read_lsp_diagnostics tool. 556 passed, 6 skipped.
- **Model:** deepseek-v4-flash
- **Provider:** deepseek
- **Files:** `engine/evaluator.py`, `engine/guard_manager.py`, `gitreins/cli.py`
- **AC:**
  - `gitreins judge <task>` invokes LSP guard as part of Tier 1
  - LSP diagnostics feed into Tier 2 evaluation context
  - Evaluator can suggest fixes based on LSP diagnostics
  - Test: create task with a lint error, verify evaluator catches it

## [x] GR-053: LSP multi-language support — Round 1
- **Priority:** low
- **Model:** deepseek-v4-flash
- **Provider:** deepseek
- **Files:** `engine/lsp.py`, `tests/test_lsp.py`, `.gitreins/config.yaml`
- **AC:**
  - Support at least 2 more languages beyond Python (e.g., rust-analyzer for Rust, typescript-language-server for TS)
  - Auto-detect language from file extension
  - Config: `lsp_tools: [pylsp, rust-analyzer, typescript-language-server]`
  - Tests for each language's tool discovery
  - Graceful skip if LSP server not installed
- **Result:** 570 passed, 7 skipped (+14 new tests). rust-analyzer skip for type-error test when no Cargo.toml (expected). ts-lsp skip graceful.
- **Commit:** `97ae130`

## [x] GR-054: Increase guard test timeout from 120s to 180s
- **Priority:** medium
- **Model:** deepseek-v4-flash
- **Provider:** deepseek
- **Files:** `.gitreins/config.yaml`, `engine/guard_manager.py`
- **AC:**
  - Full test suite (537 tests) completes within timeout
  - No more --no-verify commits needed for timeout margin
  - Configurable via `test_timeout` in guard config
  - Default: 180s
- **Result:** Changed `test_timeout: 120` → `180` in config.yaml. Added `self._test_timeout = guards_cfg.get("test_timeout", 180)` in GuardManager.__init__. Updated `_run_test_command` to use `self._test_timeout` (subprocess.run timeout + dynamic error message). 570 passed, 7 skipped. Guard PASS.

## [x] GR-055: Suppress MCP debug spam from mcp package
- **Priority:** low
- **Commit:** `d99f8ad`
- **Model:** deepseek-v4-pro (direct)
- **Files:** `gitreins_mcp/server.py`
- **AC:**
  - mcp package debug-level logging is suppressed at import time
  - No functional impact — all 718 tests pass
  - `gitreins guard` still works
- **Result:** Added `logging.basicConfig(level=WARNING, stream=sys.stderr, force=True)` before mcp imports. 718 passed, 3 skipped.

## [x] GR-056: Add MCP guard tool — dead_code boolean
- **Priority:** medium
- **Model:** deepseek-v4-pro (direct — 2-file mechanical wiring)
- **Provider:** deepseek
- **Files:** `gitreins_mcp/server.py`, `engine/guard_manager.py`, `gitreins/cli.py`
- **AC:**
  - `gitreins guard --dead-code` runs dead code detection
  - MCP tool `guard_run` supports `dead_code: true` parameter
  - Test verifies dead code tool reports unused functions
- **Result:** 718 passed, 7 skipped.

## [x] GR-057: Add MCP propagate tool for multi-repo quality
- **Priority:** low
- **Model:** deepseek-v4-flash
- **Provider:** deepseek
- **Files:** `gitreins_mcp/server.py`, new `engine/propagate.py`
- **AC:**
  - `mcp_gitreins_propagate` tool copies guard config to sibling repos
  - Works with gitreins-poc and downstream repos
  - Test verifies config propagation preserves overrides
- **Result:** engine/propagate.py (180 lines) — Propagator class with recursive dict merge. MCP server integrated with _propagate handler. 7 tests (create, merge, multi-target, error cases, MCP JSON-RPC). 725 passed, 7 skipped.

## [x] GR-058: Type-safe GuardResult dataclass
- **Priority:** medium
- **Model:** deepseek-v4-flash
- **Provider:** deepseek
- **Files:** `engine/guard_manager.py`, `engine/types.py` (new)
- **AC:**
  - `GuardResult` is a frozen dataclass with typed fields
  - All callers use field access instead of dict access
  - Tests verify immutability and type correctness
- **Result:** GuardResult + Tier1Result moved to engine/types.py with frozen=True. GuardResult(name, passed, output, error) + Tier1Result(passed, results, extra) both frozen. Immutability tests verify FrozenInstanceError on mutation. 684 passed, 2 skipped. Backward compat: guard_manager.py re-exports via `from engine.types import ...`.
