# Changelog

All notable changes to GitReins will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **`ruff format` is gated in CI and in the guard's lint lane (GR-GAP-063)**
  — formatting cleanliness lived only as prose in task briefs: the CI workflow
  grepped zero hits for `ruff` (it ran no ruff at all), and
  `.gitreins/config.yaml`'s lint lane graded `ruff check` alone, which accepts
  a file the formatter would rewrite. Nine tracked files had drifted from
  `ruff format` and no gate could see it until an idle sweep paid it down
  (repaired in `4a55784`) — and the tree drifted again after that. Both halves
  now run `ruff format --check`: a `Verify formatting with ruff format --check`
  step in `.github/workflows/ci.yml` (before `Run guards`, using the ruff the
  existing `pip install -e ".[dev]"` step already installs) and a format
  sub-check inside `GuardManager._check_lint` over the SAME graded scope the
  check step used (`--force-exclude`, so a config-excluded file is not graded
  merely because it was named; an all-excluded list stays the named skip it
  was). The lane's verdict shape is unchanged — one `lint` `GuardResult`,
  formatting failures failing it — and the failure output names every drifted
  file plus the `ruff format <files>` command that fixes them. `--check` and
  not `--diff`/bare `ruff format`: those exit 0 on differences (and the bare
  form silently rewrites the tree), the false-green shape GR-GAP-061 hit.
  Regression: `tests/test_guard_format_check.py` (RED with the sub-check
  disabled — 7 failures) alongside `tests/test_ci_workflow_pins.py`'s
  `TestRuffFormatGate`.

### Fixed
- **`gitreins commit-audit` names its skip and honors the documented `mode`
  placement (DF-GITREINS-POC-30)** — the command promised "Validate a commit
  message against staged diff" and, on the state `gitreins install` + `init`
  leave behind (no `commit_audit` stage in `pipeline.stages[]`), exited 0 with
  stdout AND stderr empty: it audited nothing and said nothing, so a
  commit-msg hook was indistinguishable from a passing audit. It now prints a
  named skip line and still exits 0:
  `commit audit: no pipeline stage with type commit_audit for trigger commit-msg — audit NOT run`
  (the wording distinguishes the three states — no stage, stage not armed for
  this trigger, stage armed but its `condition` excluded it — and the
  not-armed case names the `on: [commit-msg]` fix). `mode` also resolves with
  an EXPLICIT precedence now: the pipeline stage's own `mode` >
  `defaults.commit_audit.mode` > top-level `commit_audit.mode` > `warn`.
  `_load_commit_audit_config` returned `cfg.get("commit_audit", {})` — top
  level only — so the stage-scoped placement the CLI reference described was
  dead config (a stage-level `mode: block` stayed "(Warning only — commit will
  proceed)" with exit 0) and `defaults.commit_audit.mode` was dead entirely;
  only the undocumented top-level placement blocked. The top-level key is
  still honored, so nothing that worked before changed behavior — verified
  against all four measured placements plus the CLI as a subprocess in a
  scratch repo. Regression: `tests/test_commit_audit.py`
  (`TestResolveCommitAuditMode`, `TestCommitAuditModePrecedence`,
  `TestCommitAuditSkipLine` — 16 of 18 RED against the unfixed engine, the two
  that stay green being the backward-compat guards).
- **`judge.status` payloads carry an additive `running` boolean and the docs
  ship a poll loop keyed on the terminal status set (DF-GITREINS-POC-24)** —
  the payload's `{"status": "running"}` is a poll-phase value; the terminal
  value is `{"status": "complete" | "error"}`, and a client following the
  natural "poll until running == false" pattern had no field to poll and
  looped forever. Every `judge.status` payload (and every disk job record,
  MCP and CLI alike) now also carries `"running": true` while the job is
  dispatched/running and `"running": false` once terminal; the three `status`
  strings and every existing field are unchanged, and a record written by an
  older build (no `running` key) is reported as `running: false`. An
  old-build record that gets auto-resumed reports `running: true` again (the
  resume claim write is a current-build write). docs/mcp-api.md §11 gained a
  worked poll loop keyed on `status in {"complete", "error"}` with an
  explicit warning against polling a bare `running` field on older builds.
  Regression: `tests/test_mcp_server.py::TestJudgeAsyncPersistence` —
  `test_judge_status_running_boolean_fresh_and_terminal` (exact key-set pins
  for the fresh/running and terminal payloads), `test_old_build_record_
  without_running_key_reports_not_running`, `test_old_build_record_resumes_
  and_reports_running` (all three RED on the pre-fix code: `KeyError:
  'running'`), plus a record-shape assertion in `tests/test_job_store.py`.
- **The tier-2 compaction valve meters the cumulative budget it protects
  (GR-GAP-062)** — the proactive compaction check compared
  `cumulative_prompt_tok`, which the loop maintains as the LARGEST SINGLE
  CALL's prompt size (`engine/evaluator.py:1218`, `max(...)`), against
  `compaction_threshold × max_input_tokens`, a share of the CUMULATIVE input
  budget that `EvalCap` enforces (`engine/eval_cap.py:122,177`). On any rung
  where the budget dwarfs a single prompt (the fleet runs 2M–24M against
  ~50k prompts) the threshold was unreachable: compaction never fired,
  `reset_context_tracking()` never ran, and the counter walked into the hard
  cap, which returns INCOMPLETE and loses the verdict mid-write. The valve now
  reads `self.eval_cap.cumulative_input_tokens` — the same quantity the cap
  meters — and the warning line shows both figures so operators can tell them
  apart. Regression:
  `tests/test_evaluator.py::TestTransportFailureClassification::test_compaction_fires_on_cumulative_consumption_not_prompt_size`
  (RED under the old comparison: 1000-token prompts against a 5%×100k
  threshold never fired; GREEN: fires once cumulative consumption crosses
  5000).
- **An MCP-dispatched evaluation now persists its verdict into
  `.gitreins/history` (DF-GITREINS-POC-23)** — the MCP paths wrote only the
  job record, so `gitreins serve` / `gitreins report` / the static judgment
  page never showed an MCP-driven run (live: a completed
  `~/.local/share/gitreins/jobs/job-326f9cbf….json` beside a workdir with
  history enabled whose `.gitreins/history` did not exist). The verdict
  construction and the persister call moved out of the CLI into the shared
  `engine.persist.build_verdict_data` / `persist_evaluation`, which the CLI,
  the MCP async job and the MCP `wait=true` sync path all call; the record
  carries `job_id` and a `source` marker (`mcp` / `mcp-sync`). Persistence is
  non-fatal and silent on stdout (the MCP channel is JSON-RPC), and the async
  job persists BEFORE it lands its terminal state, so a job that reads
  `complete` always has its verdict on disk.

## [0.14.0] — 2026-09-18

### Added
- **An executable `python -m gitreins` — the interpreter form installed
  pre-commit hooks pin (DF-024, 509acff)** — the generated hook pins the
  gitreins that ran `install` (the absolute console-script path when one
  launched the command, otherwise `<sys.executable> -m gitreins`), and the
  package had no `__main__.py`, so that second form could never run:
  `/.../python: No module named gitreins.__main__; 'gitreins' is a package and
  cannot be directly executed` (exit 1). Because the hook ends with `exit $?`,
  every repo whose hook carried the pinned interpreter form had a pre-commit
  gate that BLOCKED all commits with an opaque Python message instead of
  running secrets/lint/tests — while the CHANGELOG, the hook template's own
  comment and `docs/dogfood/diagnostics.md` all advertise that form.
  `gitreins/__main__.py` delegates to `cli.main` (`sys.exit(main())`) with no
  other side effects, so `python -m gitreins <command>` behaves exactly like
  the console script — including the exit code, which is what the hook depends
  on. `tests/test_cli.py::TestPreCommitHookPathPinning::test_pinned_python_m_invocation_is_actually_runnable`
  runs the pinned form from a foreign cwd (the consumer-install shape) and
  asserts exit 0 plus a version banner.
- **A board id gate so one id means one finding (QA-GITREINS-POC-8, 4495a60)** —
  the QA filing path numbered its per-cycle findings from 1, so each cycle
  re-used `QA-GITREINS-POC-1` (later -2, -3) for a different finding: 12 rows
  ended up sharing 3 ids, title/id dedupe could never match, and the board
  reported the duplicates as pre-existing errors on every run.
  `scripts/check_board_ids.py` fails on a NEW duplicate id, a row missing
  id/title/status, and a baseline entry whose count no longer matches the board
  (the baseline may only shrink); `.coding-hermes/board/id-baseline.json`
  grandfathers the 12 legacy rows by count, `.coding-hermes/board/README.md`
  writes the rule down, and CI runs the gate as **"Verify board id hygiene"**.
- **QA runs in the static judgment page too (JVIEW-007)** — the QA run ledger
  became a first-class data source, `gitreins serve` exposes it at `GET /api/qa`
  and renders it, but `scripts/judgment_viewer.py` (the standalone page published
  without a server) still showed task verdicts only, so the harness' own QA
  history stayed invisible exactly where the record is browsed off-line. The
  generator now reads the same `engine.qa_ledger` rows and renders a **QA Runs**
  panel with a PASS/FAIL badge, kind, cells summary, exit code, commit and the
  ledger path under the list — and degrades to "no QA runs recorded (ledger: …)"
  on an absent, unreadable or half-garbage ledger instead of dying. Tests:
  `tests/test_judgment_viewer_script.py` (4 — absent ledger, rows present,
  unreadable ledger, and a generated page carrying the section).
- **Per-judgment tokens and cost in the judgment viewer (JVIEW-006)** — the
  judge's token spend lives in `.gitreins/usage.jsonl` and carries no task id, so
  the economics of quality were invisible next to the verdicts that produced it.
  `engine/usage.py` attributes each usage line to the verdict whose
  `evaluated_at` is the earliest one at or after the line's `ts` (1:1, so a line
  is never double-counted; a line that precedes no verdict stays unattributed),
  and `gitreins serve` now exposes the join: `GET /api/verdicts/<d>/<h>` carries
  a `usage` block (`tokens_in/out`, `cache_read/write`, `rows`, `steps`,
  `cost_usd`, `priced`, `model`) when telemetry is traceable, `GET /api/stats` an
  aggregate `usage` summary (`judgements`, `verdicts`, `unattributed`, tokens,
  `cost_usd`, `priced`/`unpriced`, `prices_configured`). Costs come from the
  checkout's own rates (`usage.price_per_1m_input/_output`, model defaulting to
  `defaults.model`) — with none configured the reader reports tokens with
  `priced: false` and `cost_usd: null` instead of inventing a rate. The SPA shows
  a cost badge in the detail pane and an aggregate Judge spend card in the stats
  header (`unpriced` + the reason when rates are missing).
- **Worker evidence embedded in the verdict directory (JVIEW-005)** — a verdict
  recorded *what* was decided but not the run that produced it: the worker brief
  and driver log usually live in `/tmp` and die with the tick, and the record
  named a commit without the patch the judge actually graded. `task complete`
  now copies the run's artifacts next to `verdict.json` — `worker-brief.md`
  (`GITREINS_WORKER_BRIEF`, else `<checkout>/.gitreins/worker-brief.md`, first
  32 KiB), `driver-log.tail.txt` (`GITREINS_DRIVER_LOG`, last 16 KiB),
  `commit.patch` (the patch of the stamped commit — the fix as landed) and
  `worktree.patch` (`git diff HEAD`, the uncommitted diff the judge read, kept
  separate so a permanently dirty checkout cannot pass its noise off as the fix;
  patches bounded at 256 KiB) — lists them in `verdict.json → evidence.items`
  with `bytes`/`truncated`/`source`, and `gitreins serve` renders them as an
  **Evidence** section in the detail pane behind an additive, manifest-bound
  route (`/api/verdicts/<date>/<hash>/evidence/<name>`). Collection is
  best-effort: an absent or unreadable source is omitted (never faked as an
  empty file) and can never fail a verdict; a clipped artifact names the bytes
  it dropped.
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
- **A truncated task store loaded as a silent partial list (DF-GITREINS-POC-22)** —
  `yaml.dump` always terminates the store with a newline, so a store whose final
  byte is not one was cut short mid-write; the loader accepted whatever parsed,
  returned rc 0, printed no warning and left no `.corrupt-<hash>` sidecar — so
  the next write destroyed the dropped tasks. The loader now reports the
  truncation loudly on **stderr** (stdout stays JSON-RPC-pure for the MCP stdio
  server, POC-5 law), preserves the raw bytes aside through the same
  content-addressed mechanism as an unreadable store, and still serves what
  parsed; the MCP fixture seeds `tasks.yaml` in canonical form.
- **A zero-work `gitreins guard` printed a clean PASS (TRUST-001)** — on a clean
  tree the guard printed "Tier 1 Guards: PASS" with exit 0 while its own
  persisted log said `lint="No Python files staged"` and
  `tests="No files staged — skipped"`, and the LSP gate reported "clean"
  whenever its server was not installed: a gate that never ran was
  indistinguishable from one that passed, which CI and merge-back both consume
  as truth. Skips are now labelled (`~` plus the reason instead of a checkmark,
  zero-work skips in lint/tests/lsp/static-analysis), `cmd_guard_run` prints
  `Tier 1: DEGRADED PASS (skips: ...)` and exits **2** unless
  `guards.allow_skips` is true (`init` writes `allow_skips: true` for fresh
  repos), `verdict.json` carries
  `stages.tier1.{degraded,skipped_steps,degradation_reason}`,
  `gitreins worktree merge` refuses a PASS whose Tier 1 record carries skips,
  and the guard run log records a DEGRADED overall line plus `[SKIP]` entries
  with the reason. A guard disabled by config, or replaced by the language's own
  gate (Go vet/test/build), is not a degradation.
- **Corrupted state was destroyed instead of preserved (QA-GITREINS-POC-6)** — an
  unreadable `.gitreins/tasks.yaml` was warned about and then REPLACED
  (200-byte payload → 145-byte fresh file, no copy anywhere, exit 0); a
  binary-corrupted QA ledger and `verdict.json` killed `qa list`/`report` with
  `UnicodeDecodeError` tracebacks and lost the rows already salvaged; an
  undecodable `.gitreins/config.yaml` raised out of `load_defaults()` instead of
  falling back. The task store is now preserved as
  `<tasks.yaml>.corrupt-<sha256[:12]>` before any write (content-addressed, so
  repeated loads do not churn sidecars) and the write is REFUSED when even that
  copy cannot be made (`TaskStateCorruptError` → one `error:` line + exit 1), the
  ledger decodes with `errors="replace"` so a garbage line costs that line only,
  an undecodable verdict is skipped like a malformed one, and an unreadable
  config degrades to the built-in defaults with a named warning while a wrong
  VALUE still raises. 20 tests in `tests/test_corrupted_state_restart.py`,
  observed through the real CLI boundary.
- **MCP `protocolVersion` was pinned to one revision (DF-GITREINS-POC-20)** —
  `initialize` answered the hardcoded `2024-11-05` for every client.
  `SUPPORTED_PROTOCOL_VERSIONS` now advertises the four revisions that share the
  session `initialize` handshake and the tools-only capability surface this
  stdio server implements (2025-11-25 / 2025-06-18 / 2025-03-26 / 2024-11-05):
  `initialize` echoes a supported request, otherwise answers `2025-11-25` plus
  one stderr line naming the mismatch and the revisions a client may retry
  with, and an unknown **notification** (no id) is no longer answered with
  `-32601` — a JSON-RPC notification must not get a response. 2026-07-28 is
  deliberately NOT advertised (it removed the initialize handshake).
- **A quiescent LSP spawn read as a clean tree, and the gopls integration test
  failed on it (INT-FLAKE-4)** — `run_lsp_check` returns `[]` both for "the
  server is healthy and found nothing" and for "the server never checked the
  file", so a load-dependent stall was indistinguishable from a clean tree (and
  the guard's LSP lane reported one as the other). Reproduced under CPU load:
  a gopls v0.22 spawn answers `workspace/symbol` and resolves definitions and
  document symbols while publishing no `publishDiagnostics` for 42 s — and
  re-sending the same content as a `textDocument/didChange` publishes the check
  in **0.01 s**. Root cause: the `didOpen` lands before gopls holds a snapshot
  for the file, after which the check simply never runs. `engine/lsp.py` now
  opens each file once and, when the server has not published for it within
  `recheck_after` (default 5 s — a healthy server publishes in 0.06-1.5 s even
  under load), re-sends the same content as a change to force the check;
  `run_lsp_check_status` reports `published` (did the server report on every
  file — an empty list counts), `rechecks`, `server_ready` (a real
  `workspace/symbol` round trip, opt-in via `probe=True`) and `stalled` with a
  named reason. `run_lsp_check` keeps its diagnostics-only contract for the
  guard. The gopls integration test now retries fresh servers on the readiness
  signal inside a wall-clock stall budget (the attempt count is only a ceiling),
  asserts strictly when the server *did* report, and reports a never-reported
  spawn as a distinct non-failing diagnostic.
- **A detached judge job was polled under a fixed 30 s deadline
  (INT-FLAKE-3)** — the async-dispatch test polls `judge --status` for a
  *detached worker process*, so its runtime scales with machine load while the
  deadline was a constant: one of the Tier-2 judge's twelve parallel full-suite
  runs went red with `subprocess.TimeoutExpired` although the job was healthy
  (11/12 and 8/8 local runs green). The budget is now derived from the measured
  workload — the 1-minute load average per CPU, refined with the wall time of
  the first `judge --status` child actually observed — clamped to 30-240 s, and
  exhausting it is only a failure for a job that is genuinely stuck: a worker
  pid that is gone (or an errored job) still fails, while a worker that is
  still running is reported as a distinct, non-failing slow-run diagnostic.
  Tests cover the budget derivation (idle/loaded/slow-child/capped/non-POSIX),
  the stuck-job failure path and the live-worker diagnostic.
- **The disposable reap treated a benign race as an infrastructure failure
  (INT-CI-11)** — a CI-only red on a board-only commit:
  `could not reap disposable worktree .../.disposable/run-…: fatal: Invalid
  path '<repo>/.git/worktrees/run-…': No such file or directory`, which turned
  `worktree repro -k 3` into a failure (`assert 2 == 0`) and went green on a
  rerun. A parallel repro farm reaps its `k` trees at once, so one run's
  repo-wide `git worktree prune` can delete another run's admin metadata
  (`<git-common-dir>/worktrees/<run-id>`, or the whole `worktrees/` directory
  via `delete_worktrees_dir_if_empty`) between that command's worktree-list
  snapshot and its own path resolution — git then exits non-zero even though the
  tree is already gone. The reap is now **idempotent**: on a non-zero `git
  worktree remove` the decision is made from the registry state, not the exit
  code — a tree git no longer tracks is already reaped (any leftover directory
  is deleted, metadata pruned) and only a tree that is still registered, or a
  directory that cannot be deleted, still raises. All git metadata mutations are
  serialized under the manager's cross-process registry lock, so the farm's own
  reaps can no longer race each other. Before/after proof in the tick record:
  the same stale state raised `WorktreeError` and left the tree on disk before
  the fix, and is reaped cleanly after it.
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
- **CI installed its analyzer from a floating `@latest` (INT-CI-8)** — the
  workflow's `go install honnef.co/go/tools/cmd/staticcheck@latest` resolved on
  the runner, so run 34628610209 failed test (3.12) after v0.8.1 pulled a newer
  Go toolchain and `proxy.golang.org` answered `stream error: stream ID 37;
  INTERNAL_ERROR` for the `golang.org/x/tools` zip — while the identical commit
  passed the next run, i.e. a transient network failure arriving as a code
  failure. The step now pins the version in `STATICCHECK_VERSION`, retries the
  install three times with growing backoff, falls back to `GOPROXY=direct` on
  the last attempt, verifies the installed binary, and fails the job loudly
  when every attempt failed (a bare `for` loop ends on its last `sleep`, so the
  status is checked explicitly instead of read off the loop).
  `tests/test_ci_workflow_pins.py` guards the pin, the retry and the loud
  failure so neither can fall out again.

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
