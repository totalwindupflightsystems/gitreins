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
