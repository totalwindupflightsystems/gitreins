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
- **Commit:** `9d11da7`
- **Result:** Added `errors='replace'` to `subprocess.run()` in `_check_secrets()`. Gitleaks now runs without decode crash. Found real secret: `.env` (git-ignored, untracked — false positive for guard). Also found doc false positive in `.memory-bank/`.
