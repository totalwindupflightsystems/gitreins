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
- **Result:** 38 tests pass.

## [x] GR-029: Commit pending AGENTS.md + AC changes
- **Priority:** low

## [x] GR-030: Fix hanging test_judge_existing_task_exits_0
- **Priority:** high

## [x] GR-031: Speed up test_run_command_timeout
- **Priority:** low

## [x] GR-032: Fix gitleaks UTF-8 decode error
- **Priority:** medium
- **Commit:** `9d11da7`

## [x] GR-033: Add .gitleaksignore to suppress false positives
- **Priority:** medium
- **Commit:** `a9c1e46`

## [x] GR-034: Fix hanging test_judge_requires_api_key
- **Priority:** high
- **Commit:** `d335582`

## [x] GR-035: Clean working tree — remove untracked cruft
- **Priority:** low
- **Commit:** `0deb91a`
- **Result:** Added *.db to .gitignore, removed .acl_test. .coverage/.skylos/*.tmp already gitignored.

## [x] GR-036: Add uv.lock to repo for deterministic builds
- **Priority:** low
- **Commit:** `0deb91a`
- **Result:** uv.lock (1101 lines) committed.
