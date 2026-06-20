# GitReins Improvement Tasks

## [x] GR-020: Add `gitreins install` command
- **Priority:** high
- **Commit:** `015a0c1`
- **AC:** `gitreins install` creates .gitreins/config.yaml and git pre-commit hook

## [x] GR-021: Fix YAML `on:` key parsing bug
- **Priority:** medium
- **Commit:** `ae1d308`
- **AC:** `"on":` (quoted) in config.yaml preserves correct trigger list instead of parsing as boolean

## [x] GR-022: Go project support
- **Priority:** medium
- **Files:** engine/guard_manager.py, engine/guards.py
- **AC:** Guards detect Go projects (go.mod present) and run `go vet`/`go test`/`go build` instead of pytest/ruff

## [x] GR-023: Update gitreins-workflow skill
- **Priority:** medium
- **Status:** Skill already at v1.4.0 — documents `gitreins install`, Go project pattern, config loading fix, pitfalls. No update needed.

## [x] GR-024: Pre-commit hook for Go repos
- **Priority:** low
- **Status:** Existing `gitreins install` pre-commit hook calls `gitreins guard` which now detects Go projects and runs the correct guards. No separate hook needed.

## [x] GR-025: ASCE integration test
- **Priority:** low
- **Result:** All 4 Go guards pass on ASCE with real code (build, vet, test, secrets). No Python guards run (correctly skipped). No false positives.

## [x] GR-026: Skip Python guards on Go projects (discovered during GR-025)
- **Priority:** medium
- **Files:** engine/guard_manager.py
- **Fix:** `run_all()` now skips Python-specific guards (lint, tests, dead_code) when `go.mod` detected
- **AC:** Go projects don't run pytest/ruff/dead_code — only Go-native guards execute

## [x] GR-027: Mock LLM calls in hanging CLI judge test
- **Priority:** high
- **Files:** tests/test_cli.py
- **Commit:** `2138689`
- **AC:** `test_judge_existing_task_exits_0` no longer makes real LLM calls — mock `engine.llm.LLMClient.chat` so it returns instantly with fake verdict JSON

## [x] GR-028: Unit tests for dead_code detector
- **Priority:** medium
- **Files:** tests/test_dead_code.py (new), engine/dead_code.py
- **Model:** MiniMax-M3 (minimax)
- **AC:** Tests cover unreachable code, unused functions, unused imports, empty functions; all pass
- **Result:** 38 tests pass (0.03s). Covers all 4 categories + edge cases.

## [ ] GR-029: Commit pending AGENTS.md + AC changes
- **Priority:** low
- **Files:** AGENTS.md, .hermes/acceptance-criteria.md
- **Model:** direct (no spawn)
- **AC:** Pending diffs committed with descriptive message + Co-authored-by trailer
