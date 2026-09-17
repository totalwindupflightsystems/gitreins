# Changelog

All notable changes to GitReins will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **MCP onboarding: the server names itself, and a client can be written from
  the docs (DF-GITREINS-POC-5)** — the stdio server used to start silently: a
  client that piped a request in and read nothing back could not tell "still
  starting" from "died before reading stdin", and `docs/mcp-api.md` had no
  usable client example. `run_stdio()` now writes exactly one acknowledgement
  line to **stderr** (name, version, protocol, tool count, resolved workdir)
  and one named exit line on stdin EOF — stderr because stdout is
  protocol-pure and an unsolicited line there would corrupt the stream — and
  `python -m gitreins_mcp.server --version` answers the version without
  opening a transport. `docs/mcp-api.md` gained a copy-pasteable raw JSON-RPC
  quick start (shell one-liner + a 20-line Python client) naming the three
  traps that cost a debugging session: one response per line,
  `notifications/*` never answers, and unknown method/tool is `-32601`.
  `docs/cli-reference.md` documents the acknowledgement and exit line.
- **QA run ledger (QA-GITREINS-POC-7)** — QA verdicts were recorded nowhere
  durable: `worktree fresh|repro|dogfood` outcomes lived in stdout and in the
  gitignored, ceiling-pruned disposable registry, so the harness record covered
  `task complete` verdicts (dev / foreman work) and no QA verdicts at all.
  Every QA run now appends one row to a QA ledger, and `gitreins qa record`
  accepts a run produced outside the harness (a fleet QA lane, a bunker
  battery). `gitreins qa list` reads it back and `gitreins report` prints a QA
  block. Rows carry the fleet QA-ledger keys (`ts`, `project`, `status`,
  `cells`, `findings`, `evidence`, `note`) plus harness extras (`kind`,
  `verdict`, `run_id`, `exit_code`, `commit`, `harness_version`, `detail`), so
  a consumer that already reads that schema can read a harness-written ledger;
  `GITREINS_QA_LEDGER` points the ledger at a fleet file. Recording never fails
  the run it records — a write failure or `qa_ledger.enabled: false` is
  reported on stderr and the run's exit code is unchanged.

### Fixed
- **The MCP handshake reported a version that disagreed with the CLI and
  README (DF-GITREINS-POC-5)** — `initialize` answered with a hardcoded
  `"version": "0.1.0"` while the shipped release was 0.13.0, so a client
  reasoning about the tool surface from `serverInfo.version` reasoned about
  the PoC (12 tools shipped since; `docs/mcp-api.md` even called the field "a
  display constant"). `serverInfo.version` now reports `engine.version` — the
  same source `gitreins --version` reads, falling back to `pyproject.toml` on
  a bare checkout — and `PROTOCOL_VERSION`/`SERVER_NAME` are single constants.
  A regression test pins CLI == package == handshake and fails if a doc pins a
  stale server version literal; `docs/architecture.md` no longer opens with a
  frozen "IMPLEMENTED (v0.1.0)" banner, and the PoC-era transcripts in
  `specs/02-MCP-Protocol.md` carry a note saying where the live value comes
  from.
- **Tier 1 evidence was cut mid-line and the 4 KB cap was not a cap
  (DF-GITREINS-POC-5)** — the head+tail bound sliced at raw character offsets,
  so a verdict's `output` ended in a broken fragment
  (`…tests/test_case_50 PASSED [`) and resumed with the other half of that same
  line, and the omission marker was added on top of the budget (4027 chars for
  a 4000 cap). Both cuts now land on line boundaries and the marker is charged
  against the cap, naming how many chars and lines went and flagging the one
  documented exception (a single line longer than its side's budget — minified
  JSON, one huge traceback line — is cut mid-line and says so). Hoisted
  FAILED/ERROR ids are themselves budgeted, and the marker spends only the room
  the cap actually leaves: a 200-char budget on a 200-failure payload reports
  how many ids it could not carry instead of returning 1123 chars for it (the
  Tier-2 judge caught that on the first submission of this row). Live: a 28 KB
  pytest payload serializes to 3891 chars, head ending at a newline, tail
  starting at a test line.
- **A whole-tree lint graded files the repo's own ruff config excludes
  (DF-GITREINS-POC-18)** — `exclude`/`extend-exclude` apply only while ruff
  recurses into directories, so passing an explicit file list (what `gitreins
  guard` does) linted a tracked scratch tree the repo deliberately excludes:
  `guard --full` reported F401/E402 in `sandbox/` on every run and could never
  go green, which trains readers to ignore the strongest gate. The lint lane
  now lints with `ruff check --force-exclude` and resolves the real scope with
  `--show-files`, reporting it (`ruff: clean (96 tracked files, 19 excluded by
  config)`); a list the config excludes ENTIRELY is a named skip, not a clean
  pass. Diff/staged mode follows the same rule.
- **`-x` + xdist made a real test failure exit 2, and the judge read it as an
  interruption (INT-FLAKE-2)** — xdist's `DSession` raises
  `Interrupted(KeyboardInterrupt)` when maxfail trips, which pytest maps onto
  `ExitCode.INTERRUPTED`, so a failing suite reported the same code as a
  signalled run. The tier1 tests step now records `data.pytest_outcome`
  (`kind`/`detail`/`first_failing_test`/`failures`/`interrupted`, via
  `engine.types.pytest_outcome`) instead of leaving a bare exit code, and says
  `maxfail` (real failure) vs `interrupted` (signalled) vs
  `interrupted-unclassified` (evidence too short) explicitly.
- **Step capture kept only the first 2000 chars** — the head-only slice landed
  where pytest's short test summary begins, so the DF-GITREINS-POC-8
  head+tail evidence bound had no tail to preserve and the failing test id
  never reached `verdict.json`. The whole output is now kept and bounded once,
  at serialization.
- **A CLI test could silently acquire a live provider call (INT-FLAKE-1)** —
  `test_full_task_lifecycle_subprocess` completed a task without
  `--skip-tier2`, so whenever the caller's shell exported one of the
  credentials `engine/llm.py` falls back to (a foreman session does) the
  child ran a real Tier 2 evaluation inside the test's 30 s subprocess
  timeout: green in CI, intermittently red under the parallel guard. Every
  CLI subprocess test now starts from a hermetic environment (ambient
  provider credentials stripped, LLM endpoint pinned to a loopback dead
  port), each lifecycle step asserts its exit status and reports both
  streams on failure, and two regression tests pin the failure mode — a
  sentinel socket proving a Tier 1-only lifecycle never dials the endpoint,
  and four concurrent lifecycles proving no shared task store.

## [0.13.0] — 2026-09-16

### Added
- **Worktree fleet** — per-task worktrees (`gitreins task worktree <id>`, registry,
  list/clean), canonical shared board across trees, worktree-correct guard/judge semantics
  (merge-base diff, staged-scoped secrets, verdict tree stamps), judge-gated merge-back
  (AUTO/REBASE/HOLD/MANUAL), and a bounded parallel worker fleet
- **`gitreins serve`** — live judgment browser: local web server + JSON API over
  `.gitreins/history`, board events, and the scheduler tick ledger
- **Judge verdict evidence bounding** — step evidence capped/split (head+tail) with
  FAILED/ERROR lines hoisted; guard failures name the failing test
- **Docs-drift CI guard** — README banner version vs pyproject + count claims enforced
  (`scripts/check_docs_drift.py`)

### Fixed
- config-less `guard`/`commit` false-green (refuse without config; MCP `guard.run` too)
- single-flight judge race across instances; orphaned async-job resume leases
- deepseek max_output_tokens clamp; pytest exit-5 treated as pass-with-warning
- hermetic static-tool discovery (cppcheck PATH independence); test interpreter pinning


### Added
- **PR #2 (carterlasalle, merged 4779fd2)** — quality-gate correctness + evaluator hardening:
  - **Four "PASS-but-failing" bugs fixed** (all reproduced live on main, fixed on branch, 8 regression tests in `tests/test_quality_gate_regressions.py`):
    1. Pipeline exceptions returned `passed=True` even without `pass_on_error` (dead code made every pipeline crash pass)
    2. Failing script steps with `on_fail: continue` were marked passed
    3. Default tier-1 lint/test commands carried `2>/dev/null || true`, zeroing exit codes (all languages)
    4. Cap-hit partial verdicts reported COMPLETE when only ANY criterion was verified
  - Mandatory test-verification hard rule in the evaluator prompt (no PASS on test/build claims without command output)
  - `scan_security` ast-grep tool (CodeRabbit essential rules, SARIF findings) for deterministic security scanning
  - `prompt_template` pipeline config is now actually wired through as the evaluator system prompt (was a no-op `pass`)
  - Judge token usage persisted to `.gitreins/usage.jsonl` (best-effort cost telemetry, now gitignored)
  - Default-pipeline built-in secrets scanner runs under `sys.executable` + `PYTHONPATH` (fixes ModuleNotFoundError outside the venv)

## [0.12.1] — 2026-08-28

### Fixed
- **Wheel version drift (DF-015)** — the 0.12.0 wheel shipped a static `engine/version.py` reporting 0.11.0 while METADATA said 0.12.0. The built wheel now reports the installed metadata version (`engine/version.py` stays importlib.metadata-dynamic, no static literal); `gitreins --version` in the 0.12.1 wheel prints `gitreins 0.12.1`.

## [0.12.0] — 2026-08-14

### Added
- **Disk-backed async judge jobs (DF-006)** — background evaluation jobs are persisted to a shared job store (`~/.local/share/gitreins/jobs/`, override with `GITREINS_JOB_DIR`) instead of living only in MCP server memory. Jobs survive MCP server restarts; an orphaned `running` job whose process died is resumed automatically on the next `judge.status` poll. New CLI flow: `gitreins judge <task> --async` dispatches a detached worker process (survives the CLI exiting), `gitreins judge <job_id> --status` polls it (exit codes: 0 complete / 1 error / 2 running). MCP `judge.status` sees CLI-started jobs and vice versa; running responses include `pid`/`started_at`. `StepResult.to_dict()` now serializes structured `data` (verdict/items/summary) so pipeline-path results are included in judge result dicts.
- CVE-style scored severity system for commit review (`review_score_threshold`, `review_score_offset`)
- Anthropic Messages API endpoint support (auto-detected provider routing)
- DeepSeek prompt caching telemetry (`cache_read_tokens` / `cache_write_tokens` in evaluator output)
- Large-repo hardening: fast-track mode, aggressive timeout respect, `--skip-tier2` flag, token budget overflow protection
- Expanded language coverage: C++, Go, Java, Kotlin, C#, Swift, Dart, Elixir, Scala LSP + static analysis + pipeline
- MCP `propagate` tool for multi-repo quality config distribution
- Type-safe `GuardResult` / `Tier1Result` frozen dataclass (engine/types.py)
- Dedicated test files for types, guards, propagate, persist, config (+109 tests)

### Changed
- MCP: judge.evaluate/task.complete now run evaluations asynchronously (judge.status to poll) — fixes 300s client-side tool-call timeouts
- `max_input_tokens: -1` treated as unlimited (use with care — can hang on large repos)
- Default `code_context_budget: 0.70`, `compaction_threshold: 0.90`

### Fixed
- Duplicate `_parse_staticcheck` function (shadowing bug from commit 4d5f01a)
- CVE-2026-59950: bumped mcp 1.28.0→1.28.1 (Cross-Site WebSocket Hijacking, CVSS 7.6)
- LSP integration tests in CI: pyflakes+pycodestyle added to dev deps (both Python 3.10 and 3.12 affected)
- 80 ruff lint errors reduced to 0
- Flaky LSP integration test: `_lsp_read_response` retry on select timeout
- Pre-commit hook now pins the gitreins binary that ran `install` (absolute path or `python -m gitreins`) — PATH shadowing can no longer silently run a different version that skips guards (DF-011)
- Secrets guard cross-checks gitleaks-clean results against the built-in scanner in the judge pipeline tier1 (workdir mode), and the generated `.gitleaks.toml` now includes ghp_/glpat-/AIza rules (DF-012)
- Generated `.gitleaks.toml` allowlists now emit escaped regexes (`.*\.log`) instead of bare globs — bare globs made gitleaks panic and the secrets guard fail forever on fresh installs (DF-001, fixed 9a54e79, first shipped in this release)
- `gitreins init` now detects Python for plain-Python repos without a pyproject.toml (previously reported `Language: unknown` and disabled static analysis without warning — GR-GAP-026, first shipped in this release)

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
