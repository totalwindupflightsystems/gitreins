# 08-Test-Strategy.md — Test Strategy

> **Document Status:** Draft | **Last Updated:** 2026-06-20 | **Author:** GitReins Test Strategy Spec

---

## 1. Overview

GitReins maintains a comprehensive test suite that validates every layer of the system: unit tests for individual engine modules, integration tests for CLI and MCP interactions, and real-LLM integration tests for cap enforcement. The test suite runs in under a minute for the fast path and provides confidence that changes do not break existing behavior.

**Current state:** 411 tests across 26 test files (including 4 real-LLM integration tests). Core engine tests complete in ~58 seconds.

---

## 2. Test Suite Overview

### 2.1 Test Count

| Category | Count | Files |
|----------|-------|-------|
| Unit tests (engine) | ~270 | 7 files |
| Integration tests (CLI) | 53 | `tests/test_cli.py` |
| Integration tests (MCP) | 5 | `tests/test_mcp_integration.py` |
| MCP server tests | ~30 | `tests/test_mcp_server.py` |
| v0.7 feature tests | ~18 | `tests/test_v07_features.py` |
| Eval cap tests (fast) | 38 | `tests/test_eval_cap.py` (non-LLM) |
| Eval cap tests (LLM) | 4 | `tests/test_eval_cap.py` (real LLM) |
| **Total** | **411** | **11 files** |

### 2.2 Core Engine Test Timing

The 270 core engine tests (evaluator, judge, LLM, task manager, guard manager, pipeline, eval cap) complete in approximately 58 seconds on a standard Linux development machine.

```
pytest tests/test_evaluator.py tests/test_judge.py tests/test_llm.py \
  tests/test_task_manager.py tests/test_guard_manager.py \
  tests/test_pipeline.py tests/test_eval_cap.py
# 270 passed in 58.45s
```

---

## 3. Test Categories

### 3.1 Unit Tests

Unit tests isolate individual modules and verify their public APIs and edge cases. Each engine module has a dedicated test file.

| Test File | Module Under Test | Tests | Coverage |
|-----------|-------------------|-------|----------|
| `tests/test_task_manager.py` | `engine/task_manager.py` | ~32 | 93% |
| `tests/test_llm.py` | `engine/llm.py` | ~45 | 91% |
| `tests/test_guard_manager.py` | `engine/guard_manager.py` | ~40 | 54% |
| `tests/test_evaluator.py` | `engine/evaluator.py` | ~56 | 85% |
| `tests/test_eval_cap.py` | `engine/eval_cap.py` | 42 | 92% |
| `tests/test_judge.py` | `engine/judge.py` | 19 | 95% |
| `tests/test_pipeline.py` | `engine/pipeline.py` | ~40 | 84% |

#### test_task_manager.py (~32 tests)

Tests CRUD operations on `.gitreins/tasks.yaml`:
- Task creation with criteria and dependencies
- Task start (status transition `pending` → `in_progress`)
- Task completion with dependency validation
- Task deletion
- Dependency blocking (`DependencyError` when prerequisites incomplete)
- YAML persistence round-trips

#### test_llm.py (~45 tests)

Tests the multi-provider LLM client:
- OpenAI-compatible API calls
- Anthropic native API calls
- Tool call parsing and execution
- Retry logic on transient failures
- Token usage tracking
- Response formatting

#### test_guard_manager.py (~40 tests)

Tests Tier 1 static checks:
- Secrets pattern detection
- Lint runner invocation (ruff)
- Test runner invocation (pytest full and diff modes)
- Diff-mode test file mapping (`engine/foo.py` → `tests/test_foo.py`)
- Force-full triggers (config changes, `pyproject.toml`)
- Go project auto-detection and guard execution
- Guard result formatting and summary generation

#### test_evaluator.py (~56 tests)

Tests the agentic LLM evaluation loop:
- Tool execution (read_file, run_command, search_pattern, read_diff)
- Sandbox read/write
- Verdict JSON parsing (valid and invalid)
- Cap checking before each LLM call
- Iteration credit tracking
- Forced `INCOMPLETE` on cap exhaustion
- Error handling (missing files, bad commands)

#### test_eval_cap.py (42 tests)

Tests resource cap parsing and enforcement:
- Legacy string parsing (`"100/30m/200k/50k"`)
- Individual cap parsing (iterations, time, tokens)
- Unlimited mode (`-1`, `"unlimited"`, `"none"`)
- Tool call weighting (fractional cost)
- Config-driven cap loading from `.gitreins/config.yaml`
- Pipeline cap regression (ensuring Pipeline defaults to `-1` for `max_iterations`)
- **Real LLM tests:** iteration cap stop, time cap stop, unlimited complete, tool discount verification

#### test_judge.py (19 tests)

Tests the Tier 1 + Tier 2 orchestrator:
- Guard-first evaluation (Tier 1 fails → no Tier 2)
- Criteria-only evaluation (no guards → direct Tier 2)
- Verdict result formatting
- Pipeline integration
- Error handling for missing tasks

#### test_pipeline.py (~40 tests)

Tests the configurable pipeline engine:
- Stage execution (sequential and parallel)
- Step result piping between stages
- Conditional execution (`stage.tier1.any_failed`)
- AI evaluation stage integration
- Default pipeline configuration loading
- Error handling for malformed pipeline configs

### 3.2 Integration Tests

#### MCP Integration Tests (`tests/test_mcp_integration.py`, 5 tests, 279 lines)

Tests the MCP stdio server with real JSON-RPC 2.0 communication:
- Tool list returns all 9 exposed tools
- Task roundtrip: create → start → complete
- Cross-repo task management via `workdir` parameter
- Server startup and shutdown
- Error responses for invalid tool calls

These tests spawn the MCP server as a subprocess and communicate over stdin/stdout, validating the full stdio transport layer.

#### CLI Integration Tests (`tests/test_cli.py`, 53 tests)

Tests CLI commands via subprocess invocation:
- `install` command creates config, hook, and gitignore entry
- `task create/start/complete/list/delete` commands
- `guard` command runs and reports results
- `judge` command evaluates tasks
- `commit` command with guard gate
- Error handling for missing tasks, non-git directories
- Output formatting and exit codes

### 3.3 LLM Integration Tests

Four tests in `tests/test_eval_cap.py` run the full evaluator loop against a real LLM provider. These require an API key (`GITREINS_LLM_API_KEY` or `DEEPSEEK_API_KEY`) and are skipped when not configured.

| Test | Purpose | Cap |
|------|---------|-----|
| `test_iteration_cap_stops_evaluator` | Verifies evaluator stops after cap iterations | `max_iterations=2` |
| `test_time_cap_stops_evaluator` | Verifies evaluator stops on time limit | `max_time="5s"` |
| `test_unlimited_completes_normally` | Verifies `-1` (unlimited) allows completion | `max_iterations=-1` |
| `test_tool_calls_discounted` | Verifies tool_call_weight reduces iteration cost | `max_iterations=3, tool_call_weight=0.1` |

These tests create temporary git repositories, write minimal config, and run the evaluator against real LLM calls. Each test takes 10-30 seconds depending on LLM latency.

---

## 4. Coverage Targets

### 4.1 Engine Core Coverage (Verified)

| Module | Statements | Missed | Coverage | Target |
|--------|-----------|--------|----------|--------|
| `engine/evaluator.py` | 300 | 45 | **85%** | >85% ✅ |
| `engine/judge.py` | 79 | 4 | **95%** | >85% ✅ |
| `engine/llm.py` | 165 | 15 | **91%** | >85% ✅ |
| `engine/task_manager.py` | 100 | 7 | **93%** | >85% ✅ |
| `engine/pipeline.py` | 218 | 34 | **84%** | >85% ⚠️ |
| `engine/guard_manager.py` | 283 | 130 | **54%** | >85% ❌ |
| `engine/eval_cap.py` | 249 | 19 | **92%** | >85% ✅ |

### 4.2 Coverage Gaps

| Module | Gap | Reason |
|--------|-----|--------|
| `engine/guard_manager.py` | 46% missed | Go-specific guards, dead code paths, error handling for missing tools |
| `engine/pipeline.py` | 16% missed | Conditional stage execution, error recovery paths |
| `engine/config.py` | 70% missed | Update checker (network-dependent), type coercion error paths |
| `engine/persist.py` | 0% | No dedicated test file; tested indirectly via CLI integration |
| `engine/dead_code.py` | 0% | No dedicated test file; opt-in feature |

### 4.3 Target Policy

- **Engine core modules:** >85% coverage (evaluator, judge, llm, task_manager, pipeline, eval_cap)
- **Guard manager:** Target >70% (currently 54%, needs Go guard tests)
- **Config:** Target >50% (currently 30%, needs update checker mocking)
- **Integration surface:** CLI and MCP integration tests cover user-facing behavior regardless of line coverage

---

## 5. Test Modes

### 5.1 Fast Mode (Pre-Commit)

```bash
pytest -m "not llm"
```

Runs all tests except real-LLM integration tests. ~407 tests, ~60 seconds. Used for pre-commit validation and rapid feedback during development.

### 5.2 Full Mode (CI)

```bash
pytest
```

Runs the complete suite including LLM integration tests if API keys are available. ~411 tests. Used in CI pipelines for comprehensive validation.

### 5.3 LLM-Only Mode (Cap Validation)

```bash
pytest tests/test_eval_cap.py::TestEvalCapRealEvaluator
```

Runs only the 4 real-LLM cap tests. Used when validating cap behavior against actual LLM providers. Requires `GITREINS_LLM_API_KEY` or `DEEPSEEK_API_KEY` environment variable.

### 5.4 Module-Specific Mode

```bash
pytest tests/test_evaluator.py          # Evaluator only
pytest tests/test_guard_manager.py -v   # Guards with verbose output
pytest tests/test_eval_cap.py -k "parser"  # Parser tests only
```

---

## 6. Mocking Strategy

### 6.1 HTTP Mocking

The LLM client tests use `requests-mock` to intercept HTTP calls to OpenAI and Anthropic endpoints. This allows testing:
- API request formatting
- Response parsing
- Retry logic
- Error handling (timeouts, 5xx, 4xx)

Without requiring real API keys or network access.

### 6.2 Subprocess Mocking

CLI and MCP integration tests use actual subprocess calls to validate:
- Command-line argument parsing
- Exit codes
- Stdout/stderr formatting
- File system side effects (config creation, hook installation)

These are "integration" tests by nature — they exercise the real CLI entry point.

### 6.3 Real LLM for Cap Tests

The 4 eval cap integration tests use a real LLM because:
- Cap stopping behavior depends on actual LLM response timing
- Tool call discounting requires real tool execution
- Mocking would not validate the actual iteration credit accounting

These tests use tight caps (2 iterations, 5 seconds) to minimize cost and latency.

---

## 7. Test Data

### 7.1 Demo Projects

Two demo projects serve as evaluator test fixtures:

| Directory | Purpose | Content |
|-----------|---------|---------|
| `demo-calc/` | Calculator demo | Simple Python calculator with tests |
| `demo-slugify/` | Slugify demo | String slugification with tests |

These are used by the evaluator integration tests to provide realistic codebases for the LLM to inspect.

### 7.2 Reliability Test Fixtures

The `tests/reliability/` directory contains subdirectories with intentionally buggy code for guard detection validation:

| Fixture | Bug Type | Guard Target |
|---------|----------|--------------|
| `dead-tests/` | Dead test code | Dead code detection |
| `hallucinated-import/` | Non-existent imports | Lint / import checks |
| `missing-error-handling/` | Missing try/except | Error handling guards |
| `secret-leak/` | Hardcoded API keys | Secrets scanner |
| `silent-exception/` | Swallowed exceptions | Error handling guards |
| `silent-logic-bug/` | Subtle logic errors | Test coverage |
| `wrong-control-flow/` | Incorrect branching | Test coverage |

---

## 8. CI Integration

### 8.1 GitHub Actions

The project uses GitHub Actions for continuous integration. The workflow runs:

1. **Fast suite on every push** — `pytest -m "not llm"` for rapid feedback
2. **Full suite on PR** — `pytest` with LLM tests if secrets are available
3. **Coverage reporting** — `pytest --cov=engine --cov-report=term-missing`

### 8.2 CI Configuration

```yaml
# .github/workflows/ci.yml (conceptual)
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.11" }
      - run: pip install -e ".[dev]"
      - run: pytest -m "not llm" --cov=engine --cov-report=xml
      - run: pytest  # full suite, LLM tests skipped if no API key
```

### 8.3 Pre-Commit Hook

The GitReins pre-commit hook runs `gitreins guard`, which executes the fast test suite for changed packages. This ensures that commits do not break tests in the modules they touch.

---

## 9. Test Growth

The test suite has grown steadily as features were added:

| Milestone | Tests | Date | Notes |
|-----------|-------|------|-------|
| Initial | 221 | v0.3.x | Core engine + basic CLI |
| v0.5 expansion | 322 | v0.5.x | MCP integration, pipeline, eval cap |
| v0.6 current | 411 | v0.6.0 | v0.7 features, extended CLI tests |

Test count is tracked per-module in the acceptance criteria (AC-019). The goal is to maintain or increase coverage with each feature addition.

---

## 10. Verification Checklist

| # | Check | Verification |
|---|-------|------------|
| 1 | Fast suite passes | `pytest -m "not llm"` → all green |
| 2 | Core engine coverage >85% | `pytest --cov=engine` → evaluator 85%, judge 95%, llm 91%, task_manager 93% |
| 3 | LLM cap tests validate real stopping | `pytest tests/test_eval_cap.py::TestEvalCapRealEvaluator` → 4/4 pass |
| 4 | CLI integration tests pass | `pytest tests/test_cli.py` → 53/53 pass |
| 5 | MCP integration tests pass | `pytest tests/test_mcp_integration.py` → 5/5 pass |
| 6 | Diff-mode test mapping correct | `pytest tests/test_guard_manager.py -k diff` → pass |
| 7 | Pipeline cap regression tests pass | `pytest tests/test_eval_cap.py::TestPipelineCapRegression` → 4/4 pass |
| 8 | No tests fail on clean checkout | `pytest` → all pass (LLM tests skipped if no key) |

---

## 11. Document Status

| Field | Value |
|-------|-------|
| **Version** | v0.6.0 |
| **Status** | Draft |
| **Last updated** | 2026-06-20 |
| **Author** | totalwindupflightsystems <totalwindupflightsystems@gmail.com> |
| **Co-author** | wojons <wojonstech@gmail.com> |
