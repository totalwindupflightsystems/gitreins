---
name: gitreins-agent-operations
description: >-
  How an AI agent OPERATES GitReins: the task lifecycle done right (criteria
  that mean something, start, complete, the async judge), the 13-tool MCP
  surface with each tool's real parameter shape and the calls that refuse while
  a task is in_progress, the pre-commit hook contract, the evidence contract v1
  (--json shape, redaction, the 32 KiB bound), worker-brief conventions
  including the test-count sync criterion, and the worktree-fleet operations
  contract. Load this before creating tasks, driving the MCP server, or
  committing in a gitreins-managed repo — it is the agent-facing contract, not
  the pitfall catalogue (that is `gitreins-usage`).
version: 1.0.0
category: software-development
---

# GitReins — Agent Operations

> **Reading the ids.** References like `DF-010`, `GR-GAP-055`, `POC-12` or `INT-CI-8` are rows on this project's own
> internal work board. They are kept so a claim can be traced to the incident that produced it;
> nothing in this skill requires knowing what they contain.

GitReins is a git-native quality harness. An agent operating it has three
surfaces: the **task store** (what you promise to deliver), the **MCP server**
(how a tool-using agent drives it), and the **commit gate** (what actually
lands). This skill is the contract for all three, plus the evidence document an
agent emits and the repository conventions that keep the docs/CI gates green.

Verified against `gitreins` 0.15.0, HEAD `abc7f7c`. Anything not proven against
that source is labelled **(unverified)**.

## 0. Minimum discipline: an agent committing in a gitreins repo

1. `gitreins task create <id> "<title>" "<criterion>" ...` **before** you start
   editing — a criterion written afterwards is a description, not a gate.
2. `gitreins task start <id>`; do the work.
3. `gitreins guard` and fix every BLOCKING lane; re-run until it passes.
4. `gitreins task complete <id>` (or `gitreins judge <id>`) and read the
   per-criterion verdict — not the one-word summary.
5. Commit. Never `--no-verify` for code; the hook is the same gate CI runs.
6. Never commit `.gitreins/tasks.yaml` (per-checkout state) or verdict/QA
   runtime artifacts — `gitreins install` gitignores them, and a `git add -A`
   in a repo whose ignore list is short will sweep them into history.

If you only remember one rule: **the judge is not the gate.** Judge Tier 1 runs
its `secrets` step only (`docs/evidence-contract-v1.md:45` for what the judge
does *not* write; the tier-coverage gap is tracked as POC-12 and still open at
0.15.0 — labelled **(unverified) at this HEAD**, it was measured on
0.14.0/0.13.0). Gate merges and commits on `guard` (hook/CI), never on a judge
exit code alone.

## 1. Task lifecycle

### What a task is

A task lives in `<workdir>/.gitreins/tasks.yaml` — **per-checkout, gitignored,
never committed** (`docs/worktree-fleet-quickstart.md:33`). Fields:
`status` (`pending | in_progress | complete`), `criteria`, `depends_on`,
timestamps (`engine/task_manager.py:59-62`). The store is written under a lock
and re-read before each mutation, so a task another process added is visible
(`engine/task_manager.py:248-283`).

### Criteria that mean something

A criterion is graded by an evaluator that reads files and runs commands. It
cannot grade a mood.

| Bad | Good |
|---|---|
| "auth works" | "`POST /login` returns 401 with a JSON `error` field on bad credentials" |
| "tests pass" | "`go test -count=1 -short ./pkg/auth` exits 0" |
| "docs updated" | "`README.md` and `CONTRIBUTING.md` state the live collection total (`N tests`)" |
| "handles errors" | "a missing `config.yaml` raises `ConfigError`, not `KeyError`" |

Rules that survive review:

- **One behaviour per criterion.** Three narrow criteria out-grade one compound one.
- **Name the exact path and extension.** The evaluator searches what you name;
  a criterion about generated `*.code.ts` output that says only "assembled
  files" searches `*.spec.ts` and returns a false FAIL.
- **Avoid substrings that occur in the target string** (a criterion matching
  `foo` against a file whose content includes `foobar` passes for the wrong
  reason). Grep the criterion yourself before trusting it.
- **Layer them**: happy path → edge case → failure mode → invariant.
- Cap a reasonable task at ~6 criteria; the evaluator verifies roughly that many
  in one budget on a mid-size repo. Group by spec rather than one task per
  acceptance criterion.
- **Encode a fix as a criterion** so a regression cannot silently return.

### start → complete: what `complete` actually asserts

`TaskManager.complete(id, force=False)` checks `depends_on`, then flips status
and stamps `completed_at` (`engine/task_manager.py:263-283`). That is *all* it
asserts — completion is an assertion of **status**, and the evaluation is
triggered separately. With unmet dependencies it raises `DependencyError`; the
MCP path calls `tm.complete(id)` with no `force`, so through MCP a blocked
dependency surfaces as an unhandled handler exception (JSON-RPC `-32000`), not a
clean domain error (`gitreins_mcp/server.py:494-496`).

### Dependencies

`--depends-on` is **CLI-only**: the MCP `task.create` schema has no
`depends_on` property (`gitreins_mcp/server.py:176-198`), so an agent working
through MCP cannot declare a dependency chain. Ids passed to `--depends-on` are
not validated at create time, so a chain can point at tasks that never exist.

### The async judge — the only correct poll loop

`judge.evaluate` is async by default (`wait=false`) and returns a `job_id`
immediately; `task.complete` with an LLM key dispatches the same kind of job
(`docs/mcp-api.md:216-247`, `gitreins_mcp/server.py:494-517`). Jobs are
disk-backed under `~/.local/share/gitreins/jobs/` (override `GITREINS_JOB_DIR`)
and survive server restarts; an orphaned `running` job is auto-resumed on the
next poll — so a per-tool-call server pattern works.

Key the loop on the **terminal set of `status`**, never on a bare boolean:

```python
def poll_until_terminal(status_fn, job_id, timeout_s=1800):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        p = status_fn(job_id)
        if p.get("status") in {"complete", "error"} and not p.get("running", False):
            return p                      # complete -> result; error -> error
        time.sleep(5)
    raise TimeoutError(job_id)
```

`"running"` is additive metadata; `status in {"complete","error"}` is the
termination signal guaranteed on every build (`docs/mcp-api.md:260-289`).
Do **not** write `while payload["running"]:` — builds older than that field
never send it.

Two consequences for how you brief work:

- MCP clients cap a tool call around 300 s while a real evaluation runs for
  minutes. Never pass `wait=true` on a repo with a large suite; dispatch async
  and poll, or run the judge from the CLI in the background.
- Jobs run **one at a time per server instance** — concurrent judges contend on
  ports/tmp and on the shared history store (`docs/mcp-api.md:366-368`).
  Parallelise by running several CLI judges, not by firing several MCP judges.

## 2. The MCP surface as an agent drives it

### Connect

`gitreins mcp-server` speaks line-delimited JSON-RPC 2.0 on stdio:
`initialize` → `notifications/initialized` → `tools/list` → `tools/call`
(`docs/mcp-api.md:10-56`). No SDK is required, and you can read the server's
identity without opening the transport:

```bash
python -m gitreins_mcp.server --version     # -> gitreins MCP server 0.15.0
```

Verified live at HEAD: `.venv/bin/python -m gitreins_mcp.server --version` →
`gitreins MCP server 0.15.0`.

Client rules that cost debugging time otherwise:

- **One response per line, in order.** Read exactly one line per request.
- **`notifications/*` never answer.** `notifications/initialized` returns
  nothing; a client that waits for it hangs. Send it only after reading the
  `initialize` result, and read `result.protocolVersion` rather than assuming
  the revision you asked for.
- **stdout is protocol-only.** Startup/shutdown acknowledgement, warnings and
  revision notes go to **stderr**; merging the streams makes you parse prose as
  JSON.
- **Tool errors live INSIDE the result text** as `{"error": "..."}`. Only an
  unknown method/tool (`-32601`) or a handler crash (`-32000`) is a JSON-RPC
  error (`docs/mcp-api.md:373-395`). Always check the parsed payload for an
  `error` key before branching on success.
- The result payload for a tool call is
  `json.loads(resp["result"]["content"][0]["text"])`.

Run it as a real client before trusting any of it — this is the cheapest
verification an agent can do:

```bash
cd /path/to/repo
{ echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-25","capabilities":{},"clientInfo":{"name":"agent","version":"1"}}}';
  echo '{"jsonrpc":"2.0","method":"notifications/initialized"}';
  echo '{"jsonrpc":"2.0","id":2,"method":"tools/list"}';
  echo '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"task.list","arguments":{}}}';
} | python -m gitreins_mcp.server
```

### The 13-tool surface (verified against `gitreins_mcp/server.py:130-143` and `docs/mcp-api.md:115-348`)

The registry is `self._tools` (`server.py:130`); `tools/list` returns these 13
schemas. Names are dotted on the wire.

| Tool | Parameters (real shape) | Returns / notes |
|---|---|---|
| `configure` | `env` (object), `model`, `base_url`, `provider` (`openai\|anthropic`) — all optional (`server.py:147-175`) | Hot-reloads LLM config: `{configured, previous, current, note}`. Use instead of restarting to set `GITREINS_LLM_API_KEY`. |
| `task.create` | `id`, `title`, `criteria` (array\<string\>, required); `workdir` optional | The task dict. **No `depends_on`.** |
| `task.start` | `id` required; `workdir` optional | Updated task dict. |
| `task.complete` | `id` required; `workdir` optional | With LLM: `{task, job_id, status:"running", note}` → poll `judge.status`. Without: `{task, note:"LLM not configured — skipping evaluation"}`. |
| `task.list` | `status` (`pending\|in_progress\|complete`), `workdir` — optional | `{tasks: [...]}`. **Filter by status, don't count rows by hand.** |
| `task.get` | `id` required; `workdir` optional | The task dict; not found → `{error: "Task not found: <id>"}`. |
| `task.delete` | `id` required; `workdir` optional | `{deleted: "<id>"}` or `{error: ...}`. |
| `commit` | `message` required (**no `workdir`**) | Runs Tier 1 first; `{committed, output}`. Refuses on guards failure or any in-progress task. |
| `guard.run` | `workdir` optional, `dead_code` (bool, default false) | `{passed, workdir, results:[{name, passed, output}]}` — output truncated to 500 chars per guard. Refuses when the target repo has no `.gitreins/config.yaml` (`server.py:576-580`). |
| `judge.evaluate` | `id` required; `workdir`, `wait` (default false), `max_iterations`, `max_time` (`"5m"`), `max_input_tokens` (`"200k"`), `max_output_tokens` (`"50k"`), `tool_call_weight`, `eval_cap` (`"100/30m/200k/50k"`) | Async: `{job_id, status:"running", task_id, workdir}`. Sync (`wait=true`): the full result dict. |
| `judge.status` | `job_id` required | `{status: running\|complete\|error, running: bool, result?/error?}`. |
| `propagate` | `targets` (array\<string\>, required); `source` optional | `{source, results}` — pushes guard config to sibling repos. |
| `context.resolve` | `question` required; `budget` (int, default the engine's 28k-token ceiling) | The full resolution verdict object. **Off by default** — needs `resolution.enabled.mcp: true` in the config, else ABSTAIN / `surface-disabled`. |

`context.resolve` is the context-saving primitive: ask the repo instead of
reading it. Bands are decided in code, not by the model: **RESOLVED** (≥ 0.85),
**REVIEW** (0.50–0.85), **UNRESOLVED** (< 0.50, with `missing_kind` naming what
is absent), **ABSTAIN** (any failure — fail-closed, with a named
`abstain_reason`). The verdict carries the bundle manifest, token counts and
cost, so an answer with no traceable evidence is never returned
(`docs/mcp-api.md:304-348`).

### Calls that refuse while a task is `in_progress`

- **`commit` refuses.** The check runs *before* guards
  (`server.py:536-551`): `{"error": "Tasks still in progress: <ids> — ...",
  "tasks": [...]}`. The rationale is in the message itself — `task.complete`
  judges the committed state, so committing mid-task would grade a tree the
  judge never saw. Complete (`task.complete`) or delete (`task.delete`), then
  retry.
- **`guard.run` refuses** on a repo with no `.gitreins/config.yaml`, rather than
  printing a vacuous green from built-in defaults (`server.py:576-580`).
- The **pre-commit hook does not have the in_progress rule**, and neither does
  `gitreins commit` — the two commit doors disagree (CLI `commit` runs the same
  guards and commits with zero mention of an open task). If you script agents on
  the CLI, the judge-skip protection is MCP-only today.
- `gitreins install` installs **only** `pre-commit`. There is no `commit-msg`
  hook, so commit-message auditing runs only if you create the hook *and*
  declare a `commit_audit` stage armed for `commit-msg` — otherwise it prints a
  named skip line and exits 0 (inert, not broken).

### Cross-repo semantics

Every repo-touching tool accepts an absolute `workdir` and defaults to the
server's workdir; tasks come from `<workdir>/.gitreins/tasks.yaml`, guard config
from `<workdir>/.gitreins/config.yaml`. **`commit` is the exception — it takes
no `workdir`** and always commits the server's repo (`docs/mcp-api.md:198-204`).
An agent driving several repos from one server can grade cross-repo but cannot
commit cross-repo: start one server per repo you intend to commit in.

## 3. The pre-commit hook contract

- **What runs.** `gitreins guard` (Tier 1) on the staged scope. The template is
  `PRE_COMMIT_HOOK` in `gitreins/cli.py:137-157`; it exits 0 immediately if
  `$REPO_ROOT/.gitreins/config.yaml` is absent, then `cd`s to the repo root and
  runs the pinned command.
- **How it is pinned.** At install time `_render_pre_commit_hook()` replaces
  `__GITREINS_CMD__` with the absolute path of the binary (or
  `python -m gitreins` for the interpreter) that ran the install
  (`gitreins/cli.py:189-199`). This kills a real failure mode: a bare
  `gitreins` resolves through `PATH` at commit time and can silently run a
  *different, older* version that skips guards. If no invocation is resolvable
  at install time the hook says so in a comment and falls back to `PATH`
  lookup — a loud-later, quiet-now degradation. If the venv later moves, the
  hook fails with `command not found`: confusing but loud, which is the right
  direction. `install` writes and chmods the hook
  (`gitreins/cli.py:411-421`).
- **Why `--no-verify` is a last resort.** The hook runs the same guard the job's
  CI runs (`gitreins guard`), so skipping it does not skip the check — it moves
  the failure to CI, after the commit exists. Reserve `--no-verify` for
  docs-only changes and harness self-upgrades, and say so when you do.
- **Never trust a green badge to name its scanner.** A `✓ secrets` line does not
  tell you whether gitleaks or the built-in regex fallback ran, and their
  coverage differs by machine. When `guard` is unexpectedly clean, read the
  guard log to see which scanner actually executed.

## 4. Evidence contract v1 — `--json`

The stable automation surface is three commands, each emitting exactly one
UTF-8 JSON document to stdout (`docs/evidence-contract-v1.md:17-27`):

```bash
gitreins guard  --scope working-tree --json
gitreins judge  rorca-run-42-US-001 --ephemeral --title "Story gate" \
                --criterion "Acceptance criteria are satisfied" \
                --scope working-tree --json
gitreins report -n 20 --json
```

All three flags are live at HEAD (verified via `gitreins guard --help` and
`gitreins judge --help`, 0.15.0).

**Shape.** The normative schema is `schemas/evidence-v1.schema.json`, identified
by `https://gitreins.dev/schemas/evidence/v1.json` with
`schemaVersion: "1.0"`. Required top-level keys: `$schema`, `schemaVersion`,
`producer` (`{name: "gitreins", version}`), `command` (`guard|judge|report`),
`generatedAt`, `scope` (`staged|working-tree|history`), `outcome`
(`pass|fail|error|unknown`), `passed` (boolean or null), `summary` (≤ 2048
chars), `checks` (≤ 32 items of `{id, outcome, passed, summary}`) and
`metadata`.

**Redaction rules.** Every string crosses a secret-redaction boundary before
serialization. `redact_text()` replaces secret-shaped spans *then* caps, so a
secret straddling the cap cannot leak; `redact_document()` is the final
boundary and never touches keys (`engine/evidence.py:395-445`).
`metadata.redacted` is the const `true` — always; `metadata.redactionsApplied`
says whether a replacement actually happened; `metadata.truncated` says a cap
bit. A consumer that sees `truncated: true` is looking at a partial document and
must not treat absence of a check as its passing.

**Size bound.** `MAX_EVIDENCE_BYTES = 32 * 1024` (`engine/evidence.py:365`).
Text and collections are capped first; `metadata.truncated` reports it.

**Who consumes it.** `tests/test_evidence_contract.py` — it validates each
command's output against the schema, holds it under the 32 KiB cap, checks the
redaction flags, and asserts the `--scope working-tree` collection is read-only
(the index is byte-identical after a run). `scripts/check_cli_examples.py`
replays the documented invocations through the real parser on every run. So a
change to the emitted document breaks a test, not a downstream reader's day.

**Exit codes are the API.** `guard` and `judge` exit 0 only for a passing
result, 1 for a non-passing result, 2 for CLI usage errors
(`docs/evidence-contract-v1.md:33`). Never "normalise" a non-zero command exit
into a harness error, and never read exit 1 as breakage.

**Ephemeral judge** (`judge --ephemeral`) builds an in-memory task from
`--title` and repeatable `--criterion` and persists *nothing*: no
`TaskManager`, no `.gitreins/tasks.yaml`, no history entry, no stash, no usage
line. It exits 0/1/2 like the sync command and carries
`"ephemeral": true` in `subject` with `metadata.historyPersisted: false`. The
one opt-in exception is `--persist-verdict`, which writes the single merge-gate
document `.gitreins/verdicts/verdict.json` inside the graded tree so a
judge-gated `worktree merge` can find a verdict for that exact
worktree/branch/commit (`docs/evidence-contract-v1.md:43-47`). Use this for
per-story gates in a tree you do not want to mutate.

## 5. Worker / agent brief conventions

A brief that will be executed by another agent is a contract. The conventions
that keep this repository's gates green:

- **Number the acceptance criteria** and make each one independently checkable.
  A criterion the worker cannot verify locally is a criterion the reviewer will
  reject.
- **Name the files in scope, and the files out of scope.** A diff that touches
  something the brief did not name is a review failure even when the change is
  good.
- **If the diff adds, removes, or renames a test file, the brief MUST carry this
  acceptance criterion verbatim:**

  > update test-count sites (README.md x2, CONTRIBUTING.md x2) to the new
  > collection total

  `scripts/check_docs_drift.py` is the single implementation: it runs the live
  collection (`pytest --collect-only -q --override-ini=addopts=`) and fails when
  any `N tests pass` / `N tests across` / `N test files` claim in `README.md`
  **or** `CONTRIBUTING.md` disagrees with it, plus a version check against
  `pyproject.toml` and an evaluator-tool-count check against
  `engine/evaluator.py` (`scripts/check_docs_drift.py:1-40`). It is chained into
  the local guard's `test_command`, so a drifted claim FAILS the commit. Three
  CI reds came from briefs that added tests without this criterion.
- **Require the exact verification command and its observed output** in the
  worker's report — "tests pass" is not evidence; `pytest -q` → `2488 passed` is.
- **Require a stated residual**: what was not verified, and why. An honest gap
  costs one review round; a false "done" costs a rework cycle.
- **Ask for the guard result, not just the diff.** "`gitreins guard` exit 0,
  all lanes graded" is the claim the reviewer checks first.

## 6. Worktree-fleet operations contract

`gitreins worktree fleet lanes.json [--merge]` runs manifest lanes in
branch-backed worktrees. The agent-facing rules (all from
`docs/worktree-fleet-quickstart.md`):

**Commit the harness config before the first fleet run.**
`gitreins init` leaves `.gitreins/config.yaml` untracked; a lane tree is a fresh
checkout of the repo's *committed* files, so every lane guard/judge dies with
`no .gitreins/config.yaml — run 'gitreins init' first` — a hint that cannot fix
it from inside the tree (`docs/worktree-fleet-quickstart.md:19-43`). Commit
`.gitreins/config.yaml` (required), `.gitignore` and `.gitleaks.toml` (part of
the gate). Never commit `.gitreins/tasks.yaml`, `worktrees.json`, `usage.jsonl`,
`qa-ledger.jsonl`, `logs/`, `verdicts/`. Commit the manifest too, or keep it
outside the checkout — an untracked manifest is dirt and the merge gate refuses
a dirty canonical main.

**Lanes create their own tasks.** The task store is per-checkout, so a fresh
lane tree starts with an EMPTY store. The fleet seeds each lane's task (title +
criteria) from canonical main just before the judge phase — which only works if
the task was created in canonical main *before* the fleet run. So: create the
task in the canonical checkout, then run the fleet. A lane whose command
creates its own task in-tree is the pattern that satisfies the gate; the
README's example `gitreins judge <id>` fails in-tree with `Task not found`
because `tasks.yaml` is gitignored.

**Why the merge gate needs a persisted verdict.** The gate refuses
`source_head == branch_point` ("task branch has no commits beyond its
registered branch point"), then requires a verdict stamped for the lane's
**exact** worktree/branch/commit (`engine/worktree_manager.py:930-963`):

1. both trees clean (runtime artifacts exempt);
2. a verdict exists for the lane's exact branch tip — a verdict that graded a
   different commit is not a verdict;
3. that verdict is a PASS (a FAIL holds the tree and branch in place);
4. its Tier 1 carries **no skipped steps** — `_tier1_skipped_steps()`
   (`engine/worktree_manager.py:209-224`) reads
   `stages.tier1.{degraded,skipped_steps}`, because "a PASS whose gates never
   ran cannot merge";
5. the branch is fast-forwardable, or gets rebased — and after a rebase the
   guard **and** judge re-run, because the graded commit changed.

The verdict can come from the shared history (`refs/gitreins/history`) or from
the opt-in disk document `.gitreins/verdicts/verdict.json` written by
`judge --ephemeral --persist-verdict` (`engine/worktree_manager.py:61-72`,
:312-352). A lane that persists nothing can never satisfy the gate — hence
"persisted verdict" is a hard requirement, not a nicety.

**Lane commands must commit and be idempotent.** The merge gate refuses a dirty
tree on both sides, so the lane command must `git add` **and** `git commit` its
work in its own tree. And because the same manifest gets re-run (after a refused
merge, a failed sibling lane, a scheduler retry), use:

```bash
git add -A && (git diff --cached --quiet || git commit -m "<task>: <what>")
```

A plain `git commit` fails the second run with `nothing to commit, working tree
clean` and reports a failed phase even though the work merged.

**Read the tick report, not the exit code.** `worktree fleet` exits 0 even when
lanes fail; the JSON report on stdout is the source of truth — `state: "merged"`,
`merge_errors`, `merge_order`, and each lane's `stages[].exit_code`/`output`.
A judge-failed lane can report `error: null` with the refusal reason only in
`stages[].output`.

**Recovery.** A failed lane's tree sits at the failed run's HEAD and is never
silently reused: reap with `gitreins worktree clean`, and if a lane died before
committing, `git branch -D gitreins/task/<id>` before re-running. To merge an
already-judged lane without re-running the work, fix the dirt in canonical main
and run `gitreins worktree merge <id>`.

**Disposable verification** for "is this claim true in a clean tree?":
`gitreins worktree fresh --cmd "<cmd>"`, `worktree repro -k N`, and
`worktree dogfood --skip-judge`. Exit 0 pass / 1 command failure / 2
infrastructure — the exit contract is the API; never normalise a child's
non-zero exit into 2. `--keep`/`--keep-failures` trees are **evidence of an exit
code, not a reproduction environment**: children inherit the parent session's
`PATH`, so re-running a failed command inside a kept tree can resolve a
different interpreter and pass. The batteries also need `.coding-hermes/board/`
to exist in the main checkout — create it with `mkdir -p .coding-hermes/board`
or the command raises a raw traceback and exits 1.

## 7. Conventions that keep the docs and CI gates green

The repository gates its own documentation. If your change alters a surface a
doc claims, the doc is part of the change:

| Gate | Command | Enforces |
|---|---|---|
| Docs drift (version) | `python scripts/check_docs_drift.py` | README release banner == `pyproject.toml` version |
| Docs drift (counts) | same script | every `N tests pass` / `N tests across` / `N test files` claim in README.md **and** CONTRIBUTING.md == the live collection |
| Docs drift (tool counts) | same script | every evaluator "N tools" claim in `docs/architecture.md` / `docs/evaluator-loop.md` == `len(EVALUATOR_TOOLS)` in `engine/evaluator.py` |
| CLI examples parse | `python scripts/check_cli_examples.py` | documented invocations go through the real parser |
| CLI doc sync | `python scripts/check_cli_doc_sync.py` | `docs/cli-reference.md` subcommands == the live CLI (a table can be wrong while its examples parse) |
| Board id hygiene | `python scripts/check_board_ids.py .coding-hermes/board` | unique, sequential board ids |
| Formatting | `git ls-files -z '*.py' '*.pyi' \| xargs -0 ruff format --check --force-exclude` | `ruff format --check` (exits 1 on drift; `--diff` and a bare `ruff format` both exit 0 — never substitute them) |
| Guards | `gitreins guard` | Tier 1 on the staged scope |

All of these run in CI (`.github/workflows/ci.yml:73-96`). The one-line rule an
agent should carry: **run `python scripts/check_docs_drift.py` before pushing
test-touching work**, and when you add a subcommand or flag, update
`docs/cli-reference.md` in the same commit — `check_cli_doc_sync.py` compares
the doc's claims against the live parser, so a stale table reds the build with
no code bug at all.

## 8. Verification checklist for an agent

- [ ] Task created **before** the work, criteria specific, one behaviour each,
      exact paths/extensions named.
- [ ] `task list` filtered by status, not finger-counted.
- [ ] Judge polled on the terminal set `{complete, error}` — never on a bare
      running flag.
- [ ] Every grading claim comes from `guard` or a named command's real output,
      not from a judge verdict alone.
- [ ] No task left `in_progress` at commit time (MCP `commit` will refuse; and
      a leftover in-progress task blocks every subsequent MCP commit).
- [ ] `.gitreins/tasks.yaml`, QA ledger and verdict artifacts not staged.
- [ ] Test-touching diff → count sites re-synced, `check_docs_drift.py` exit 0.
- [ ] `--no-verify` not used for code (and if used at all, stated explicitly).
- [ ] Residuals written down: what was not verified, and why.

## Related

- `skills/gitreins-usage/SKILL.md` — the pitfall catalogue: known defects,
  scanner coverage holes, ledger quirks, per-release drift. Load it when
  something `guard`/`judge` does contradicts this contract.
- `docs/mcp-api.md` — the MCP contract; trust it over any summary of it.
- `docs/evidence-contract-v1.md` + `schemas/evidence-v1.schema.json` — the
  automation surface.
- `docs/worktree-fleet-quickstart.md` — the fleet walkthrough this section
  compresses.
- `AGENTS.md` — this repo's operator block: mandatory guard, worker-brief
  test-count criterion, and the destructive-command prohibitions.
