---
name: gitreins-troubleshooting
description: >-
  How to track down a GitReins failure instead of guessing: the diagnostic
  ladder from a failing signal to a root cause, what every exit code actually
  means (content failure vs infrastructure vs skip vs degraded pass), where
  each artifact lives (verdict.json, the history ref, guard logs, the QA
  ledger, job dirs) and how to read it, how to re-run one lane or one test
  instead of the whole battery, how to separate a load/timing flake from a
  real regression with the control-worktree method, what a tier-1 divergence
  (judge FAIL, tier-2 all-PASS) means, why CI goes red on a docs-only commit,
  and how to recover a corrupt or truncated state file. Load this when a
  guard, judge, commit, worktree, or CI step reported a failure you cannot yet
  explain.
version: 1.0.0
category: software-development
---

# GitReins Troubleshooting — From a Failing Signal to a Root Cause

> **Reading the ids.** References like `DF-010`, `GR-GAP-055`, `POC-12` or `INT-CI-8` are rows on this project's own
> internal work board. They are kept so a claim can be traced to the incident that produced it;
> nothing in this skill requires knowing what they contain.

This skill is the diagnostic counterpart to `gitreins-usage`. Usage tells you how
to run the harness; this tells you what to do when a signal goes red and you need
the cause, not a workaround.

The single most expensive mistake with GitReins is **reading the number instead
of the artifact**. A pytest exit code does not say *why* a run ended
(`engine/types.py:206-230`) and a console summary is deliberately truncated
(`gitreins/cli.py:2137-2141`). Every section below is shaped as
**symptom → what it means → how to confirm → fix.**

Never run `gitreins guard` to "see what happens" when you are already inside a
slow battery — read the artifact first. The log and the verdict were written to
be read after the fact.

---

## 0. The diagnostic ladder — walk it in this order

Stop at the first step whose answer you already have. Do not skip to step 5
because step 1 looked familiar.

1. **Identify *which* signal this is.** Guard (Tier 1, commit gate), judge
   (Tier 1+2, task verdict), `worktree fresh|repro|dogfood` (disposable
   battery), a QA-lane row, or CI. They have different exit contracts and
   different artifacts. Confusing a judge FAIL for a guard FAIL sends you
   hunting in the wrong store.
2. **Read the exit code with the right table.** Section 1. Exit 2 is not
   "worse than 1" — on the guard it means *a gate never ran*, and on a script
   step it can mean *maxfail stopped a real failure*.
3. **Open the full artifact, not the summary.** Section 2 maps signal → path.
   The console prints the *first* line of a bounded tail; the persisted log and
   `verdict.json` hold the whole thing.
4. **Establish the scope that was graded.** A lane that graded an empty scope
   must never be read as a lane that graded your work. Section 3.
5. **Classify: content vs infrastructure vs flake vs divergence.** Sections 4-6.
   This is the fork that decides whether you fix code, fix the environment,
   file a harness bug, or do nothing.
6. **Prove the failing thing is the code you think you pushed.** Section 9.
   Before any of the above, if HEAD, the index, or the installed binary might
   not be what you assume, go there first — everything else is wasted motion.

```bash
# cheap first reads — none of these run the battery
git -C <repo> rev-parse HEAD && git -C <repo> status --porcelain -uall | head
ls -t <repo>/.gitreins/logs/ | head -3
git -C <repo> log --oneline -1 <the-commit-the-verdict-graded>
git -C <repo> show <verdict-ref>:<path>          # see section 2 for the ref
```

---

## 1. Exit-code semantics — decode before you debug

There is no single table; each surface has its own. Read the one that matches
the signal you saw.

### `gitreins guard` (and the pre-commit hook that runs it)

| Code | Meaning | Source |
|---|---|---|
| 0 | Graded pass — every enabled lane ran and passed | `gitreins/cli.py:2130-2152` |
| 1 | A gate **failed** on content (tests failed, secrets found, etc.) | `gitreins/cli.py:2151-2154` |
| 2 | **DEGRADED PASS** with `guards.allow_skips: false` — a substantive lane (lint/tests/lsp) did no work | `gitreins/cli.py:2156-2168` |

Exit 2 is deliberately **not** 1: "a gate failed" and "a gate never ran" are
different facts (`gitreins/cli.py:2157-2158`). The string
`Tier 1 Guards: PASS` is printed **only** on a fully graded run; a degraded run
prints `Tier 1: DEGRADED PASS (skips: ...)` instead (`gitreins/cli.py:2126-2134`).
So *grepping for `Tier 1 Guards: PASS` is proof the gates ran*: if you cannot
find that exact string, treat the run as ungraded.

- **Confirm:** `gitreins guard --json` emits one document whose
  `metadata.degraded` and per-step skip fields name what did not run. This path
  exits **0 for a degraded pass** by contract (`gitreins/cli.py:2094-2096`) —
  the *document* carries the skip, the *code* does not. If you grade CI on the
  exit code of `--json`, you will read a degraded run as green.
- **Fix:** stage the files you want graded (`git add`), or set
  `guards.allow_skips: true` to explicitly accept zero-work runs
  (`gitreins/cli.py:2164-2166`).

### `DEGRADED PASS` vs `PASS` vs skipped vs N/A

This is the distinction that quietly invalidates audit reports. Learn it once:

| Console | `passed` | `skipped` | What it is evidence of |
|---|---|---|---|
| `✓ <lane> — ok` | true | false | the tool ran and graded a non-empty scope |
| `~ <lane> — skipped (reason)` | true | true | **nothing was graded** — not evidence of anything |
| `Tier 1: DEGRADED PASS` | true | ≥1 | some substantive lane did no work; the run is not proof |
| `N/A` / `n/a` | — | — | the step did not apply; never counted as pass or fail |

The substantive lanes are `lint`, `tests`, `lsp`
(`_SUBSTANTIVE_STEPS`, `engine/types.py:44`), plus the Go aliases
`go_lint`/`go_tests`/`go_build` (`_SUBSTANTIVE_STEP_ALIASES`,
`engine/types.py:53`; the predicate is `_is_substantive_step`,
`engine/types.py:56-58`).
A repo that lints nothing because `guards.lint: false`, or tests nothing
because diff mode found no mapping, prints a **green header over an ungraded
tree**. Section 3 covers how to catch that.

**Rule:** a degraded pass must never be promoted to a graded pass in a report,
a board row, or a release gate. If your evidence is a degraded run, write
"skipped" — there is no path from "the lane did not run" to "the tree passes."

### pytest exit codes, and why 5 and 127 are read wrong

Raw pytest codes are *four* different facts wearing one integer. GitReins
classifies them from the captured output, not the number
(`engine/types.py:263-330`, kinds listed at `engine/types.py:250-261`):

| pytest exit | `pytest_outcome()` kind | Honest read |
|---|---|---|
| 0 | `passed` | passed |
| 1 | `failed` | real test failures |
| 2 | `maxfail` | **real failures** — `-x` (+xdist) raises `Interrupted` and pytest maps that to 2 |
| 2 | `interrupted` | signalled from outside (KeyboardInterrupt banner, no failing test) |
| 2 | `interrupted-unclassified` | truncated capture — report unknown, never a code defect |
| 3 | `internal-error` | pytest's own crash — infrastructure |
| 4 | `usage-error` | bad invocation — infrastructure/config |
| 5 | `no-tests-collected` | **nothing was graded** — benign in the guard, fatal in the judge |
| 127 | (shell) | **tool missing** — infrastructure, but surfaced as a failed lane |

Two traps live here:

- **Exit 2 is not automatically an interruption.** `-x` plus `-n` makes one
  real failing test exit 2 with a `xdist.dsession.Interrupted: stopping after N
  failures` marker. Reading the bare 2 as "the harness got signalled" is how a
  genuine regression gets filed as a flake. Confirm with the marker or the
  `FAILED <id>` line, both of which `pytest_outcome` writes into
  `StepResult.data` (`engine/pipeline.py:715`).
- **Exit 127 means a tool is missing, not that your code is broken.** The guard
  has a dedicated hint for this (`engine/guard_manager.py:608-622`,
  GR-GAP-064) and still marks the lane failed. Confirm the tool exists on the
  PATH the guard shells out to — the guard runs its command through `sh -c`
  with the **ambient** PATH, so a venv that is not activated is invisible even
  if pytest is installed into it. Fix: activate the venv (or pin the
  interpreter in `test_command`), then re-run.

**The exit-5 asymmetry is real and must be handled, not assumed away.** The
guard treats pytest exit 5 as a **benign skip**
(`no_tests_benign = return_code == 5 and _pytest_no_tests_benign(output)`,
`engine/guard_manager.py:2313`), but the judge's Tier-1 script step grades on
the code alone — `passed = exit_code == 0` (`engine/pipeline.py:700-704`), with
the comment that a non-zero exit is a hard failure regardless of `on_fail`.
Consequence: **a repo with no collectible tests can pass its hook and fail its
judge on the same tree.** If you see a judge Tier-1 `tests` FAIL with a pytest
banner saying no tests ran, that is this class — not your code.

### Script steps in the pipeline (judge Tier 1)

`engine/pipeline.py:699-704`: `passed = exit_code == 0`. Any non-zero exit is a
hard failure and `on_fail` cannot convert a failed lint/test into a pass. Two
exceptions the record carries:

- **Timeout** returns `error="Command timed out after Ns (step budget)"` with
  `data.timed_out: true` (`engine/pipeline.py:686-698`). That is a **budget
  exhaustion, not a code finding** — the gate did not finish. Do not file it as
  a defect; raise the step budget or narrow the scope.
- **Refusal** by command hygiene returns `passed=False` with the refusal reason
  (`engine/pipeline.py:679-680`). A refused command is an infrastructure
  condition, not a failing test.

### Disposable battery (`worktree fresh|repro|dogfood`) and QA-lane rows

`0` pass, `1` the child `--cmd` failed, `2` harness infrastructure failure. The
child's exit code propagates **unchanged**; never normalize a nonzero child
exit to 2 (`docs/cli-reference.md`, `worktree` section). An argparse-level
malformed invocation is also 2 (`gitreins/cli.py:2185-2193`) — same code as a
harness failure, so read the message, not the number.

---

## 2. Where every artifact lives, and how to read it

### Verdicts — `.gitreins/history/<date>/<hash>/`

```
.gitreins/history/2026-09-27/6c31e36d/
├── verdict.json     the machine record: stages, steps, exit codes, per-criterion items
├── summary.md       the human summary the judge wrote
├── commit.patch     the diff the judged commit contained
└── worktree.patch   the working-tree diff at judge time
```

- **The directory hash is NOT the commit sha and NOT the task id.** It is
  `sha256(f"{task_id}:{evaluated_at}")[:8]` (`engine/persist.py:233-237`). You
  cannot derive it — list the date directory, or read it out of `report --json`.
- **`verdict.json` is the authority for a judge run.** Shape (verified on a
  live file):

  ```
  .task_id  .task_title  .task_criteria[]  .passed  .evaluated_at  .summary
  .stages.tier1.passed / .any_failed / .summary
  .stages.tier1.steps[].{id,type,passed,output,error,data.exit_code}
  .stages.tier2.passed / .any_failed / .summary
  .stages.tier2.steps[].data.verdict / .data.items[].{criterion,status,detail}
  ```

  Read `.stages.tier1.steps[].data.exit_code` and the step's `output` *before*
  believing `.passed` — the boolean is downstream of them.

  ```bash
  # the failing tier-1 step and its real output, without a re-run
  python3 -c "import json;d=json.load(open(p));s=d['stages']['tier1']['steps'];[print(x['id'],x['passed'],x.get('data',{}).get('exit_code'),x['output'][-800:]) for x in s if not x['passed']]"
  ```

- **The verdict ref, and the legacy branch.** Verdicts are committed to
  `refs/gitreins/history` (`engine/persist.py:149`). A repo written before that
  change also has them on the legacy `refs/heads/gitreins` branch
  (`engine/persist.py:159`); the reader **unions both**, plus their
  remote-tracking copies (`refs/remotes/origin/gitreins/history`,
  `refs/remotes/origin/gitreins`, `engine/persist.py:170-171,303`). Read either
  with:

  ```bash
  git show refs/gitreins/history:.gitreins/history/2026-09-27/6c31e36d/verdict.json
  git show gitreins/history:<path>          # shorthand, same ref
  ```

  A fresh `git clone` does **not** fetch `refs/gitreins/history` — a clone with
  no verdict history is expected, not a corruption. Create the local branch
  from the remote-tracking copy if one exists, or query a checkout that has it.

- **`gitreins report` reads the refs, not your working tree.** On a checkout
  with neither ref readable it prints "No verdict history found" while the
  verdicts exist on the origin. `report --json` gives the parsed entry list, and
  the serve API's `/api/verdicts/<date>/<hash>` serves the record correctly —
  prefer those when a detail pane looks blank.

### Guard run logs — `.gitreins/logs/guard-<UTC-timestamp>.log`

Written on **both** pass and fail; the console prints the path as
`guard log: <path>` (`gitreins/cli.py:2137-2141`). This is the untruncated
record. Its header is a compact diagnostic block (verified shape):

```
run_utc / workdir / test_mode / test_targets
overall: PASS (DEGRADED — skipped checks)
guards: 4 (0 failed, 3 skipped)
diagnostics:
  first_failing_test: none detected
  secrets_scanners: clean (gitleaks + builtin cross-check)
skipped_steps:
  - lint: no staged files
  - tests: no test files match the changed sources (diff mode)
```

Then one block per lane: `[PASS]/[FAIL]/[SKIP] <name> passed=<bool> exit_code=<n>
skip_reason=<...>` followed by `--- output (untruncated) ---`.

```bash
grep -n "diagnostics:\|^  [a-z_]*:\|overall:\|guards:\|skip_reason=\|\[FAIL\]" \
  "$(ls -t .gitreins/logs/*.log | head -1)"
```

`first_failing_test` is the fastest route from "tests lane failed" to the one
test to re-run. `secrets_scanners` names which scanner actually ran — the
gitleaks-vs-builtin coverage differs by machine, so "clean" is only as strong
as the scanner named there.

### QA ledger — `.gitreins/qa-ledger.jsonl`

Resolution order: `GITREINS_QA_LEDGER` (file or directory) >
`qa_ledger.path` in `.gitreins/config.yaml` > `<repo>/.gitreins/qa-ledger.jsonl`.
Read it with `gitreins qa list -n N --json`. Worth knowing when a verdict "went
missing": `worktree fresh|repro|dogfood` append their own row, and their
verdict outlives the reaped tree. Caveats: a row is `unknown`/`UNKNOWN` unless
`--verdict`/`--exit-code` were passed; `max_entries` (default 1000) evicts the
oldest row **silently**; and the project field is the directory name at record
time, so a renamed repo carries two spellings in history. Never treat a ledger
row as a graded signal — it is a record *about* a run.

### Async judge jobs — `~/.local/share/gitreins/jobs/job-<hex>.json`

Override with `GITREINS_JOB_DIR` (`engine/job_store.py:42`). Jobs **survive the
process that started them** and are shared globally, so any later
`judge.status` call resumes an orphaned job (`engine/job_store.py:4-12`). When a
judge was dispatched through the MCP/async path, the full verdict lives **here**
— that path does not write `.gitreins/history/`, so `report`/`serve` show
nothing for it. Read the job JSON directly.

### Other state you may need mid-diagnosis

| Path | Holds | Note |
|---|---|---|
| `.gitreins/tasks.yaml` | task board (local, gitignored) | a truncation here is recoverable — section 7 |
| `.gitreins/usage.jsonl` | per-step usage rows | one row per graded step |
| `.gitreins/worktrees.json` + `.lock` | disposable/fleet registry | lock files are written by every run |
| `.gitreins/disposable.json` + `.lock` | kept disposable trees | see the tree-scope caveat in section 3 |
| `.gitreins/config.yaml` | the config the **run** read | if it is dirty, diff mode may have widened to full |

---

## 3. Confirm the scope that was graded — the empty-scope trap

A lane that returned `passed=True` **before invoking any tool** is the single
most convincing false negative in the harness. The general shape: the lane
resolved an empty file set, so it had nothing to grade and reported success.

**Symptom:** `Tier 1 Guards: PASS` (or `✓ <lane> — ok`) on a tree you know does
not compile or does not pass.

**Confirm:** read the per-lane line, never the header. The honest vocabulary is:

- `No <lang> files staged` → the **index** was the scope and it was empty.
- `No <lang> files in scope` → a working-tree/whole-tree scope held none.
- Either way a zero-work lane must be a **SKIP** (`~`, `skip_reason=...`), never
  `✓ ... ok`.

Then check the guard log's `guards: N (M failed, K skipped)` and the
`skipped_steps:` list. If a substantive lane is in that list and you expected it
to grade your work, the run is ungraded no matter what the header said.

```bash
gitreins guard --scope working-tree          # escape hatch: grade files that are neither staged nor committed
git status --porcelain -uall                 # what the index actually holds
```

Related scope asymmetries to keep in mind: **the judge grades the whole
worktree, the guard grades the staged scope**, so untracked files can fail the
judge while the hook passes. And a **kept disposable tree is evidence of an exit
code, not a repro environment** — children inherit the parent session's PATH, so
re-running a failed command inside `--keep-failures` can resolve a different
interpreter and PASS where the run FAILED.

---

## 4. Flake vs regression — prove it with a control

"Run it again" is not a test. A re-run that passes tells you the failure was
not 100% deterministic; it does **not** tell you the code is fine.

### Triage matrix

| Evidence | Read |
|---|---|
| Fails at `-count=1` on the exact graded commit, in a clean tree | **regression** |
| Passes alone, fails in the full run | **order/coupling dependency** — still a real defect in the suite |
| Fails only under parallel load (`-n`), passes single-threaded | load/timing flake — confirm with the control below |
| Fails, and the trace is `Interrupted`/`KeyboardInterrupt` with no `FAILED` id | interrupted run — infrastructure |
| Exit 2 with a maxfail marker or a `FAILED` id | **real failure** — section 1 |
| Fails with `not found` / exit 127 in the output | tool/environment, not code |
| Fails with a pytest `INTERNALERROR` / exit 3/4 | harness/config |

### The control-worktree method

Prove *which* of two trees is at fault by varying one thing at a time, in a
tree nobody else touches:

```bash
# 1. control: the parent commit, same command, same host — isolates "my change"
git worktree add /tmp/ctrl-parent <parent-sha>
( cd /tmp/ctrl-parent && <the exact same test command with the same flags> )

# 2. treatment: current HEAD, clean tree, single-threaded
git -C . rev-parse HEAD
<the exact same test command> -p no:randomly -x     # kill order/parallelism

# 3. load control: same command, forced concurrency — if ONLY this fails, it is load
```

The rule: **if the parent commit fails the same way, your change is not the
cause.** If parent passes and HEAD fails single-threaded at `-count=1`, you have
a regression. If both pass and only the parallel/loaded run fails, it is a
timing or shared-resource defect — reproducible load must come from the
documented bounded harness, never an ad-hoc busy loop.

For repeated runs in fresh trees, use the built-in repro farm rather than a
shell loop — it isolates each copy and reports the rate as evidence:

```bash
gitreins worktree fresh --cmd "<exact command>" --json /tmp/wf.json
gitreins worktree repro --cmd "<exact command>" -k 10 --concurrency 3 --json /tmp/wr.json
```

### `-count=1` and cache discipline

A green run can be a cached run. For Go, `-count=1` defeats the test cache. For
pytest, re-check with a clean cache (`-p no:cacheprovider`) if the result
disagrees between runs. When you report "passes", say which flag produced it.

### What is *not* evidence

Parallel work being in flight, a dirty config, or a warm daemon are all real
causes of failure but **not** flake classifications — they are environment
state and must be named as such, not waved away as "probably flaky."

---

## 5. The tier-1 divergence class — judge FAIL, tier-2 all PASS

**Symptom:** the judge verdict is `passed: false`, but every criterion under
`.stages.tier2.steps[].data.items[]` is `status: PASS` with cited detail.

**What it means:** the failure is in **Tier 1**, not in the criteria. The
criteria were met by an agent that read the repo and verified each one; Tier-1
is a mechanical gate that ran on that same tree and tripped. The verdict is
still correctly `false` — the divergence is a signal *about which subsystem
failed*, not about whether the work is done.

**Confirm, in order:**

```bash
p=<verdict.json>
python3 -c "import json;d=json.load($p);print('t1',d['stages']['tier1']['passed'],'t2',d['stages']['tier2']['passed']);[print(s['id'],s['passed'],s.get('error',''),s['output'][-600:]) for s in d['stages']['tier1']['steps']]"
```

1. Read `.stages.tier1.steps[]` — one of them is `passed: false`.
2. Read that step's `data.exit_code` and classify it with section 1. The
   classic members of this class: pytest **exit 5** graded as a hard failure by
   the judge while the guard skips it benignly; a **step timeout**
   (`data.timed_out: true`); a **refused** command; and a **Tier-1 only runs
   the secrets step** verdict (`stages.tier1.steps == ['secrets']`), which
   means tests and lint were never part of the verdict at all.
3. Decide which subsystem owns the failure and fix *that*. Do not re-run the
   tier-2 criteria — they already passed. Do not "fix" the code to satisfy a
   Tier-1 environment gap.

**The rule that follows:** the judge is not a merge gate. Gate merges on the
guard (hook or CI), and treat a judge PASS as evidence about criteria, never as
proof the tree passes tests.

---

## 6. Why CI goes red on a docs-only commit

**Symptom:** a commit that changed only markdown (or a test file) turns CI red
on a *docs* step, while the code is untouched.

**What it means:** it is almost always the **docs-drift gate**. The single
implementation is `scripts/check_docs_drift.py`; it runs the live pytest
collection and fails when any `N tests pass` / `N tests across` / `N test files`
claim in `README.md` or `CONTRIBUTING.md` disagrees with the live count, and
when the README release-banner version disagrees with the version in
`pyproject.toml` (`scripts/check_docs_drift.py:2-13,433-515`).

It is chained into the **local** guard's `test_command`
(`.gitreins/config.yaml:19`: `.venv/bin/python scripts/check_docs_drift.py &&
.venv/bin/python -m pytest -x --tb=short`), so in a well-configured repo a
drifted claim fails the *commit* before it can reach CI. If it went red in CI
only, the local chain is missing or was bypassed.

**Confirm and fix:**

```bash
python scripts/check_docs_drift.py         # names the drifted claim and the live count
```

- **Added/removed/renamed test files → the counts must be updated.** Any worker
  brief whose diff touches test files must carry that as an explicit acceptance
  criterion, or it will land a red gate.
- **Version drift** → update the README release banner to the `pyproject.toml`
  version.
- The same file also backs two CI steps (`.github/workflows/ci.yml:82,84`), so
  a fix to the claim fixes the local gate and CI together.

CI runs sibling doc gates too: documented CLI examples must parse, and the CLI
reference must match the live parser (`.github/workflows/ci.yml:86-92`). A doc
edit that adds an example can fail those — read the step name, not just "CI
failed."

**A docs-only change is not a licence to `--no-verify`.** The exemption some
hooks make for docs is about the *code* lanes; it does not disable the
docs-drift gate.

---

## 7. Recovering a corrupt or truncated state file

**Symptom:** a restart prints a warning naming a state file (`tasks.yaml` or a
db) and the task list looks empty or partial; or an operation refuses with a
parse error.

**What GitReins does (verified by `tests/test_corrupted_state_restart.py`):**

- A read of an unreadable task store **must not crash** and **must not mutate**
  the corrupt bytes — it warns and exits cleanly
  (`tests/test_corrupted_state_restart.py:132-144`).
- The corrupt file is **preserved with a content-addressed copy** named
  `<basename>.corrupt-<something>` beside it, so repeated loads do not churn
  the sidecar and a second, different corruption gets its own copy
  (`tests/test_corrupted_state_restart.py:88,146-186`).
- The next write keeps the preserved bytes on disk **and** writes fresh, valid
  state — so you can keep working without losing the evidence
  (`tests/test_corrupted_state_restart.py:163-173`).
- A pre-corruption copy can be restored to recover the tasks
  (`tests/test_corrupted_state_restart.py:187-199`).

**Fix procedure:**

```bash
ls -la .gitreins/*.corrupt-* 2>/dev/null     # the preserved evidence, if any
# 1. back up whatever is on disk now
cp .gitreins/tasks.yaml /tmp/tasks.yaml.$(date +%s)
# 2. restore from a pre-corruption copy (backup, or git if it was tracked)
#    — never hand-edit the corrupt bytes, and never assume a partial list is complete
# 3. verify the store loads AND lists the tasks you expect
gitreins task list
```

**Do not** "repair" a truncated YAML by appending a closing brace and moving on:
the load may then succeed with a silent partial task list, which is worse than
the loud failure. If the store must be rebuilt, do it from a backup or from the
verdict history, and state in your report that it was rebuilt.

---

## 8. Fresh install vs repo HEAD — is the binary yours?

**Symptom:** a behaviour documented at HEAD does not reproduce, or reproduces
only sometimes; a hook blocks or lets through something it should not.

**What it means:** the thing that ran is a **different build** of GitReins than
the one you are reading.

**Confirm:**

```bash
which -a gitreins                 # PATH order — the hook runs the FIRST match
gitreins --version                # note: the version string has been wrong on some wheels
python -c "import gitreins,os;print(os.path.dirname(gitreins.__file__))"
head -3 .git/hooks/pre-commit     # does the hook pin an absolute path, or bare `gitreins`?
```

- A hook that calls **bare** `gitreins` runs whatever PATH resolves first — a
  different venv can silently substitute an older version at the commit gate.
  Prefer a hook that pins the installing binary's absolute path; if the venv
  moves, the hook then fails loudly with `command not found` rather than
  silently running the wrong build.
- **Version strings have disagreed with the packaged code** on released wheels,
  so `--version` is not a trust anchor. Compare the installed **file path** and,
  when in doubt, behaviour, not the number.
- A repo can be green at HEAD while the **published package** ships an older
  build for weeks. When dogfooding a consumer, verify against the *installed*
  package, not repo HEAD.

**Fix:** run the harness from the venv you intend
(`PATH="<repo>/.venv/bin:$PATH" gitreins ...`), or pin the interpreter in
`test_command` so the guard shells out to the right one.

---

## 9. "Is this even the code I pushed?"

Ask this **before** any deep diagnosis when the signal is surprising. It is
cheap and it invalidates everything downstream.

```bash
git rev-parse HEAD                              # what is checked out
git log --oneline -1 <sha-from-the-verdict>     # what the verdict graded
git status --porcelain -uall                    # what is dirty
git diff --cached --stat                        # what a guard would grade
git ls-remote origin <branch>                   # what the remote actually has
git rev-parse HEAD; git rev-parse @{u}          # local vs upstream tip
```

Check, in order:

1. **Does the verdict's commit equal HEAD?** `verdict.json`'s directory and the
   `commit.patch`/`worktree.patch` inside it are the tree that was judged. If
   they disagree with HEAD, the verdict is about a different tree.
2. **Is the change staged or only in the working tree?** The guard grades the
   index; the judge grades the worktree.
3. **Is the config dirty?** A dirty `.gitreins/config.yaml` widens diff-mode
   tests to the full suite, which changes what the tests lane does on the same
   code.
4. **Is the failing lane reading the files you think?** Check the guard log's
   `workdir:` line — it records the directory the run actually used.
5. **Did the push land?** A green local run and a red CI run usually means the
   remote does not have the commit you think it does.

---

## 10. Quick reference — signal to artifact to command

| Signal | Authority artifact | First command |
|---|---|---|
| `gitreins guard` red | `.gitreins/logs/guard-*.log` | `grep -n "overall:\|\[FAIL\]\|first_failing_test" $(ls -t .gitreins/logs/*.log\|head -1)` |
| Pre-commit hook red | same log + `guard log:` line | read the log, then `git status --porcelain -uall` |
| Judge FAIL | `.gitreins/history/<d>/<h>/verdict.json` | read `.stages.tier1.steps[]` first |
| Judge FAIL + tier-2 all PASS | same, plus `.stages.tier2.steps[].data.items[]` | section 5 |
| Async/MCP judge | `~/.local/share/gitreins/jobs/job-<id>.json` | read `.status`, then the verdict |
| Verdict not in `report` | `refs/gitreins/history` (+ legacy branch) | `git show refs/gitreins/history:<path>` |
| Disposable battery | `gitreins qa list -n 5 --json` | `gitreins worktree repro -k 10` for the rate |
| QA lane row unexpected | `.gitreins/qa-ledger.jsonl` | `gitreins qa list --json` |
| CI red on a docs-only commit | `scripts/check_docs_drift.py` | `python scripts/check_docs_drift.py` |
| Tests lane failed | guard log `first_failing_test` | re-run that one id, `-count=1` |
| Wrong/old behaviour | installed binary path | `which -a gitreins` / `head -3 .git/hooks/pre-commit` |
| Corrupt state | `.gitreins/<name>.corrupt-*` | section 7 |

**Re-run the smallest thing that can still fail.** One test id beats the suite;
one lane beats the battery; one clean tree beats your dirty checkout. The whole
point of the artifacts above is that you should rarely need to re-run anything
at all.

---

## Anti-patterns that wasted real debugging time

- **Reading the exit code without the output.** Exit 2 is four different
  stories; exit 5 is benign in one gate and fatal in another.
- **Promoting `DEGRADED PASS` to PASS** in a report or a board row.
- **Grepping the console summary for the failure.** It shows the first line of
  a bounded tail. The log has the whole thing.
- **Deriving the verdict directory from the task id.** It is a hash of task id
  **and** timestamp.
- **Re-running the full battery to "see if it's flaky."** Use the control
  worktree or the repro farm, and vary one thing at a time.
- **Filing a harness bug before checking whether a surface just needs config or
  a declared stage.** Several surfaces are *inert* until enabled; a capability
  that "does nothing" is frequently off, not broken.
- **Assuming a green badge names its scanner.** Coverage varies by what is
  installed; the log's `secrets_scanners` line says what actually ran.
