# Changelog

All notable changes to GitReins will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- CVE-style scored severity system for commit review (`review_score_threshold`, `review_score_offset`)
- Anthropic Messages API endpoint support (auto-detected provider routing)
- DeepSeek prompt caching telemetry (`cache_read_tokens` / `cache_write_tokens` in evaluator output)
- Large-repo hardening: fast-track mode, aggressive timeout respect, `--skip-tier2` flag, token budget overflow protection
- Expanded language coverage: C++, Go, Java, Kotlin, C#, Swift, Dart, Elixir, Scala LSP + static analysis + pipeline
- MCP `propagate` tool for multi-repo quality config distribution
- Type-safe `GuardResult` / `Tier1Result` frozen dataclass (engine/types.py)
- Dedicated test files for types, guards, propagate, persist, config (+109 tests)

### Changed
- `max_input_tokens: -1` treated as unlimited (use with care — can hang on large repos)
- Default `code_context_budget: 0.70`, `compaction_threshold: 0.90`

### Fixed
- Duplicate `_parse_staticcheck` function (shadowing bug from commit 4d5f01a)
- CVE-2026-59950: bumped mcp 1.28.0→1.28.1 (Cross-Site WebSocket Hijacking, CVSS 7.6)
- LSP integration tests in CI: pyflakes+pycodestyle added to dev deps (both Python 3.10 and 3.12 affected)
- 80 ruff lint errors reduced to 0
- Flaky LSP integration test: `_lsp_read_response` retry on select timeout

## [0.10.2] — 2026-07-14

### Added
- `GITREINS_MAX_ITERATIONS`, `GITREINS_MAX_TIME`, `GITREINS_MAX_INPUT_TOKENS`, `GITREINS_MAX_OUTPUT_TOKENS` environment variable overrides. Highest priority — always win over config file values.

## [0.10.1] — 2026-07-14

### Fixed
- Evaluator HTTP 400 on DeepSeek: per-request `max_tokens` now uses `max_tokens_per_call` (default 16384) instead of sending the full session budget
- Added `max_tokens_per_call` config key under `evaluator`

## [0.10.0] — 2026-07-14

### Added
- **CodeRabbit-style commit review engine** — `commit_audit` section with three review modes:
  - `message`: validate commit message vs diff (original Tier 2)
  - `review`: single-pass code review for bugs, security, anti-patterns
  - `agent`: multi-turn LLM with read_file/search_pattern for deep analysis
- Configurable review checks: bugs, security, anti_patterns, style, performance
- `review_severity` control (critical-only / standard / all)
- `review_suggest_fix` toggle for inline fix suggestions
- `sandbox/test_review_sample.py` live demo

## [0.9.1] — 2026-07-14

### Changed
- Raised tight timeouts: LSP 10s→60s, lint 30s→120s, git commands 10s→60s, PyPI check 5s→15s
- Config fix instructions now included in timeout error messages

## [0.9.0] — 2026-07-14

### Added
- **LLM commit message auditor** — Tier 2 validates commit messages against staged diffs
- Configurable strictness: lenient / standard / strict
- Configurable mode: warn / block / suggest
- `show_diff` config key — displays git diff alongside audit results

## [0.8.2] — 2026-07-14

### Fixed
- `max_output_tokens` default 128K with per-provider clamping
- Language-aware default pipeline (prevents pytest on Go projects)
- `pass_on_error` config key
- Expanded API key fallback chain (KIMI, GROQ, OPENROUTER)

## [0.8.1] — 2026-07-13

### Fixed
- `tier1_passed: null` regression from default pipeline injection in `load_pipeline_config()`

## [0.8.0] — 2026-07-13

### Added
- **Evaluator `file_scope`** — restrict analysis to changed files only, no full-codebase chasing
- Graded release with config trailing zeros cleanup

## [0.7.9] — 2026-07-13

### Changed
- Defaults tuned for large-context models: `code_context_budget: 0.70`, `compaction_threshold: 0.90`
- LSP fixed (pyflakes deps), 746/746 tests pass

## [0.7.8] — 2026-07-13

### Added
- Configurable `compaction_threshold` (default 0.70) and `code_context_budget` (default 0.30)
- 5 regression tests for compaction behavior

## [0.7.7] — 2026-07-13

### Added
- **Token budget awareness** — LLM evaluator knows its limits
- `max_input_tokens` cap read from evaluator config
- Code context capped at 30% of input budget, compaction triggered at 60%

## [0.7.6] — 2026-07-13

### Added
- LSP guard (Tier 1, off by default) — catches undefined vars, type errors per-staged-file
- Static analysis guard (Tier 1, off by default) — runs type checker
- Both feed optional diagnostics to Tier 2 LLM evaluator

## [0.7.5] — 2026-07-13

### Added
- **Evaluator compaction** — context checkpointing + resume loop for large projects
- **Code context pre-loading** — evaluator gets changed code in initial prompt
- `check_for_updates: true` with update notifications

### Changed
- `max_tokens` default 2048→131072

## [0.7.4] — 2026-07-12

### Added
- Code context pre-loading — evaluator gets changed code in initial prompt

## [0.7.3] — 2026-07-12

### Added
- `mcp_gitreins_configure` — hot-reload LLM keys/model at runtime

## [0.7.2] — 2026-07-12

### Added
- `.gitleaks.toml` auto-generation — 500x faster secrets scan (256MB→509KB, 30s→52ms)

## [0.7.1] — 2026-07-12

### Added
- Dogfooded GitReins on itself — pre-commit hook blocks secrets

## [0.7.0] — 2026-07-12

### Added
- **Verdict persistence** — `.gitreins/history/` with verdict.json per judge run
- **Smart init** (`gitreins init`) — auto-detects language, test command, size-appropriate caps
- **diff/full test modes** — `test_mode: diff` only runs tests on changed packages
- Cleaner guard output

## [0.6.0] — 2026-07-11

### Added
- **Diff-mode test selection** for pre-commit guards — only tests packages with staged changes
- Safety trigger: full suite runs when config/pyproject.toml/Makefile is staged

## [0.5.1] — 2026-07-11

### Fixed
- CI fix + GitHub/PyPI metadata

## [0.5.0] — 2026-07-11

### Added
- **Unified defaults** (`engine/config.py`) — single source of truth for all config values
- **Update checker** — notifies when new versions are available on PyPI

## [0.4.1] — 2026-07-10

### Fixed
- Default model changed from `deepseek-chat` (legacy) to `deepseek-v4-flash`

## [0.4.0] — 2026-07-10

### Added
- **DeepSeek defaults** — canonical model names, API base URLs
- **Cache token tracking** — `cache_read_tokens` / `cache_write_tokens` in evaluator output
- Per-provider `max_output_tokens` clamping

## [0.3.2] — 2026-07-09

### Added
- Pipeline cap regression tests

## [0.3.1] — 2026-07-09

### Changed
- Pipeline `max_iterations` defaults to -1 (unlimited) so evaluator config takes over

## [0.3.0] — 2026-07-09

### Added
- **Individual cap keys** — `max_time`, `max_input_tokens`, `max_output_tokens` alongside `max_iterations`
- **Tool-call discount** — tool calls cost 0.1 iterations (10 tool calls = 1 reasoning turn)
- `eval_cap` string format for combined caps
- Real LLM integration tests for eval cap stopping behavior

## [0.2.2] — 2026-07-08

### Added
- Decimal token notation support (0.1M, 1.5k)

## [0.2.1] — 2026-07-08

### Added
- Cross-repo `workdir` parameter on MCP tools

## [0.2.0] — 2026-07-08

### Added
- **Flexible evaluator caps** — iterations, time, and token budgets
- Cross-repo task workdir
- OpenRouter secrets detection

## [0.1.4] — 2026-07-07

### Changed
- Evaluator default max_iterations: 15→100

## [0.1.3] — 2026-07-07

### Added
- `max_iterations` exhaustion tests + default=100 verification

## [0.1.2] — 2026-07-06

### Added
- Linux man page installation support

## [0.1.1] — 2026-07-06

### Added
- `gitreins install` subcommand

## [0.1.0] — 2026-07-05

### Added
- **Initial release** — pip-installable Python package
- Pre-commit hook: secrets detection (regex-based)
- Tier 1 guards: secrets, lint, build, tests
- Dead code detector (Python AST-based)
- Skylos multi-language dead code detection (opt-in)
- `eval-runner.py` pattern for standalone evaluation
- 221 tests (unit + integration)
- GitHub Actions CI (lint + test + guard)
- MIT LICENSE, CONTRIBUTING.md, SECURITY.md

[Unreleased]: https://github.com/totalwindupflightsystems/gitreins/compare/v0.10.2...HEAD
[0.10.2]: https://github.com/totalwindupflightsystems/gitreins/compare/v0.10.1...v0.10.2
[0.10.1]: https://github.com/totalwindupflightsystems/gitreins/compare/v0.10.0...v0.10.1
[0.10.0]: https://github.com/totalwindupflightsystems/gitreins/compare/v0.9.1...v0.10.0
[0.9.1]: https://github.com/totalwindupflightsystems/gitreins/compare/v0.9.0...v0.9.1
[0.9.0]: https://github.com/totalwindupflightsystems/gitreins/compare/v0.8.2...v0.9.0
[0.8.2]: https://github.com/totalwindupflightsystems/gitreins/compare/v0.8.1...v0.8.2
[0.8.1]: https://github.com/totalwindupflightsystems/gitreins/compare/v0.8.0...v0.8.1
[0.8.0]: https://github.com/totalwindupflightsystems/gitreins/compare/v0.7.9...v0.8.0
[0.7.9]: https://github.com/totalwindupflightsystems/gitreins/compare/v0.7.8...v0.7.9
[0.7.8]: https://github.com/totalwindupflightsystems/gitreins/compare/v0.7.7...v0.7.8
[0.7.7]: https://github.com/totalwindupflightsystems/gitreins/compare/v0.7.6...v0.7.7
[0.7.6]: https://github.com/totalwindupflightsystems/gitreins/compare/v0.7.5...v0.7.6
[0.7.5]: https://github.com/totalwindupflightsystems/gitreins/compare/v0.7.4...v0.7.5
[0.7.4]: https://github.com/totalwindupflightsystems/gitreins/compare/v0.7.3...v0.7.4
[0.7.3]: https://github.com/totalwindupflightsystems/gitreins/compare/v0.7.2...v0.7.3
[0.7.2]: https://github.com/totalwindupflightsystems/gitreins/compare/v0.7.1...v0.7.2
[0.7.1]: https://github.com/totalwindupflightsystems/gitreins/compare/v0.7.0...v0.7.1
[0.7.0]: https://github.com/totalwindupflightsystems/gitreins/compare/v0.6.0...v0.7.0
[0.6.0]: https://github.com/totalwindupflightsystems/gitreins/compare/v0.5.1...v0.6.0
[0.5.1]: https://github.com/totalwindupflightsystems/gitreins/compare/v0.5.0...v0.5.1
[0.5.0]: https://github.com/totalwindupflightsystems/gitreins/compare/v0.4.1...v0.5.0
[0.4.1]: https://github.com/totalwindupflightsystems/gitreins/compare/v0.4.0...v0.4.1
[0.4.0]: https://github.com/totalwindupflightsystems/gitreins/compare/v0.3.2...v0.4.0
[0.3.2]: https://github.com/totalwindupflightsystems/gitreins/compare/v0.3.1...v0.3.2
[0.3.1]: https://github.com/totalwindupflightsystems/gitreins/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/totalwindupflightsystems/gitreins/compare/v0.2.2...v0.3.0
[0.2.2]: https://github.com/totalwindupflightsystems/gitreins/compare/v0.2.1...v0.2.2
[0.2.1]: https://github.com/totalwindupflightsystems/gitreins/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/totalwindupflightsystems/gitreins/compare/v0.1.4...v0.2.0
[0.1.4]: https://github.com/totalwindupflightsystems/gitreins/compare/v0.1.3...v0.1.4
[0.1.3]: https://github.com/totalwindupflightsystems/gitreins/compare/v0.1.2...v0.1.3
[0.1.2]: https://github.com/totalwindupflightsystems/gitreins/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/totalwindupflightsystems/gitreins/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/totalwindupflightsystems/gitreins/releases/tag/v0.1.0
