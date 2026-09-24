---
name: gitreins-usage
description: >-
  How to use the GitReins quality harness in this repo (and any repo it's
  installed in): task lifecycle, guards, LLM judge, MCP tools, and the known
  pitfalls that will bite you. Load this before committing or creating tasks.
version: 1.8.0
category: software-development
---

# GitReins Usage — Task Lifecycle, Guards, Judge, MCP

GitReins is a git-native quality harness: tasks with completion criteria, Tier-1
static guards (secrets/lint/tests) on every commit, and an agentic LLM evaluator
that judges task completion per-criterion. This repo (gitreins-poc) both BUILDS
GitReins and uses it as its own quality gate (dogfood).

## Entry points

| Entry | What it is | Use when |
|---|---|---|
| `gitreins` CLI (`.venv/bin/gitreins`) | full harness | scripts, humans, non-MCP agents |
| `gitreins mcp-server` | MCP stdio server | AI agents with MCP tool access (`task_create`, `task_start`, `task_complete`, `judge_evaluate`, `guard_run`, `commit`, `configure`, `propagate`) |
| pre-commit hook (`.git/hooks/pre-commit`) | runs `gitreins guard` | every `git commit` — cannot be skipped via `--no-verify` for code changes (AGENTS.md) |
| `gitreins report` | verdict history browser | after judge runs |

## Quick workflow (the right way)

```bash
# 1. Create a task with explicit, verifiable criteria
gitreins task create my-task "Do the thing" \
  "file X exists with feature Y" "tests pass"

# 2. Mark in progress, do the work
gitreins task start my-task
# ... implement ...

# 3. Run the judge — agentic LLM evaluates each criterion
gitreins task complete my-task        # triggers evaluation

# 4. Commit — the hook runs Tier 1 guards and BLOCKS on failures
git commit -m "feat: done"
# or: gitreins commit "feat: done"    # same guard, explicit

# 5. Browse verdicts
gitreins report
```

MCP agents: `task_create` → `task_start` → (work) → `task_complete` → `commit`.
The MCP `commit` tool runs guards first and rejects the commit if they fail.

## Guards (Tier 1) — what blocks, what warns

- **secrets** (BLOCKS): gitleaks first, built-in regex scanner fallback. Covers
  sk-/ghp_/glpat-/AKIA/AIza/slack/JWT/password patterns; whitelists common
  false positives (os.getenv, ${VAR}, placeholders).
- **lint** (WARNS): ruff. `*.md` docs-only changes are exempt in practice.
- **tests** (BLOCKS): full or diff mode. Diff mode maps changed files to test
  files by basename; config changes / unmapped files → full suite fallback.
- Config: `.gitreins/config.yaml` → `guards:` (secrets/lint/tests/test_mode/
  test_command/test_timeout). Test command here: `uv run pytest -x --tb=short`,
  timeout 900s (full suite takes ~11 min — don't lower the timeout).

## Judge (Tier 2) — agentic evaluator

- `task complete` runs the evaluator automatically; `gitreins judge <id>` standalone.
- Reads files, runs tests, delivers per-criterion PASS/FAIL, persists verdicts to
  `.gitreins/history/` (git-stored).
- LLM config: `GITREINS_LLM_BASE_URL` / `GITREINS_LLM_API_KEY` / `GITREINS_LLM_MODEL`
  (fallback: OPENAI/ANTHROPIC/DEEPSEEK keys). Model default: deepseek-v4-flash.
- Caps: `evaluator:` config or `GITREINS_MAX_ITERATIONS`, `GITREINS_MAX_TIME`,
  `GITREINS_MAX_INPUT_TOKENS`, `GITREINS_MAX_OUTPUT_TOKENS` env (highest priority).
  For quick tasks bound them (e.g. `GITREINS_MAX_ITERATIONS=12 GITREINS_MAX_TIME=8m`).
- `judge_evaluate` MCP tool accepts eval_cap like `"20/5m/200k/50k"`.

## Pitfalls (learned the hard way — 2026-08-03 dogfood run)

1. **`gitreins init` writes a broken `.gitleaks.toml`** — allowlist contains invalid
   regexes (`*.log`, `*.egg-info/`, `*.spec.md`, `*.md`). If gitleaks is installed,
   it PANICS on every run → `✗ secrets — ○` → commits blocked. Fix: edit those
   entries to `.*\.log` etc., or delete the file (built-in scanner runs). Generator
   fix tracked as task (see board / findings in tasks).
2. **Bare `pytest` may not import your root package** (pytest 9 importlib mode).
   If `gitreins guard` says `✗ tests` but `python3 -m pytest` passes, add
   `[pytest] pythonpath = .` to `pytest.ini`/`pyproject.toml`, or change
   `test_command` to `python3 -m pytest -x --tb=short`.
3. **Guard summaries truncate failure output** (last 2000 chars, summary shows the
   FIRST line). When a guard fails, re-run the failing command yourself to see the
   real error.
4. **`gitreins init` reports `Language: unknown` for plain-Python repos** without
   pyproject.toml — add one first so Python exclusions/tuning apply.
5. **Never commit `.gitreins/tasks.yaml`** — local state. Add `__pycache__/` and
   `.venv/` to your own `.gitignore` before the first `git add -A`.
6. **Never use `os.kill()`/`os.killpg()` without PID validation** in this codebase
   (`int(mock.pid) == 1` kills init — see AGENTS.md, engine/lsp.py:408-428).
7. **MCP `judge_evaluate`/`task_complete` calls time out at 300 s** while the
   server-side evaluation keeps running (tier-1 runs the full suite; this repo's
   suite is ~11 min). For slow-suite repos: run the judge via CLI (`gitreins task
   complete <id>`) or in the background and poll `.gitreins/tasks.yaml` / history.
8. **MCP `commit` refuses while any task is `in_progress`** — "Tasks still in
   progress — complete or delete them first". Complete (judge) the task first, then
   commit. The pre-commit hook does not have this rule.
9. **`gitreins report` only sees verdicts on the `gitreins` branch** — with
   `history.storage: git`, verdicts commit to a separate branch; on `main` the
   report says "No verdict history found". Check out the branch or read the verdict
   files via `git show gitreins:.gitreins/history/...`.
10. **PyPI is 11 days behind HEAD (2026-08-14): 0.11.0 predates the DF-001
    gitleaks-regex fix.** `pip install gitreins` → `gitreins init` writes a
    BROKEN `.gitleaks.toml` (bare `*.log` globs) → `gitreins guard` = `✗ secrets
    — ○` forever. Fix: use the repo `.venv/bin/gitreins` (HEAD) for real work,
    or fix the generated config by hand; track the release in DF-010. Always
    verify a dogfood target by what's ON PYPI, not just repo HEAD — a green
    repo can ship a broken package for weeks.
11. **The pre-commit hook calls bare `gitreins` — PATH shadowing runs a
    DIFFERENT version.** On this machine `/home/kara/.hermes/venvs/board/bin`
    (gitreins 0.8.1) precedes `.venv/bin` (0.11.0), so the hook silently ran
    0.8.1 and let real `sk-`/`ghp_` secrets through. Before trusting a commit
    gate: `which gitreins` from the hook's perspective, or prepend the target
    venv: `PATH="$HOME/gitreins-poc/.venv/bin:$HOME/go/bin:$PATH"`.
12. **gitleaks (default rules AND the generated config) reports `no leaks
    found` for `sk-...` and `ghp_...` patterns** — the built-in regex scanner
    catches both, gitleaks doesn't. "gitleaks clean" is not proof of clean;
    the guard's binary coverage (gitleaks-first vs fallback) is a known hole
    (DF-012).
13. **GR-099 is correctly blocked (pydantic 2.13.4 pins pydantic-core==2.46.4
    exactly; `pip install gitreins pydantic-core>=2.47.0` → ResolutionImpossible)
    — do NOT re-verify it every tick.** Park it with a recheck date; idle
    audits should skip recently-verified blocked tasks (DF-013).

## Fresh install on PyPI 0.12.0 (2026-08-27 dogfood run) — what changed

Verified against the RELEASED wheel in a fresh venv, not repo HEAD:

- **0.12.0 is on PyPI** (08-14) — the 11-day release lag (pitfall 10) is over.
  BUT the wheel's `engine/version.py` statically says 0.11.0 → every command
  prints `Update available: 0.11.0 → 0.12.0` on the already-current install
  (DF-015). `gitreins --version` is NOT trustworthy on the 0.12.0 wheel.
- **The pre-commit hook now pins the installing binary's absolute path**
  (pitfall 11 fixed in the wheel) — no PATH shadowing on fresh installs. If
  the venv moves later, the hook fails with `command not found` (confusing
  but loud).
- **DF-001 gitleaks-regex fix is in the wheel** — a fresh `.gitleaks.toml`
  is valid; gitleaks runs without panicking (pitfall 1 closed for fresh
  installs).
- **Secrets scan reports only the FIRST finding per file** (DF-016): a file
  with `sk-` AND `ghp_` shows one finding. Each pattern is caught in
  isolation. Don't trust "N finding(s)" as the full count for a file.
- **uv on PATH + root-package layout still breaks the tests guard** (DF-017):
  init writes `uv run pytest -x --tb=short`, which cannot import root
  packages (no `pythonpath`). If guard fails with a bare pytest summary,
  run `uv run pytest` yourself and add
  `[tool.pytest.ini_options] pythonpath = ["."]` to pyproject.toml.
- **Guard failure output still won't tell you WHICH test failed** (DF-018) —
  it shows only the last pytest summary line. Re-run pytest directly to see
  failures; MCP `guard_run` returns the full pytest log.
- **init claims "Static analysis: enabled (mypy, pyright)" but writes no
  tools** (DF-019) — the guard no-ops. Don't assume mypy/pyright run.
- Tier-2 judge on a tiny repo: ~3.5 min, evidence-cited per-criterion PASS
  (deepseek-v4-flash). MCP `guard_run`/`judge_evaluate` accept a `workdir`
  param for cross-repo use — verified against an external repo.

## Config reference (quick)

```yaml
guards: { secrets: true, lint: true, tests: true, test_mode: diff,
          test_command: "uv run pytest -x --tb=short", test_timeout: 900 }
evaluator: { max_iterations: 200, max_time: 45m, max_input_tokens: 10M,
             max_output_tokens: 1M, tool_call_weight: 0.1, fast_track: auto }
defaults: { model: deepseek-v4-flash }
```

## Verifying a run is healthy

```bash
PATH="$HOME/go/bin:$HOME/gitreins-poc/.venv/bin:$PATH" gitreins guard   # must PASS
gitreins task list                                                      # board state
gitreins report -n 3                                                    # recent verdicts
```

More detail: `docs/dogfood/2026-08-03-integration.md` (real-use report),
`docs/dogfood/2026-08-14-integration.md` (PyPI consumer path),
`docs/dogfood/2026-08-27-integration.md` (fresh 0.12.0 wheel path),
`docs/dogfood/2026-09-15-integration.md` (fresh-machine bunker leg + HEAD consumer leg),
`docs/dogfood/2026-09-16b-integration.md` (0.13.0 wheel-verification run),
`docs/dogfood/diagnostics.md` (build/error trail).

## Disposable verification — worktree (2026-09-15: verify claims in clean trees)

Throwaway checkouts under `.disposable/`, from HEAD (WORKTREE-006; on the PyPI
wheel since 0.13.0 — see pitfall 14):

```bash
gitreins worktree fresh --cmd ".venv/bin/python -m pytest tests/test_version.py -q" --json /tmp/wf.json
gitreins worktree repro  --cmd ".venv/bin/python -m pytest tests/test_config.py -q" -k 3 --concurrency 2 --json /tmp/wr.json
gitreins worktree dogfood --skip-judge --json /tmp/wd.json   # init+task+guard+judge flow in a throwaway tree
```

- `fresh` = one clean tree; `repro -k N` = N copies (catches flaky/order-dependent
  tests); `dogfood` = the harness's own self-test. Exit 0 pass, 1 command failure,
  2 infrastructure. JSON records match `docs/cli-reference.md` shapes.
- `worktree dogfood --skip-judge` passes even on pylsp-less machines (clean config
  → diff mode) — use it as the quick sanity check that dodges pitfall 15.

## QA run ledger — where QA verdicts land (0.13.0+)

Every QA run records its own outcome, so the verdict outlives the reaped tree
and the gitignored registry:

```bash
gitreins qa list --json          # newest runs: verdict, cells, exit code, commit
gitreins qa record --project <repo> --kind bunker --verdict PASS --exit-code 0 \
  --cell launch=OK --evidence /tmp/evidence.jsonl --note "fresh-system battery"
```

- `worktree fresh|repro|dogfood` append their own row (`kind` = fresh/repro/dogfood);
  `qa record` is for a run produced outside the harness — a fleet QA lane, a
  bunker battery, a manual audit.
- Rows carry the fleet QA-ledger keys (`ts`, `project`, `status`, `cells`,
  `findings`, `evidence`, `note`) plus harness extras (`kind`, `verdict`,
  `run_id`, `exit_code`, `commit`, `harness_version`, `detail`). The two schemas
  are merged on purpose: a consumer that already parses fleet QA ledgers reads a
  harness-written one with no translation layer.
- Location: `GITREINS_QA_LEDGER` (a file, or a directory) > `qa_ledger.path` in
  `.gitreins/config.yaml` > `<repo>/.gitreins/qa-ledger.jsonl`.
  `qa_ledger.enabled: false` stops recording — announced on stderr, exit 1, never
  a failure of the run it records.
- `gitreins report` prints a QA block after the task verdict history.
- **ALWAYS pass `--verdict` (and `--exit-code`).** Omitting both writes
  `"status":"unknown","verdict":"UNKNOWN"` with exit 0 (measured 2026-09-20b on
  HEAD and on the 0.14.0 wheel), although `docs/cli-reference.md` says they
  default to "a passing verdict". An UNKNOWN row renders with neither a tick nor
  a cross, so it is indistinguishable from an undecided run — POC-29.
- `--evidence <path>` is stored verbatim **without checking the path exists**
  (exit 0, dangling pointer). Verify the path yourself before recording — POC-29.
- `max_entries` (default 1000) keeps the newest rows, and the eviction is silent:
  at a full ledger, the next `qa record` still prints "recorded" and exits 0 while
  the count stays put — POC-32. Raise the cap before a long battery.
- `gitreins install` does **not** add `.gitreins/qa-ledger.jsonl` to the consumer's
  `.gitignore` on the **0.14.0 wheel** (only this repo's own .gitignore has it), so a
  `git add -A` commit will sweep fleet QA rows — agent ids, server names, evidence
  paths — into user history. Add the ignore yourself — POC-31. Fixed at HEAD
  (2026-09-21): the installer's `GITREINS_GITIGNORE_ENTRIES` template carries the
  entry, so a wheel built from HEAD needs no manual line.

## Driving the MCP server as a real client (2026-09-20 dogfood run — verified at HEAD 0.14.0)

No SDK needed. `gitreins mcp-server` speaks line-delimited JSON-RPC 2.0 on stdio:

```python
p = subprocess.Popen(["gitreins", "mcp-server"], cwd=repo,
                     stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                     stderr=subprocess.PIPE, text=True, bufsize=1)
# handshake: initialize -> notifications/initialized -> tools/list
# tool call: {"jsonrpc":"2.0","id":N,"method":"tools/call",
#             "params":{"name":"task.create","arguments":{...}}}
# result payload = json.loads(resp["result"]["content"][0]["text"])
```

Client-side rules the hard way:

- **Tool errors live INSIDE the result text** as `{"error": "..."}` — only unknown tools
  (-32601) and handler crashes (-32000) are JSON-RPC errors. Always check the parsed payload
  for an `error` key.
- **Poll `judge.status` until `status` is `"complete"` or `"error"`** — there is no
  `running` boolean anywhere in the payload (POC-24). "Poll until running == false" loops
  forever. The background judge takes minutes (deepseek-v4-flash: ~7 min for a 6-test task).
- **The background judge survives your process.** Jobs persist to
  `~/.local/share/gitreins/jobs/` with their own workdir+pid; the next `judge.status` from
  any server instance resumes an orphaned job. A per-tool-call server pattern works.
- **MCP-judged verdicts are NOT browsable** (POC-23, open at 0.14.0): the async path never
  writes `.gitreins/history/`, so `gitreins serve`/`report` show nothing for MCP-driven
  runs. The full verdict lives in `~/.local/share/gitreins/jobs/job-<id>.json`. Read it
  from there until POC-23 lands.
- The 12 tool names/schemas in `docs/mcp-api.md` match the wire exactly (verified
  tool-by-tool). That doc is the contract; trust it over any summary.
- The `commit` tool runs Tier 1 in-process and returns the guard output inside the tool
  result — and it refuses while any task is in_progress. Land work: complete/delete tasks
  first, then commit.

## Pitfalls 14–17 (2026-09-15 dogfood run)

14. **(Updated 2026-09-18) PyPI wheel vs HEAD — 0.14.0 IS the wheel to trust now.**
    0.14.0 (2026-09-18) carries everything 0.13.0 shipped (worktree subcommand,
    POC-3/D init persistence, POC-16 multi-finding secrets, DF-011 hook pin, POC-10
    exit codes, correct --version) plus the fixes the 0.13.0 wheel still lacked: the
    runnable `python -m gitreins` form the installed hook pins (a 0.13.0 wheel dies
    with `No module named gitreins.__main__` and blocks every commit), a truncated
    `.gitreins/tasks.yaml` reported and preserved instead of loading as a silent
    partial list, zero-work guard skips reported as a DEGRADED PASS instead of a
    vacuous green, and an MCP `initialize` that reports the installed release and
    negotiates a protocol revision.
    Older 0.12.x wheels lack all of these (POC-7). Still missing from ANY wheel AND
    HEAD: the judge tier1 tests/lint gap (pitfall 20).
15. **`task complete` fails in fresh consumer envs: the pylsp test trap (POC-6).**
    If `.gitreins/config.yaml` is dirty (init just edited it), the tier1 tests step
    runs the FULL suite; `tests/test_lsp.py` FAILS (not skips) where pylsp is not
    installed, so the whole task FAILS although criteria and judge PASS.
    Workarounds: `uv pip install python-lsp-server` in the env that runs
    `task complete`, commit/revert the config before completing, use a clean
    checkout (diff mode), or `--skip-tier2` when Tier 1-only is intended.
16. **Tier1 failure evidence in `task complete` is 500 head-chars (POC-8)** —
    pytest prints failing test names at the END, so the stored output never names
    the failure. Re-run the suite yourself (`pytest -n 4 --maxfail=1 -q`) or call
    MCP `guard_run` (full logs) to see what actually failed.
17. **Fresh minimal machines can't follow the README quickstart.** No pip/pipx/
    sudo and `python3 -m venv` broken (no ensurepip) on bare Debian 13.
    Working no-root path: `curl -LsSf https://astral.sh/uv/install.sh | sh && uv tool
    install gitreins` (5 s; hook pin + secrets multi-finding fixes verified on
    that wheel).

## Pitfalls 18–20 (2026-09-16b qa-lane run — verified on the 0.13.0 wheel + HEAD)

18. **Fresh Debian 13 machines: the README's literal `pip install gitreins` hits the
    PEP-668 wall** (externally-managed environment; pip 25+). Working quickstart:
    `python3 -m venv ~/.venvs/gr && ~/.venvs/gr/bin/pip install gitreins` → working
    CLI in ~13 s. Also budget for `pip install pytest` in that same venv — the tests
    guard shells out to `pytest` and fresh venvs don't have it (`✗ tests — pytest:
    not found`). The wheel is py3-none-any; version self-report is correct on 0.13.0.
19. **A green `✓ secrets` badge does not name its scanner — coverage differs by
    machine (POC-15).** gitleaks' official github-pat rule requires the exact 40-char
    token shape (36-char suffix); the built-in regex fallback is looser in some spots
    and stricter in others. A shape-strict `ghp_` token FAILs guard only where
    gitleaks is installed/on PATH. When auditing, check which scanner actually ran
    (the fallback warning prints only when gitleaks is MISSING). And when building
    canary fixtures: generate exact-shape tokens programmatically — a hand-typed
    33-char `ghp_...` correctly passes both scanners and will fabricate a P0.
20. **Judge tier1 is secrets-only — it does not run tests or lint (POC-12, still
    open).** `task complete` / `gitreins judge` PASS a tree with staged failing tests
    (verdict.json `stages.tier1.steps == ['secrets']`). The 09-15 "fixed at HEAD"
    note was wrong — the scratch repo had untracked secrets, and the judge scans the
    whole worktree (guard scans staged scope), so the secrets step failed and masked
    the gap. Rule: judge = criteria + secrets; gate merges on guard (hook/CI), never
    on judge exit code alone.

## commit-msg audit — OFF until you declare a stage (2026-09-20b dogfood run)

`gitreins install` does **not** install a commit-msg hook (docs/cli-reference.md says
so; verified on a fresh box — only `pre-commit` exists). You create it:

```bash
cat > .git/hooks/commit-msg <<'HOOK'
#!/usr/bin/env bash
exec gitreins commit-audit
HOOK
chmod +x .git/hooks/commit-msg
```

**That hook only runs the audit when a stage is declared and armed for
`commit-msg`.** `gitreins install` + `init` write no such stage, so
`commit-audit "any message"` prints the named skip line and exits 0 — no audit:

```
commit audit: no pipeline stage with type commit_audit for trigger commit-msg — audit NOT run
```

Declare the stage, and put `mode` where you mean it:

```yaml
pipeline:
  stages:
    - id: commit_audit
      type: commit_audit
      on: [commit-msg]
      mode: block            # stage level — most specific, wins over everything

defaults:                    # wins only when the stage sets no `mode`
  commit_audit:
    mode: warn

commit_audit:                # legacy placement — still honored, lowest precedence
  mode: block
```

- `mode` precedence (highest first): `pipeline.stages[].mode` →
  `defaults.commit_audit.mode` → top-level `commit_audit.mode` → `warn`.
  All three placements are live; before DF-GITREINS-POC-30 only the top-level
  key was read (`_load_commit_audit_config` returned `cfg.get("commit_audit", {})`),
  so a stage-level `mode: block` stayed "(Warning only — commit will proceed)"
  with exit 0 and `defaults.commit_audit.mode` was dead config entirely.
- Only `block` exits 1 ("(Commit BLOCKED — fix message or set
  commit_audit.mode=warn)"); `warn`/`suggest` report and exit 0. A value outside
  `warn`/`block`/`suggest` is ignored at that level and resolution continues.
- Once reachable the audit is genuinely good: it cites the staged diff by name
  ("the diff adds a new file f.md … the message does not mention adding
  documentation") rather than emitting generic style advice.
- It needs an LLM credential and skips on a `gitreins.skip-tier2` trailer.

## Disposable batteries need `.coding-hermes/board/` (2026-09-20b)

`worktree fresh|repro|dogfood` raise

```
WorktreeResolutionError: canonical board directory does not exist:
  <repo>/.coding-hermes/board; create .coding-hermes/board in the main checkout
```

with a raw traceback and exit **1** (the doc page reserves exit 2 for infrastructure
failures) unless that directory exists — it is the Hermes fleet scheduler's board
layout, not anything `gitreins install` creates, and no user-facing doc lists it as
a prerequisite. `mkdir -p .coding-hermes/board` and the same command passes
(`fresh: exit 0 in 0.191s`) and self-records in the QA ledger — POC-27.

## Pitfalls 21–25 (2026-09-20b run — QA ledger / commit audit / fresh machine)

21. **The documented `qa record` default is not what runs (POC-29).** Neither
    `--verdict` nor `--exit-code` → `status: unknown`, `verdict: UNKNOWN`, exit 0.
    Always pass both. `--evidence` is also accepted for a path that does not exist,
    so the audit trail can point nowhere; validate it yourself.
22. **Ledger rotation is silent (POC-32).** At `max_entries` the next record exits 0
    and the oldest row is dropped with no message. With the default 1000 that is
    minor; for a shared fleet ledger (`GITREINS_QA_LEDGER` → one path, many
    projects — the configuration the docs recommend) it means anyone's next write
    can truncate history invisibly.
23. **The QA ledger is not gitignored in consumer repos (POC-31).** `install` covers
    `tasks.yaml`, `config.yaml.bak`, `usage.jsonl`, `logs/` — not
    `qa-ledger.jsonl`. A `git add -A` commit lands the rows (agent, server,
    findings, evidence paths) in user history. Add `.gitreins/qa-ledger.jsonl` to
    `.gitignore` in every repo that records QA. Fixed at HEAD (2026-09-21): the
    installer's template now carries the entry.
24. **A fresh-machine venv must be ACTIVATED before guard/commit (POC-28).** On bare
    Debian: PEP-668 blocks the README's `pip install`; the venv path works (32 s);
    but `gitreins guard` from the unactivated venv — even with pytest installed
    *into that venv* — fails `✗ tests (full) — /bin/sh: 1: pytest: not found`
    (the guard shells out via `sh -c` with the ambient PATH), and the README's
    documented first commit is **blocked**. `source .venv/bin/activate` first, then
    `Tier 1: DEGRADED PASS`, `✓ tests`, exit 0, commit lands. Same class as the
    uv-runner fallback the 0.14.0 notes fixed — it just does not cover the
    interpreter that launched gitreins.
25. **Before filing "command X is broken", check whether it needs a declared stage
    or config placement.** Three of this run's four surfaces were *inert*, not
    broken, and the difference is one YAML block. Read
    `engine/pipeline.py:253` (stages) and `_load_commit_audit_config` (top-level
    `commit_audit`) before concluding a capability is missing — and prefer
    `gitreins <cmd> --help` plus a controlled re-run over a confident bug report.

## The resolution gate — resolve / preflight / context.resolve (2026-09-23 dogfood run 7 — verified at 0.15.0)

The v0.15.0 flagship answers "does the repo already answer this?" (the Jev resolution
gate, JEVRES-001..006, spec docs/jev-resolution-gate.md).

**Pitfall 26 — every surface ships OFF, and nothing but this skill says so (POC-35).**
`surface_enabled()` (engine/resolution.py:238-260) requires an explicit block in
`.gitreins/config.yaml`; absent/empty/wrong-typed all mean disabled, and the first
flagship call dies in ~0.1s with `abstain_reason: surface-disabled`. The error's hint
points at a nonexistent "docs/jev-resolution-gate.md §9". The fix that works:

```yaml
resolution:
  enabled:
    cli: true
    mcp: true
    predispatch: true
```

**Pitfall 27 — the gate keeps no record of what it told you (POC-36).** Verdicts are
never persisted: no .gitreins/history entry, no usage.jsonl line, nothing in
`gitreins report`/`serve`. If you need the verdict later, capture `--json` output
yourself. The records themselves are excellent (cost_usd, tokens, manifest, model
build, attempts) — they just evaporate.
*(Stale as of the POC-36 fix: a real band IS filed in `.gitreins/history` with
`source: cli|mcp|predispatch` plus one `step: "resolution"` usage row, so `report`
and `serve` show it. An ABSTAIN still writes nothing.)*

**Pitfall 28 — two JSON shapes for one gate (FIXED in POC-37).** In 0.15.0
`resolve --json` → the verdict object (verdict/probability/manifest/…), while
`preflight --json` → a dispatch RECORD {band, decision, probability, missing_kind,
question, abstain_reason, verdict_json} where `verdict_json` was an embedded JSON
STRING you had to parse twice; scripts keyed on `verdict` found nothing in a
preflight record and had to fall back to `band`. Since DF-GITREINS-POC-37 the
record is {band, decision, probability, missing_kind, question, reason,
abstain_reason, **verdict**}, with `verdict` the SAME object `resolve --json`
prints (`ResolutionVerdict.to_dict()`) — one shape, one parse on both surfaces.

Verified behavior worth trusting (measured live, 0.15.0): real discrimination on real
premises — true premise → RESOLVED 0.85 / skip-dispatch; open premise → UNRESOLVED
0.29 / dispatch; nonsense question → 0.05; `--budget 2000` enforced (1963 est tokens,
1-file manifest) and the verdict IMPROVED to 0.92 (precision beats volume); truncation
always disclosed (`clipped` + `chars_dropped`); resolve fail-CLOSED (dead transport →
ABSTAIN exit 1, named reason+action — rehearse with
`HTTPS_PROXY=http://127.0.0.1:9`, NOT by touching real keys); preflight fail-OPEN
(ABSTAIN → exit 0, decision dispatch, reason carried); MCP `context.resolve` live on
the 13-tool surface (client notes in the MCP section — remember: the initialized
notification gets NO response). Exit codes exactly as documented. Cost ≈ $0.0005/call;
~2.3s warm (hyperfine 10 runs). Doc shapes: mcp-api.md §13 documents the tool; the CLI
sections are §15 (resolve) / §16 (preflight).

## The security-scan guard — the Antares CVE scanner (2026-09-23b run 8 — verified at 0.15.0)

`gitreins security-scan` localizes known CVEs against staged Python (opt-in).
Heuristic mode (default; no ML deps) matches keyword lines ("CVE", "injection",
"exploit", "unsafe", "deserialization", "hardcoded", "vulnerability" —
`engine/antares.py:48`) and emits `CVE-SIMULATED conf=0.00` findings; ML mode
(`--force-ml`) needs huggingface_hub AND transformers and runs Antares-1b locally.

**Pitfall 29 — the README's config home is DEAD for the guard (POC-38).**
The README/cli-reference/onboarding block puts `security_scan:` under
`defaults:` — that is where the manual CLI reads it (`cli.py:2941`). The
commit-gate guard reads **`guards.security_scan.enabled`**
(`guard_manager.py:950`). Following the README verbatim → `Tier 1 Guards:
PASS` with NO `security_scan` line — the guard you enabled never runs, and
nothing tells you. The working shape (verified locally + on a fresh bunker
box; proven both shapes side-by-side):

```yaml
defaults:
  security_scan:        # feeds ONLY `gitreins security-scan`
    enabled: true
guards:
  security_scan:        # feeds the `gitreins guard` gate — the documented one is dead
    enabled: true
  secrets: true
  lint: false
  tests: false
```

Verify enablement by the LANE LINE (`✓/✗ security_scan ...`), never by the
PASS header — a silently-absent lane looks like success.

**Pitfall 30 — `min_confidence` is a no-op on findings (POC-39).** It filters
only the CVE advisory FEED (`cve_feed.py:221`). Heuristic findings are
hard-coded conf 0.0 (`antares.py:258`) and the guard fails on ANY finding
(`guard_manager.py:2416`) — so the word "injection" in a COMMENT fails a real
commit at the documented `min_confidence: 0.7`. Until POC-39 closes, keep the
keyword vocabulary out of comments, or treat the lane as a canary not a filter.
It starts meaning something only with the ML stack installed (real inference
carries model confidences).

**Pitfall 31 — ML-mode dep hint is half-written (POC-40).** `--force-ml`'s
error names only `huggingface_hub` (the DOWNLOAD dep); inference additionally
needs `transformers`. Install both up front: `pip install huggingface_hub
transformers`. The guard-path config keys `model:`/`cve_source:` are read by
nothing on the guard path (scanner constructed bare, `use_ml=False`
hard-coded, `guard_manager.py:2395`) — ML on the GUARD path is not reachable
via config at all today; `--force-ml` on the CLI is the only way in.

Measured trust (0.15.0, fresh repo): exit codes 0/1/2 exactly as the README
table promises (verify with `${PIPESTATUS[0]}` — `$?` after a pipe lies);
text and json findings agree line-for-line; 0.096s warm per staged scan, no
model download, no network in heuristic mode; gitleaks-absent degradation
names the fallback and keeps the secrets lane honest. Fresh-box install: 20s
pip venv install → guard reproduces the finding end-to-end on bare Debian.

## The Go guard lane (2026-09-23c run 9 — verified at 0.15.0, HEAD beda743)

`gitreins init` on a repo with `go.mod` detects Go reliably and writes the lane's
defaults (Python lanes correctly off):

```yaml
guards:
  secrets: true
  lint: false        # Python lint — no-op on a Go repo
  tests: false       # Python tests — no-op
  test_mode: full
  go:
    build: true      # go build ./...
    lint: true       # golangci-lint, falling back to go vet (see pitfall 30)
    tests: true      # go test -count=1 -short ./...
  allow_skips: true
```

What actually works: a **staged** uncompilable `.go` file FAILs the run (all
three lanes), exit 1, and the pre-commit hook refuses the commit with the real
compiler text; `guards.go.lint: false` / `guards.go.tests: false` really do drop
those lanes; a no-scope run is honest in the log (`No Go files staged`) and does
not crash. Whole-run cost on a small repo: ~0.8s warm (nothing worth optimizing).

**Pitfall 29 — a lane's SCOPE must be the scope it graded (POC-42, fixed).**
The Go lanes originally graded the INDEX: `_changed_go_files`
(`engine/guards.py`) fell back to `git diff --cached` whenever the caller passed
no scope, and `GuardManager._scope_files_or_none()` passed `None` for every
scope except `working-tree`. With an empty index all three lanes returned
`passed=True, output="No Go files staged"` **before running any tool** — in
`--full`, and in a bare `gitreins guard` — so a Go tree that does not compile
printed `Tier 1 Guards: PASS` / `✓ go_build — ok` and exited 0. Fixed:
`GuardManager._go_scope_files_or_none()` now resolves the scope — an explicit
`--scope working-tree` hands over the collected set, a non-empty index of `.go`
files keeps the lanes' own index discovery (byte for byte, what the pre-commit
hook grades), and otherwise `--full` hands them the whole-tree listing
(`_tree_go_files`, the Go twin of `_tree_python_files`). The general lesson
outlives the fix: a lane whose scope was empty must SAY so — `No Go files
staged` (the index was the scope) vs `No Go files in scope` (a working-tree or
whole-tree scope held none) — and that is now a SKIP (`~ go_build — skipped
(...)`), never `✓ ... ok`. `gitreins guard --scope working-tree` remains the
escape hatch when the files you care about are neither staged nor committed.
(The judge's Tier-1 `tests` step sees the file that the guard missed — guard and
judge disagree on the same tree, POC-12/POC-16's class.)

**Pitfall 30 — `✓ go_lint — ok` may mean "golangci-lint found things and
`go vet` disagreed" (POC-43).** `engine/guards.py:111-147` treats **any**
non-zero golangci-lint exit as "linter unavailable" and falls through to
`go vet ./...`, whose verdict becomes the lane's verdict. A non-zero exit caused
by real findings is indistinguishable from a missing binary, so an
errcheck-class error (`os.Mkdir` ignored — compiles, vets clean, golangci-lint
exit 1) reports `✓ go_lint — ok`. The run log names the fallback
(`output: go vet: clean`) but the console does not. Read `go_lint`'s detail line
in `.gitreins/logs/guard-*.log` before trusting it; the lane as shipped is a
`go vet` lane, and `go_build` already covers what that catches. Bonus sharpener:
`--new-from-rev=HEAD~1` cannot resolve in a single-commit repo
(`fatal: bad revision 'HEAD~1'`), which disables golangci-lint's diff processor
(it does not by itself change the exit code).

**Pitfall 31 — the DEGRADED-PASS machinery now sees the Go lane names (POC-44,
fixed for the Go lanes).** `_SUBSTANTIVE_STEPS = {"lint", "tests", "lsp"}`
(`engine/types.py`) keys the net on the PYTHON lane names; the Go lanes are
`go_lint`/`go_tests`/`go_build`, so a Go run in which a lane did no work was
**not** flagged, never printed `Tier 1: DEGRADED PASS`, and exited 0 even with
`allow_skips: false` — combined with pitfall 29, `Tier 1 Guards: PASS (test
mode: full, whole tree)` on a Go repo carried no evidence.
`_SUBSTANTIVE_STEP_ALIASES` + `_is_substantive_step()` now extend the
substantive-id mapping to the Go lane names (minimal surgery — `Tier1Result` is
not restructured), so the three lanes ride the same TRUST-001 machinery:
`Tier 1: DEGRADED PASS (skips: go_build=No Go files staged, ...)`, `~` markers
per lane, and exit 2 unless `guards.allow_skips: true`. The guard log's
`guards: N (0 failed, N skipped)` plus the per-lane `[SKIP]` entries and their
`skip_reason` remain the honest read of which lanes graded nothing.

**Pitfall 32 — `guards.test_command` is not the Go test command (POC-45).**
`init` prints `Test cmd: go test -short -count=1 ./...` but writes no
`test_command` key for a Go repo, and the Go lane hard-codes its argv
(`guards.py:168-173`) — nothing on the Go path reads `guards.test_command`. To
change how Go tests run you must change the lane's argv (a code change), not the
config.

**Pitfall 33 — no Go toolchain reads as broken code (POC-46).** With no `go` on
`PATH`, staged `.go` files FAIL all three lanes; the console shows a bare
`✗ go_build` / `✗ go_lint` / `✗ go_tests` and only the run log carries the cause
(`error: [Errno 2] No such file or directory: 'go'`). On a fresh machine, read
the log before believing the code is at fault.


## The parallel worktree fleet (2026-09-24 run 10 — verified at HEAD 412067d, 0.15.0)

`gitreins worktree fleet lanes.json [--merge]` runs manifest lanes in
branch-backed worktrees. The guard engine inside it is solid; the merge path
on a STOCK install cannot succeed. Rows POC-47..53.

**Pitfall 34 — commit the harness config BEFORE the first fleet run (POC-47/53).**
`init` leaves `.gitreins/config.yaml` untracked; worktrees branch from HEAD, so
lane guards die with "no .gitreins/config.yaml — run `gitreins init` first"
(a hint that cannot fix it inside the tree). Commit config + `.gitleaks.toml`
first, and commit the manifest itself too — the merge gate counts it.

**Pitfall 35 — the harness's own runtime files jam the merge gate (POC-47).**
The gate is `git status --porcelain -uall` minus a hardcoded ignore list
(worktrees.json/lock, board events, history//logs/). NOT exempted and written
by every fleet run: disposable.json, disposable.lock, tasks.yaml.lock; inside
the worktree also the `.venv` symlink + `uv.lock` (created by the guard's
`uv run pytest`). The installer's gitignore template misses all of them.
Fix as a user: gitignore `.gitreins/worktrees.*`, `.gitreins/disposable.*`,
`.gitreins/tasks.yaml.lock`, `.venv/`, and commit your manifest.

**Pitfall 36 — judge-gated --merge is effectively unreachable (POC-48/49).**
The gate needs a persisted PASS verdict for the exact lane commit. The
README's example judge phase (`gitreins judge <id>`) fails in-tree
("Task not found" — tasks.yaml is gitignored); `judge --ephemeral` persists
nothing so it can never satisfy the gate; and even with the working pattern
(lane command runs `gitreins task create <id> ...` in-tree, judge phase runs
`gitreins judge <id>`) the judge's Tier 1 grades pytest exit-5 as a hard FAIL
(pipeline.py:663 exit-code-only; the guard's benign exit-5 PASS at
guard_manager.py:2081 is not consulted) — a repo without tests cannot get a
PASS verdict while its hook passes. Also: judge-failed lanes report
`error: null` with the refusal reason dropped — read `stages[].output`.

**Pitfall 37 — failed lanes are a tarpit (POC-50).** `worktree clean` keeps
failed lanes forever (exit 0, "Nothing to reap"); --confirm-stale-orphan does
not reap them either; the reconcile error hint names plain `clean`; and a
fleet re-run REUSES the failed lane's stale-HEAD tree. Recovery:
`git worktree remove --force` each tree + `git branch -D gitreins/task/<id>`
+ `gitreins worktree clean --confirm-stale-orphan`. Make lane commands
idempotent: `git add -A && (git diff --cached --quiet || git commit -m ...)`.

**Pitfall 38 — fresh box, no pytest: the hook blocks commit #1 (POC-51).**
On a bare machine, `pip install gitreins` is PEP-668 blocked (use
`python3 -m venv`), and the pre-commit hook FAILs the first commit with
`pytest: not found` (exit 127 = hard FAIL, not the benign exit-5 skip).
Install pytest before your first commit, or expect the block. venv install
measured 17 s on las-bunker-03 (Debian 13, Python 3.13).

**Pitfall 39 — verdict history fails to commit in fleet repos (POC-52).**
The history branch ref `refs/heads/gitreins` collides with the fleet's own
`gitreins/task/<id>` branches (ref-lock prefix conflict): "Verdict saved to
disk but not committed (git unavailable)". Non-fatal; verdict.json is still
on disk.
