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

## [x] GR-059: Catch-up — bump uv.lock to 0.7.8
- **Priority:** low
- **Commit:** `23baf7a`
- **Result:** uv.lock version synced with pyproject.toml/engine/version.py. Pushed to GitHub. 699 passed, 6 skipped (1 flaky LSP integration test).

## [x] GR-065: CodeRabbit-style commit review agent (mini-harness for Tier 2)
- **Priority:** high
- **Model:** deepseek-v4-flash (coding-hermes cron)
- **Files:** `engine/commit_audit.py`, `engine/config.py`, `engine/pipeline.py`, `gitreins/cli.py`, `tests/test_commit_audit.py`
- **Concept:** Expand Tier 2 commit audit into a mini LLM agent that does CodeRabbit-level commit review — not just "is the message accurate?" but "is this code good?"

### New config surface (`commit_audit` section):

```yaml
commit_audit:
  enabled: true
  mode: warn | block | suggest          # unchanged — controls action on failure

  # ── NEW: review engine ──
  review_mode: message | review | agent  # message=current, review=CodeRabbit single-call, agent=multi-turn with tools
  review_checks:                         # which checks to run
    bugs: true
    security: true
    style: false                         # off by default — linter covers this
    performance: false
    anti_patterns: true
  review_severity: standard              # critical-only | standard | all
  review_suggest_fix: true               # include fix suggestions for issues found
  review_max_tokens: 2048                # cap for review response
```

### Behavior per review_mode:

| Mode | What it does | Iterations | Time |
|------|-------------|:---:|:---:|
| `message` | Current Tier 2 — validate commit message vs diff | 1 call | <2s |
| `review` | CodeRabbit-style — scan diff for bugs, security, anti-patterns, suggest fixes, validate message. Like a senior dev doing a quick review. | 1 call | <10s |
| `agent` | Full mini-harness — multi-turn LLM with read_file/search_pattern tools. Explores surrounding code for deeper analysis. | up to `max_iterations` | Configurable |

### Tasks:
- [x] GR-065a: Review system prompt — write a `COMMIT_REVIEW_SYSTEM_PROMPT` that instructs the LLM to act like a senior code reviewer. Covers all review_checks categories with examples.
- [x] GR-065b: `review_mode` implementation — wire `review` mode as a single-call path alongside existing `message` mode. Same `CommitAuditor`, new code path.
- [x] GR-065c: `agent` mode — reuse existing `_tool_loop()` from commit audit. Let the LLM explore files before rendering verdict.
- [x] GR-065d: Review result structure — `CommitReviewResult` with `{issues: [{file, line, severity, category, message, suggestion}], summary, message_valid, message_issues}`
- [x] GR-065e: Config wiring — all new keys in `GitReinsDefaults`, `overlay()`, `to_config_dict()`, pipeline step reading
- [x] GR-065f: CLI output — `gitreins commit-audit` shows review findings with file:line references, severity markers, and fix suggestions
- [x] GR-065g: Tests — mock LLM review responses, verify structured parsing, test config defaults, test all severity levels

## [x] GR-066: CVE-style scored severity system for commit review
- **Priority:** high
- **Commit:** `86b14fa`
- **Result:** 504 insertions, 10 deletions across 4 files. 81 commit_audit tests pass (41 existing + 40 new covering dataclass, config, routing, output).

## [x] GR-067: Anthropic Messages API endpoint support
- **Priority:** medium
- **Commit:** (pre-existing — fully implemented in `engine/llm.py`)
- **Model:** deepseek-v4-flash (coding-hermes cron)
- **Files:** `engine/llm.py` (not llm_client.py — board had wrong filename)
- **Result:** All 5 subtasks already implemented. `_chat_anthropic()` (line 283-354), response parsing with text/tool_use blocks, `_is_anthropic()` auto-detection (line 66-69), tool format conversion (line 410-420), message conversion (line 356-408). 45 tests pass. Provider routing via `_chat_attempt()` (line 184-187). Docs update: `anthropic` provider in `_PROVIDER_MAX_OUTPUT_TOKENS` (line 196). Board was stale — work done in prior tick but never marked [x].

## [x] GR-068: DeepSeek prompt caching + reasoning flag — cost optimization
- **Priority:** high
- **Model:** deepseek-v4-flash (coding-hermes cron)
- **Goal:** Use DeepSeek's automatic prompt caching to reduce evaluator costs by 50-90% on cache hits. Set reasoning/thinking flag appropriately for the task type.
- **Why:** DeepSeek caches repeated prompt prefixes automatically. Evaluator system prompt + code context is identical across multiple judge calls — huge cache-hit potential. Adam W. identified that setting the right flag by default unlocks this.
- **Impact:** Each evaluator run currently sends the full system prompt + code context (~50K-200K tokens). With caching, subsequent runs on the same codebase only pay for the diff.

### Tasks:
- [x] GR-068a: Research — confirm DeepSeek V4 caching behavior (auto-prefix vs explicit). Check if `enable_thinking`, `reasoning_effort`, or other flags affect cache eligibility.
- [x] GR-068b: Flag config — add `llm.reasoning` to LLMClient and `.gitreins/config.yaml`
- [x] GR-068c: Wire flags — pass reasoning parameters in `_chat_openai()` request body if provider is `deepseek`
- [x] GR-068d: Cache telemetry — log `cache_read_tokens` / `cache_write_tokens` from LLMResponse.usage in evaluator output. Show $ saved.
- [x] GR-068e: Tests — mock DeepSeek cache hit/miss responses, verify telemetry, verify flags in request body
- [x] GR-068f: Skill docs — document caching behavior, expected savings, and how to verify it's working (check `cache_read_tokens > 0` in judge output)

## [x] GR-064: Tier 2 large-repo hardening — dexdat-memory feedback
- **Priority:** high
- **Model:** deepseek-v4-flash (coding-hermes cron)
- **Source:** Real-world test on dexdat-memory (147 Go packages, 40+ tested)
- **Files:** `engine/evaluator.py`, `engine/pipeline.py`, `gitreins/cli.py`, `engine/config.py`

### What broke:
- Tier 2 LLM eval consistently timed out (15m/75 iterations never completed)
- Judge spun reading files, hit token limits despite 29M input budget
- Pre-commit hook hung on `git push` (process timeout)
- `file_scope: changed` wasn't aggressive enough — LLM still chased call graphs

### Tasks:
- [x] GR-064a: **Fast-track mode** — skip full call-graph analysis on large repos; verify only changed lines + immediate callers. Add `evaluator.fast_track` config (default: auto-detect based on package count)
- **Commit:** `24e9dc8`
- [x] GR-064b: **Aggressive timeout respect** — return partial findings when deadline hits instead of failing silently. Wire `max_time` into the tool-call loop so `read_file`/`search_pattern` check remaining budget before executing
- **Commit:** `731c0f0`
- [x] GR-064c: **`--skip-tier2` flag** — CLI flag for config/docs/ops commits that bypasses Tier 2 entirely. Also configurable per-commit via `gitreins.skip-tier2` trailer in commit message body
- **Commit:** `6e8d3e5`
- **Result:** 4 files (+165/-5): `engine/commit_audit.py` (+89: trailer parsing + has_skip_tier2_trailer), `engine/judge.py` (+51: skip_tier2 param + _run_legacy_skip_tier2), `engine/pipeline.py` (+9: "not task.skip_tier2" + "false" conditions), `gitreins/cli.py` (+21: --skip-tier2 flag on judge/commit, trailer wiring in commit-audit). 185 tests pass, guard PASS.
- [x] GR-064d: **Token budget overflow protection** — cap individual `read_file` results proportional to remaining budget. Don't let one 2MB file eat the entire context window. Add `max_file_bytes` config (default: 128KB per file in evaluator context)
  - **Commit:** `861ba52`
- [x] GR-064e: **Pre-commit hook timeout** — add configurable `hook_timeout` (default: 120s). If exceeded, fail open with warning (don't block the push indefinitely)
  - **Commit:** `4a4d14c`
  - **Result:** hook_timeout=120s default in GitReinsDefaults, time.monotonic() checks after each guard, fail-open returns Tier1Result(passed=True) with warnings list. warnings field added to Tier1Result dataclass, CLI output shows yellow warnings. All 50 guard_manager tests pass. Guard PASS (pre-existing E501 lint in overlay() noted — unrelated). 4 files (+97/-1).

## [ ] GR-063: Expand language coverage across all tool subsystems
- **Priority:** high
- **Model:** deepseek-v4-flash (coding-hermes cron)
- **Files:** `engine/pipeline.py`, `engine/lsp.py`, `engine/static_analysis.py`, `engine/config.py`, `tests/`
- **Status:** phased — cron picks up one sub-task per run
- **Current coverage gap:** Pipeline: 8 langs. LSP: 4. Static analysis: 4. Many top-20 languages missing.

### Phase 1 — C++ (most requested)
- [x] GR-063a: C++ LSP — add clangd to `_TOOL_BINARIES`, map `.cpp`/`.hpp`/`.cc`/`.cxx`/`.h` to `cpp` in `_LANGUAGE_MAP`
- [x] GR-063b: C++ static analysis — add `cppcheck` or `clang-tidy` to `_TOOL_BINARIES` + `_TOOL_INSTALL_GUIDE`
- [x] GR-063c: C++ pipeline — split "c" from "cpp" in `_LANG_COMMANDS`, add `CMakeLists.txt` → `cpp` detection
- **Commit:** `d3151d1`

### Phase 2 — Go (widely used, missing LSP+static)
- [x] GR-063d: Go LSP — add gopls to `_TOOL_BINARIES`, map `.go` → `go` in `_LANGUAGE_MAP`
- [x] GR-063e: Go static analysis — add staticcheck to `_TOOL_BINARIES` + `_TOOL_INSTALL_GUIDE`
- **Commit:** `4d5f01a`

### Phase 3 — Java/Kotlin
- [ ] GR-063f: Java LSP — add jdtls to `_TOOL_BINARIES`
- [ ] GR-063g: Kotlin LSP — add kotlin-language-server, map `.kt`/`.kts`
- [ ] GR-063h: Java/Kotlin pipeline — add kotlin to `_LANG_COMMANDS` (gradle), add `settings.gradle.kts` detection

### Phase 4 — C# / .NET
- [ ] GR-063i: C# LSP — add omnisharp-roslyn or csharp-ls
- [ ] GR-063j: C# pipeline — add to `_LANG_COMMANDS` (dotnet), detect `.csproj`/`.sln`

### Phase 5 — Swift, Dart, Elixir, Scala
- [ ] GR-063k: Swift LSP — sourcekit-lsp, map `.swift`
- [ ] GR-063l: Dart LSP — dart, map `.dart`, detect `pubspec.yaml`
- [ ] GR-063m: Elixir LSP — elixir-ls, map `.ex`/`.exs`, detect `mix.exs`
- [ ] GR-063n: Scala LSP — metals, map `.scala`/`.sc`, detect `build.sbt`

### Phase 6 — Rust, Python, JS/TS gap fill
- [ ] GR-063o: Rust static analysis — add clippy as static analysis (reuse from pipeline)
- [ ] GR-063p: JS/TS static analysis — add eslint as static analysis tool
- [ ] GR-063q: Ruby LSP — add solargraph or ruby-lsp to `_TOOL_BINARIES`

**Per-subtask pattern:**
1. Add binary detection + install guide
2. Wire into language map + tool-languages map
3. Add to default pipeline `_LANG_COMMANDS` if not present
4. Add signature file detection
5. 4+ tests per language (tool discovery, language mapping, skip-when-missing, integration)
6. Verify full suite still green

## [x] GR-062: Show git diff in commit audit output for alignment
- **Priority:** high
- **Model:** deepseek-v4-flash (coding-hermes)
- **Files:** `engine/config.py`, `engine/commit_audit.py`, `engine/pipeline.py`, `gitreins/cli.py`, `tests/test_commit_audit.py`
- **Result:** All 6 AC items already implemented. `CommitAuditResult.diff` field (commit_audit.py:161), pipeline diff output with 40-line truncation (pipeline.py:394-404), `commit_audit_show_diff` config key (config.py:61), CLI diff display (cli.py:1059-1070), 7 diff-specific tests in `TestCommitAuditDiffOutput`. 38/38 tests pass. Feature verified pre-existing — no code change needed.
- **Commit:** `4ae8189` (board verification)

## [x] GR-061: Catch-up — LSP process-group isolation + sk-api-key rule + init template sync
- **Priority:** medium
- **Commit:** `4d5f01a`
- **Result:** All AC verified. LSP process-group killpg with pid validation (reject non-int/bool/pid<=1, only killpg when pgid==pid≠our_pgid). _get_staged_files already handles fresh repos (HEAD check → ls-files fallback). sk-api-key rule uses 20+ (confirmed in .gitleaks.toml + init template). Tests updated.

## [x] GR-060: Investigate flaky LSP integration test
- **Priority:** low
- **Model:** deepseek-v4-pro (direct)
- **Files:** `engine/lsp.py`, `tests/test_lsp.py`
- **AC:**
  - `test_pylsp_detects_undefined_variable` passes reliably or is marked skip with clear reason
  - Root cause identified: pylsp not producing diagnostics, or test environment issue
  - If fixable: fix and verify 3 consecutive runs pass
  - If environment-dependent: add skip guard with explanatory message
- **Result:** Root cause: `_lsp_read_response`'s select() cap at 1.0s per call caused early bail when the global deadline was 10s. Server response occasionally took >1s (cold start, system load), hitting the cap and returning None prematurely. Fix: (1) Header and body read loops now retry on select timeout until global deadline is reached. (2) `_collect_diagnostics` uses absolute deadline instead of per-call reset, so each file gets at most `timeout_per_file` seconds total. (3) Break after receiving `publishDiagnostics` for the target file instead of waiting for full timeout. 10/10 consecutive runs pass, full suite 752 passed, 7 skipped.
