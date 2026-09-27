---
name: gitreins-known-failures
description: >-
  The catalogue of RECURRING GitReins failures — the big fights that keep
  coming back. Each entry is symptom → root cause → fix → how to confirm which
  one you are looking at, plus whether it is FIXED or STILL LIVE at the
  checkout you are on. Load this when a guard, judge, task store, verdict
  history, worktree or install behaves impossibly: a lane that reports findings
  it never scanned, a run that passes because a tool is absent, a lane that
  cannot find its own runner, a guard and a judge disagreeing on the same tree,
  a fresh clone that shows no verdicts, or a binary that reports the version
  you just replaced.
version: 1.0.0
category: software-development
---

# GitReins Known Failures — the recurring fights

Read `gitreins-usage` first: that skill tells you how the harness is *meant* to
work. This one tells you what goes wrong *repeatedly*, how to tell the failures
apart, and which ones still bite.

**Verification basis.** Every entry below was checked by reading HEAD `abc7f7c`
(`main`, version 0.15.0) — no test suite was run, no guard was executed, no
commit was made. A claim marked **FIXED** is fixed in the *code at that
commit*; it can still be live in an older wheel you have installed, so check
your binary before you trust the fix (see §13). A claim marked **STILL LIVE**
is reproducible on that checkout. Anything I could not confirm from the source
is marked **(unverified)**.

**One rule that resolves half of these.** A GitReins lane that did no work is
not a pass. The vocabulary is deliberate:

| marker | meaning |
|---|---|
| `✓ lane — ok` | the lane ran and graded the scope |
| `~ lane — skipped (reason)` | the lane graded **nothing**; the reason names why |
| `✗ lane` | the lane ran and found something (or could not run at all) |

`Tier 1 Guards: PASS` alone proves nothing. Read the **lane lines** and the run
log (`.gitreins/logs/guard-*.log`), never the header. The DEGRADED-PASS
machinery exists because a vacuous green was mistaken for a green for months —
`engine/types.py:38-58` states the rule, `engine/types.py:44` lists the lanes it
covers and `engine/types.py:53` the aliases that extend it to the Go lane names.

---

## 1. The secrets lane reports findings when nothing was scanned

**Symptom.** `✗ secrets` with output that is not a finding list —
empty, garbled, or a stack trace — and it reproduces on every run. Or, worse,
the line reads `reported findings (count unavailable)`, which *looks* like a
secret was found. Commits are blocked and there is nothing to un-block.

**Root cause.** Two layers.

1. *The config is invalid for the scanner that reads it.* gitleaks compiles
   every `[allowlist] paths` entry as a **Go (RE2) regexp, not a glob**. A bare
   `*.log`, `*.md`, `*.egg-info/` panics the scanner:
   `regexp: Compile('*.log'): missing argument to repetition operator: '*'`.
   Early `init` generated exactly those; the generated config is written **only
   when `.gitleaks.toml` does not exist** (`gitreins/cli.py:1180-1182`), so a
   repo initialised by an older release keeps its broken config **forever** —
   nothing migrates it.
2. *A scanner crash is graded as a finding.* `_check_secrets` treats any
   non-zero gitleaks exit as "the scanner found something"
   (`engine/guard_manager.py:1602`, the `else` branch at `:1654`) and
   `_gitleaks_failure_status` (`engine/guard_manager.py`, the method directly
   above `_builtin_secrets_scan`) only tries to parse a tally; when it cannot
   parse one it returns `"reported findings (count unavailable)"`. It never
   looks for a panic. There is no `panic`/`regexp:` check anywhere in
   `engine/guard_manager.py` or `engine/types.py` (grep confirms zero hits), so a
   crashed scan is reported in the same register as a real leak.

**Fix.**

- New configs are generated correctly: `_glob_to_regex` (`gitreins/cli.py:1157`)
  escapes RE2 metacharacters and rewrites `*` → `.*`; the allowlist is emitted
  through it at `gitreins/cli.py:1297`. So the *cause* is fixed for anything
  initialised at HEAD.
- Existing configs: convert the entries by hand, or delete `.gitleaks.toml` and
  let the built-in scanner grade instead. If you keep a hand-edited config,
  prove it loads: `gitleaks detect --source . --no-git --config .gitleaks.toml`
  must exit without `panic:` on stdout.

**Status: PARTIALLY FIXED.** The generator is fixed at HEAD; existing configs
are never migrated and a crashed scan is still reported as findings. Confirmed
by grepping the scanner-error taxonomy — there is no panic branch to find.

**How to know it is this one.** The output contains `panic:`, `regexp:` or
`Compile(` — a real finding never does. Second tell: `✗ secrets` on a tree you
know is clean, with the same output byte-for-byte on every run, and a
`.gitleaks.toml` whose `paths` array holds a bare `*`. Third tell:
`reported findings (count unavailable)` with no `Finding:` / `File:` / `Line:`
pair anywhere in the block.

---

## 2. The same scan passes because the scanner is missing

**Symptom.** `✓ secrets — clean`, and the same commit leaks on another machine.
Coverage silently differs by host.

**Root cause.** The lane degrades to the built-in regex scanner when gitleaks is
not on `PATH` (`FileNotFoundError` branch, `engine/guard_manager.py:1690`). The
two scanners do not agree: gitleaks' default GitHub-token rule demands the exact
36-character suffix, the built-in pattern is looser in places and stricter in
others. Historically the degradation was a `logger.debug` line, so "clean"
looked identical whether one scanner ran or two.

**Fix.** Scanner attribution is now first-class (TRUST-003): every secrets
result carries named per-scanner statuses — `SCANNER_CLEAN = "clean"`,
`SCANNER_NOT_RUN = "not on PATH"` (`engine/types.py:368-369`), rendered by
`engine/types.py:392-404`. The summary reads e.g.
`clean (builtin cross-check; gitleaks not on PATH)`. The absent scanner is
**named**, not implied.

**Status: FIXED at HEAD** (`engine/types.py:368-404`,
`engine/guard_manager.py:1615`, `:1648`, `:1696`). Still absent in older wheels.

**How to know it is this one.** The word `gitleaks` is missing from the secrets
line entirely (pre-fix surface), or the line ends `gitleaks not on PATH`.
Confirm with `command -v gitleaks` — from the *hook's* environment, not yours.
Then `PATH="$(git rev-parse --show-toplevel)/.venv/bin:$HOME/go/bin:$PATH" ...`
and re-run. Never read a green secrets lane as "both scanners ran" unless the
line says `clean` for both.

---

## 3. The test lane cannot find its runner on a fresh box (exit 127)

**Symptom.** `✗ tests (full) — /bin/sh: 1: pytest: not found` (or
`bad interpreter: No such file or directory`) and the first commit on a fresh
machine is blocked. The message reads like a content failure.

**Root cause.** Two distinct causes that present the same way, and they need
opposite fixes:

1. **The runner genuinely is not installed / not on the ambient `PATH`.** The
   test lane shells out through `sh -c` with the *ambient* environment. A venv
   that is installed-but-not-activated does not put `pytest` on that `PATH`;
   exit 127 (`command not found`) is the shell, not pytest.
2. **A stale venv.** After a repo rename or a move, `.venv/bin/*` shebangs
   point at the old absolute path. The guard resolves the runner through
   `shutil.which` and passes, while a hand-run of the same script dies with
   `bad interpreter`. That is a different failure and cannot be fixed by
   activating anything.

**Fix.** The lane now classifies instead of hard-failing: `_pytest_runner_missing_hint`
(`engine/guard_manager.py:609-620`) matches a bare-pytest exit 127 against the
shell's `not found` text and produces an actionable line, and the veto at
`engine/guard_manager.py:752-778` demands **both** the not-started evidence and
an unresolvable runner (checked with `shutil.which` *and*
`importlib.util.find_spec("pytest")`), so a real failing test can never be
swallowed. The result is a SKIP with a named reason, not a content failure
(`engine/guard_manager.py:2268`, `:2390`). For the stale-venv case the fix is
`uv venv --recreate` / reinstall.

**Status: FIXED at HEAD** — the guard no longer reports an unprovisioned
environment as a code failure. Regression tests pin it:
`tests/test_guard_manager.py:1290` (127-with-hint),
`tests/test_guard_manager.py:1455` (missing runner is a skip, not a block),
`tests/test_guard_manager.py:1064-1092` (every runner form falls back).
Still live in the class it describes: a *fresh* box still has no runner until
you install one — activate the venv, or install pytest into it.

**How to know it is this one.** The output names a shell and a missing
executable: `pytest: not found`, `command not found`, `No module named pytest`.
If instead it names a **file** with `bad interpreter`, it is the stale-shebang
variant — check `head -1 .venv/bin/pytest`. To tell "runner missing" from "real
failure" mechanically, look for pytest's own session banner: if
`=== test session starts ===` is absent, pytest never started.

---

## 4. pytest exit 5 (no tests collected): PASS in the guard, FAIL in the judge

**Symptom.** The pre-commit hook passes; the judge on the same tree returns
`Stage tier1: FAIL` / `Overall: FAIL`. A repo with no test suite can never earn
a PASS verdict.

**Root cause.** The guard and the judge graded the same tree with different
predicates. The guard has treated pytest exit 5 as a benign pass-with-warning
since it was fixed for the fresh-repo first-commit case:
`_PYTEST_NO_TESTS_RE` + `_PYTEST_ERRORS_RE` (`engine/guard_manager.py:848-849`),
`_pytest_no_tests_benign` (`engine/guard_manager.py:857-869`), applied at
`engine/guard_manager.py:2309` and `:2345`. Exit 5 **with collection errors
mixed in** stays a failure on both sides. But the judge's tier-1 step graded
only the number.

**Fix — one predicate, two callers.** The judge now imports the guard's own
classifier rather than copying it: `engine/pipeline.py:719-745` calls
`pytest_outcome` and `_pytest_no_tests_benign` and records
`data["skipped"] = True` with the guard's own skip reason. The exit code is
still recorded in `data["exit_code"]` — nothing is hidden — and the stage
summary renders it in the DEGRADED register (`~ step: skipped (...)`), never as
a `✓` claiming tests ran (`engine/pipeline.py:1216`). The same treatment was
extended to the missing-runner case at `engine/pipeline.py:745-756`.

**Status: FIXED at HEAD.** Regression tests: `tests/test_pipeline.py:1316`
(exit 5 graded like the guard) and `tests/test_pipeline.py:1470` (missing
runner graded like the guard); the hook/standalone agreement test is
`tests/test_guard_exit.py:294`.

**How to know it is this one.** Both surfaces named exit 5, or the judge's
evidence block shows a zero-test pytest run. Then check whether collection
errors are present: `ERROR collecting` lines or an `N error(s)` count means
**neither** side will pass it — that is a real configuration failure, not this
trap. If the guard skipped and the judge FAILed on a tree with no tests at all,
you are on a build older than this fix.

---

## 5. Tier-1 live/network tests flake when judges run concurrently

**Symptom.** Three shapes, all recurring:

1. The tests lane "dies mid-suite" — exit 2, output ending mid-dot-line, no
   pytest summary. Filed repeatedly as "intermittently dies".
2. A live/network test fails only when several judges (or a repro farm) run at
   once, and passes alone.
3. Load-dependent flakes get reproduced with detached shell loops instead of a
   bounded load generator, and the load outlives the run.

**Root cause.**

1. **Exit 2 is not an interruption.** With `-x` (maxfail) **and** xdist, a run
   that fails one test exits **2**: xdist's `DSession` raises
   `Interrupted(KeyboardInterrupt)` when maxfail trips, and pytest maps
   `KeyboardInterrupt` to `ExitCode.INTERRUPTED`. The same failure without xdist
   exits 1. So a real, deterministic test failure was read as an environment
   problem for six consecutive verdicts. Two harness defects made it
   unreadable: the step's head-only `[:2000]` capture slice landed exactly where
   the short summary begins (so the `FAILED` line vanished), and nothing
   recorded *why* pytest exited.
2. **Concurrency.** Each judge runs its own tier-1 tests step. Tests that bind a
   fixed port, hit the network, or share a fixed temp path collide when a wave
   of judges runs against the same host. Nothing in the harness serializes
   tier-1 across concurrent judges.
3. **Unbounded load.** Reproducing (2) with `setsid sh -c 'while :; do :; done' &`
   detaches the loop from the runner's process group, so it survives the runner
   being killed — 24+ orphaned CPU burners and a load average in the tens on a
   box that also runs a scheduler and a gateway.

**Fix.**

1. FIXED. `engine/types.py:207-270` classifies the outcome from the captured
   output (`maxfail` vs `interrupted` vs `interrupted-unclassified`, plus exits
   1/3/4/5 and signal kills), the tests step records it as
   `data.pytest_outcome` (`engine/pipeline.py:710-716`), and the capture keeps
   the whole output. Regression tests re-run the live `-x -n 2` reproduction and
   pin `returncode == 2`.
2. **STILL LIVE as a class.** No serialization exists. Mitigate on your side:
   give concurrent tier-1 runs their own port/temp namespace, or pin
   `-p no:xdist` / drop `-n` when you are already running a judge wave. There is
   no `file:line` to point at because the guard is absent, not broken.
3. FIXED as a documented rule, not a code gate. `docs/load-reproduction.md`
   (the `INT-FLAKE-5` section) forbids detached loops and names the replacement:
   `scripts/loadgen.py` — children are `daemon=True` **and** `PR_SET_PDEATHSIG`,
   so the kernel kills them when the parent dies even on `SIGKILL`; worker count
   is hard-capped, duration is bounded by `--seconds`. The tier-2 evaluator
   refuses detached burn loops and names the correct primitive
   (`engine/command_hygiene.py`), and the guard path runs steps through
   `command_hygiene.run_bounded` (`engine/pipeline.py:670-676`).

**Status: shape 1 FIXED; shape 2 STILL LIVE; shape 3 fixed by doctrine +
tooling.** Verified by reading `docs/load-reproduction.md`,
`engine/types.py:207-270` and grepping for any tier-1 cross-process lock
(none exists).

**How to know it is this one.** *Shape 1*: exit 2 **and** `-x` **and** xdist are
all in play; the output ends mid-line with no summary; the verdict predates the
`pytest_outcome` field. *Shape 2*: the failure disappears when you run the same
lane alone; grep the test for `bind(`, a hardcoded port, a live hostname, or a
fixed `/tmp/...` path. *Shape 3*: `ps aux` shows survivors after your runner
exited, or the host load average does not fall when the run ends.

---

## 6. A shared task-state file corrupted by parallel completions

**Symptom.** A wave of workers each runs `gitreins task complete`. Afterwards
`tasks.yaml` is missing tasks that were there, or will not parse at all. The
judge then reports `Task not found` for work that ran minutes ago.

**Root cause.** `TaskManager` did a read-modify-write cycle per completion. Two
processes interleave: the last writer clobbers the first's task, or an
interleaved partial write truncates the YAML. A truncated store can still parse
as a *smaller but valid* document, so it loads as a silent partial list.

**Fix.** Every mutation funnels through `_locked_load_save`
(`engine/task_manager.py:81-112`): an OS-level `flock(LOCK_EX)` on a sidecar
`tasks.yaml.lock` (`TASKS_LOCK_SUFFIX`, `engine/task_manager.py:51`) that
serializes **across processes**, auto-releases if the holder is killed, and
**reloads inside the lock** — so each writer mutates the state as of lock
acquisition and concurrent completions of *different* tasks both land. `_save`
writes via a temp file plus `os.replace` (`engine/task_manager.py:223-229`),
atomic on POSIX, so a crash mid-write can never leave a partial document.

**Status: FIXED at HEAD.** The docstring at `engine/task_manager.py:82-98`
describes the exact failure mode it replaced. Note the residual: the lock is per
*file path*, so two checkouts of the same repo have two independent stores —
the lock does not merge them (`engine/worktree_fleet.py:162` says so explicitly).
Corruption across checkouts is a different bug: see §14.

**How to know it is this one.** Count the tasks:
`grep -c '^  [a-z]' .gitreins/tasks.yaml` versus the number of completions you
ran. Then re-read: `python3 -c "import yaml,sys;yaml.safe_load(open('.gitreins/tasks.yaml'))"`
— a parse error means truncation. Two tells separate it from a *judge* bug:
tasks are missing rather than failed, and it only ever happens with concurrent
completions (a serial `task complete` loop never reproduces it).

---

## 7. Verdict storage colliding with task-branch namespaces

**Symptom.** `Verdict saved to disk but not committed (git unavailable)` — the
verdict exists locally and never reaches history. Or a task worktree cannot be
created at all, failing with a ref-lock error on every attempt.

**Root cause.** History used to be committed to the **branch**
`refs/heads/gitreins`. Task worktrees branch at `refs/heads/gitreins/task/<id>`
— a **child ref path**. Git cannot create
`refs/heads/gitreins/task/x` while `refs/heads/gitreins` exists (a ref name is a
directory prefix of the other), and symmetrically cannot lock the canonical name
while a child exists:

```
cannot lock ref 'refs/heads/gitreins': 'refs/heads/gitreins/task/fix-add' exists
```

So the two features were mutually exclusive in either order: complete one task
and every task worktree fails; create one task worktree and no verdict commits.

**Fix.** History moved **outside `refs/heads/`** to
`HISTORY_REF = "refs/gitreins/history"` (`engine/persist.py:149`), where no
branch name can prefix-collide. The rationale is spelled out in the source
(`engine/persist.py:130-147`). Reads union the canonical ref, the legacy branch
`refs/heads/gitreins` (`LEGACY_HISTORY_REF`, `engine/persist.py:159`) and the
remote-tracking copies (`:170-171`), and the first write after an upgrade chains
onto the legacy tip, so old history is preserved without a migration. One-ref
migration if you want it: `git update-ref refs/gitreins/history refs/heads/gitreins`.

**Status: FIXED at HEAD.** Verified at `engine/persist.py:149`,
`:376-393`, `:745-763`. An *older* checkout that only has
`history.storage: git` with the legacy branch still carries the collision.

**How to know it is this one.** The error text names the two refs, or a task
worktree fails with a ref-lock message mentioning `gitreins`. Check what exists:
`git for-each-ref refs/heads/gitreins refs/gitreins` — if both the plain branch
and a `gitreins/task/*` child appear, or the plain branch exists and
`refs/gitreins/history` does not, you are on the pre-fix layout. Escape hatch
while you migrate: `history.storage: filesystem`.

---

## 8. Verdict history is invisible in a fresh clone

**Symptom.** `gitreins report` / `gitreins serve` on a fresh clone prints
"No verdict history found", while the origin demonstrably carries hundreds of
verdicts. Or — the misleading variant — it shows **two** verdicts from months ago
and nothing else.

**Root cause.** Three stacked causes, all about refs a clone does not have:

1. `git clone` maps `refs/heads/*` to `refs/remotes/origin/*` and fetches
   **neither** local history ref; the canonical `refs/gitreins/history` is not
   fetched at all. The reader looked only for a *local* ref → nothing found.
2. Two verdict directories were historically **git-tracked despite
   `.gitignore`**, so a fresh clone materialises exactly those two and the
   reader's fallback never engages — you get two stale verdicts instead of an
   honest empty.
3. The legacy fallback branch was pushed nowhere, so the documented
   branch-fallback is empty.

**Fix.** The reader now consults the **remote-tracking** copies as well:
`REMOTE_HISTORY_REF = "refs/remotes/origin/gitreins/history"` and
`REMOTE_LEGACY_HISTORY_REF = "refs/remotes/origin/gitreins"`
(`engine/persist.py:170-171`), unioned in `_history_refs`
(`engine/persist.py:376-393`) and used by the report path
(`engine/persist.py:425-465`). The `:162-170` comment states the fresh-clone
shape verbatim.

**Status: FIXED at HEAD** (commit `a29e400`, *"fresh-clone verdict fallback
reads remote-tracking refs"*). The two git-tracked August verdict directories
are a separate, still-present blemish — see "how to know" below.

**How to know it is this one.** Run
`git for-each-ref --format='%(refname)' | grep gitreins`.
- Nothing → the clone has no history ref of any kind → you are seeing §8.1.
- Only `refs/remotes/origin/gitreins/history` → you are on a binary without the
  remote-tracking fix; fetch or create the local ref, or read it directly.
- Exactly two files under `.gitreins/history/` that are git-*tracked* → the
  stale-tracked-directory blemish; untrack them to see the real state.
Prefer `gitreins serve --repo <checkout-with-history>` or the JSON API
(`/api/verdicts/<date>/<hash>`) over `report` on a fresh clone.

---

## 9. An editable install makes a worktree silently import the main tree's package

**Symptom.** You edit code in a task worktree, run the tests, and they pass —
but they tested the *other* tree. Or your change appears to have no effect, or
the edit is "already implemented". Nothing errors.

**Root cause.** `_link_venv` (`engine/worktree_manager.py:587-599`) symlinks the
configured shared venv from canonical main into every task worktree
(`worktree_venv_source` / `worktree_venv_name`, both default `.venv`, read at
`engine/worktree_manager.py:395-397`). If that venv holds an **editable
install**, its import finder holds **absolute paths to the main checkout**, not
relative ones. Measured in this repo's own venv:

```
.venv/lib/python3.10/site-packages/__editable__.gitreins-0.15.0.pth
  → imports __editable___gitreins_0_15_0_finder
__editable___gitreins_<version>_finder.py:
  MAPPING = {'engine':    '<MAIN-CHECKOUT>/engine',
             'gitreins':  '<MAIN-CHECKOUT>/gitreins',
             'gitreins_mcp': '<MAIN-CHECKOUT>/gitreins_mcp'}
```

where `<MAIN-CHECKOUT>` is a hardcoded **absolute** path — the canonical main
tree, whatever directory the repo was installed from.

So `python -m pytest` inside a worktree imports the **main** tree's `engine/`,
`gitreins/` and `gitreins_mcp/` — the symlinked interpreter resolves the
editable finder first, and the finder points home. A worker's edits are
invisible to the tests the worker runs.

**Fix.** There is no in-product guard for this (grep confirms no `editable`
handling in `engine/worktree_manager.py` or `engine/worktree_disposable.py`).
Operational rules that work:

- In a task worktree, run the package by **path** — `python -m pytest` from the
  worktree root with the worktree on `sys.path` (e.g. `pythonpath = ["."]`), or
  `PYTHONPATH=<worktree>` — never rely on the symlinked venv's editable finder.
- Assert it: `python -c "import engine; print(engine.__file__)"` must print a
  path **inside the worktree**. Do this before believing any test result from a
  worktree.
- If the venv's editable install is what you want to test, use
  `worktree fresh`, which builds a tree with its own toolchain environment
  rather than the shared symlink.

**Status: STILL LIVE at HEAD** — verified by reading the finder MAPPING above
and `_link_venv` at `engine/worktree_manager.py:587-599`. This is a property of
Python editable installs plus the shared-venv design, not a single bug; it will
not be "fixed" by a version bump. It is also the mechanism behind the
dead-venv-shebang trap in §3, and behind the pinned-environment fix that makes
disposable runs match the tree they grade.

**How to know it is this one.** `python -c "import <package>; print(<package>.__file__)"`
from inside the worktree prints the **main checkout's** path. Second tell: your
edit is present in `git diff` in the worktree and absent from the failure
output. Third tell: the tests pass and the code is wrong.

---

## 10. A `.venv` symlink or venv created inside a worktree is swept into a commit

**Symptom.** Two related shapes:

1. `worktree fleet --merge` (or a task merge) refuses every lane with
   "canonical main has uncommitted changes" / "held", on a stock install where
   you committed nothing.
2. A worker's `git add -A` commits a `.venv` symlink (or thousands of venv
   files) into history, and the guard's own test run dirtied the tree that then
   got swept.

**Root cause.**

1. The merge gate is `git status --porcelain --untracked-files=all` minus an
   exemption list (`engine/worktree_manager.py:1101-1113`). The harness itself
   writes runtime files on every run — `worktrees.json`, `worktrees.lock`,
   `disposable.json`, `disposable.lock`, `tasks.yaml.lock`, guard logs, verdict
   docs — and inside a worktree the test lane's `uv run pytest` creates a
   `.venv` symlink and a `uv.lock`. None were exempt, so the gate saw the
   harness's own bookkeeping as the user's uncommitted work and closed.
2. `.venv` is **not** in the installer's `.gitignore` template.
   `GITREINS_GITIGNORE_ENTRIES` (`gitreins/cli.py:51-83`) covers the
   `.gitreins/` runtime surface; Python repos additionally get `__pycache__/`
   (`gitreins/cli.py:359-362`). `.venv/` is not there. The gate exemption does
   not gitignore the file — it only says "do not treat this as dirt for the
   merge". So `git add -A` still stages the symlink.

**Fix.**

1. FIXED. `_is_clean` (`engine/worktree_manager.py:1087-1113`) exempts the
   runtime artifacts, plus — **scoped to task worktrees only**
   (`_is_task_worktree`, `engine/worktree_manager.py:1103`) — the configured venv
   name and an untracked `uv.lock` / `.uv.lock`
   (`WORKTREE_VENV_LOCKFILES`, `engine/worktree_manager.py:289`). The scoping is
   deliberate and documented at `engine/worktree_manager.py:1075-1080`: an
   untracked `.venv` in **canonical main** is the user's real state and must
   still hold the merge. The installer's ignore list was extended to match the
   gate (`engine/worktree_manager.py:234-257` describes the paired lists;
   `gitreins/cli.py:67-72` warns they must stay in step).
2. **STILL LIVE for `git add -A`** — add `.venv/` (and `uv.lock`) to your own
   `.gitignore` before the first `git add -A` in any repo where the harness runs
   with `uv`. This repo's own `.gitignore` has `.venv/` and `venv/`; a consumer
   repo does not get them from `install`.
3. Related and FIXED: the QA ledger `qa-ledger.jsonl` is now in the template
   (`gitreins/cli.py:59-66`) — a row carries the agent id, the server, evidence
   paths and findings, so a `git add -A` used to commit infrastructure detail
   into user history.

**Status: merge gate FIXED at HEAD; the gitignore gap STILL LIVE.**

**How to know it is this one.** Run the gate by hand in the tree:
`git status --porcelain --untracked-files=all`. If the only lines are
`.venv`, `uv.lock`, or files under `.gitreins/`, you are looking at the merge
gate, not at real work — check your `gitreins --version` and get the fix, or
gitignore them. If `git log --stat` shows a `.venv` symlink in a commit, you are
looking at the sweep; `git rm --cached .venv` and gitignore it.

---

## 11. `git_*` environment variables leak from a hook into the nested test run

**Symptom.** Tests pass by hand and fail under the guard, or a nested
`git worktree add` inside a test dies with
`fatal: .git/index: index file open failed: Not a directory`. Any 'no such
file or directory' error mentioning `.git/index` in a test that itself creates
a repo.

**Root cause.** Git exports `GIT_INDEX_FILE`, `GIT_DIR`, `GIT_WORK_TREE` and
friends to **pre-commit hooks**. The guard runs the test lane as a child of that
hook, so those variables are inherited by every test process. Because
`GIT_INDEX_FILE` is often **relative**, a nested git command resolves it against
whatever cwd it is in and lands on a directory that does not exist. The staged
set itself is also at risk: staged-file discovery can be made to read a
**foreign** repo's index.

**Fix.** Strip every `GIT_*` variable at each boundary, using one shared helper
rather than per-callsite scrubbing:

- secrets lane: `_check_secrets` passes `env=_sanitized_env()`
  (`engine/guard_manager.py:1639`; the same helper guards ten other subprocess
  spawns in that module)
- Go lane: `_sanitized_env()` (`engine/guards.py:15-25`), whose docstring names
  this exact failure and notes the Go lanes originally missed it
- judge pipeline steps: `engine/pipeline.py:655-660`
- language detection: `engine/lang_detect.py:249`
- path/board resolution: `_sanitized_git_env()` (`engine/repo_paths.py:60`), used
  at `:74`, `:113`, `:205`
- verdict persistence: `_git_env()` plus a throwaway `GIT_INDEX_FILE`
  (`engine/persist.py:879`, `:803-821`) so index writes never touch the caller's
  index

Staged-file discovery is pinned by a regression test that plants a foreign
repo's index and asserts the workdir's own index is used.

**Status: FIXED at HEAD.** Verified at the six `file:line` sites above, all
present in the source.

**How to know it is this one.** Reproduce the boundary in one line:
`GIT_INDEX_FILE=/nonexistent/index .venv/bin/pytest <the failing test> -q` fails,
and the same command without the prefix passes. Or print
`[k for k in os.environ if k.startswith("GIT_")]` from inside the failing test —
a hook run shows a non-empty list that a hand-run does not. Any error text
naming `.git/index` from a test that makes its own repo is this class.

---

## 12. A guarded tree where nothing was staged grades as a pass

**Symptom.** `gitreins guard` prints `Tier 1 Guards: PASS` (or the green
`Tier 1 PASSED — committing...` line) on a tree where no lane actually ran, and
the exit code is 0. Or `gitreins commit` prints the green gate, then git's
"nothing to commit" and **exit 1**, with the guard summary last — a green gate
and a red exit in one output.

**Root cause.** Historical: with an empty change set every lane returned
`passed=True` with a prose reason ("No files staged — skipped") and the whole
run exited 0. Combined with the diff-mode sentinel ambiguity — the harness uses
`None` for both "a real full-suite fallback" and "no test targets matched →
skipped" — the run could report a full-suite banner on a synthetic run whose
tests lane executed nothing.

**Fix.**

- TRUST-001: a lane that graded nothing is a **SKIP**, not a pass
  (`engine/types.py:38-58`). Skips are counted, rendered with `~`, and named —
  lint `"no files in scope"` / `"no staged files"`
  (`engine/guard_manager.py:2034-2036`), tests the same
  (`engine/guard_manager.py:2215-2225`).
- The result carries `degraded` + `allow_skips` and the CLI decides the exit
  code: `if result.degraded and not result.extra.get("allow_skips", False)`
  → DEGRADED PASS line, **non-zero exit**, with an explicit instruction to set
  `guards.allow_skips: true` (`gitreins/cli.py:2128`, `:2156-2168`). CI and
  merge-back consume that exit code (`engine/types.py:39`).
- The diff-mode banner now distinguishes skip from fallback
  (`engine/guard_manager.py:1539`, `:1305`; commit `835dc9e`) so the collapse of
  `None` no longer lies in the console.
- The Go lanes were brought under the same machinery via
  `_SUBSTANTIVE_STEP_ALIASES` (`engine/types.py:53`), because the substantive-id
  set was keyed on the Python lane names and a Go run that did no work was not
  flagged at all.

**Residual — read the lane lines, not the header.** `init` writes
`guards.allow_skips: true` for fresh repos, deliberately, so the first commit on
an empty repo is not blocked (`gitreins/cli.py:483-487`, `:1134`, and the
template at `:120`). On such a repo a nothing-staged run is DEGRADED PASS with
**exit 0** again — by configuration, not by bug. The exit code cannot distinguish
"graded clean" from "graded nothing"; the lane lines can.

**Status: FIXED at HEAD** for the vacuous green; the `allow_skips: true`
default means exit 0 is still ambiguous by design.

**How to know it is this one.** Look for the two facts together: the header
says PASS **and** every lane line reads `~ lane — skipped (...)` or is absent.
Then `grep allow_skips .gitreins/config.yaml` — `true` explains exit 0. A
genuinely clean run shows `✓` per lane. If the header says PASS and no lane
line appears at all, you are on a build where the lane's enablement never
reached the gate (the neighbouring trap: a config key in the wrong block means
the lane is **inert**, and an absent lane looks identical to a passing one —
verify enablement by the lane line, never by the PASS header).

---

## 13. An upgraded-but-stale installed binary reports the same version as the source checkout

**Symptom.** You bump the version in the source tree, reinstall or upgrade, and
`gitreins --version` still prints the old number — while the source says the new
one. Or the CLI insists an update is available for the version it is already
running. Or it prints no update when it should.

**Root cause.** `engine/version.py` resolves the version from
**installed package metadata first**: `__version__ = metadata.version("gitreins")`
(`engine/version.py:20`), falling back to the local `pyproject.toml`
(`:24-26`) and then to `"0.0.0.dev"` (`:27`). That metadata is
**frozen at install time**. In this repo the venv holds
`gitreins-0.15.0.dist-info` with `Version: 0.15.0`, and the console script's
shebang pins the absolute interpreter
(`.venv/bin/gitreins` → `#!/<MAIN-CHECKOUT>/.venv/bin/python3`). So:

- Edit `pyproject.toml`, do not reinstall → metadata still says the old version.
- An **editable** install makes it worse: the dist-info version is frozen *and*
  the import mapping points at the main checkout (§9), so the binary behaves
  like the old tree even though the code on disk is new.
- The update checker compares PyPI against that stale number
  (`engine/config.py:541-580`), so it can nag about a version you already have,
  or stay silent about one you do not. Its answer is cached for
  `update_check_ttl` (default 24h), which can also serve a stale verdict.

**Fix.** None in-product; this is install hygiene. After any version bump or
upgrade, reinstall into the environment that runs the binary
(`uv pip install -e .`, `pipx install --force …`, or `uv tool install`), then
prove it: the version the binary prints must equal the version in
`pyproject.toml` **and** `METADATA`. The historical fix was to make the version
metadata-dynamic rather than a hardcoded literal in the source, which is what
`engine/version.py` now does — so a *correctly built* wheel can no longer
disagree with its own METADATA. The stale *install* is a different problem and
remains yours.

**Status: the hardcoded-literal half is FIXED at HEAD**
(`engine/version.py:3-27` uses `importlib.metadata`, verified by reading it);
the stale-install half is **STILL LIVE by design**.

**How to know it is this one.** Compare three numbers, in this order:

```bash
gitreins --version
grep -m1 '^version' pyproject.toml
grep -m1 '^Version' .venv/lib/python*/site-packages/gitreins-*.dist-info/METADATA
```

- Binary == METADATA != pyproject → stale install; reinstall.
- Binary == pyproject == METADATA but the *behaviour* is the old one → you have
  two installs on `PATH`; run `which -a gitreins` and check the pre-commit hook's
  own environment. A hook that calls bare `gitreins` grades with whichever one
  resolves first for the hook, not for you.
- The version is right but the update nag is wrong → the cached answer;
  `update_check_ttl` in `.gitreins/config.yaml` is the knob.

---

## Adjacent fights worth knowing (same families)

- **`history.storage: git` used to leave the working tree on a throwaway
  branch**, so every later `gitreins judge` ran in a tree with no tasks and
  answered `Task not found`. The verdict writer no longer checks anything out —
  it writes through a throwaway index and `update-ref` (`engine/persist.py:803-821`,
  `:879`) — so this shape is FIXED at HEAD. If you see repeated
  `Task not found` after a passing judge, check `git branch --show-current`
  before anything else. Related: never let `.gitreins/tasks.yaml` be **tracked**
  (`git ls-files .gitreins/tasks.yaml` must be empty) — a tracked store gets
  reverted by any git operation that touches the tree, silently removing tasks.
- **Console truncation hides the real error.** The guard keeps only the last
  2000 characters on failure and prints the **first** line, which for pytest is
  `=== test session starts ===` — the failure is in the truncated middle, and
  for a secrets finding the value and location get cut. The judge's tier-1
  evidence is likewise head-capped. Do not debug from the summary: the full
  untruncated run log is retained to disk and referenced from the verdict
  (`engine/pipeline.py:758-766`, `guard_log`), so read
  `.gitreins/logs/guard-*.log` or re-run the lane's command yourself. **How to
  know**: the console line names no test and no file, but the exit is non-zero —
  that is always truncation, never "no detail available".
- **Docs-count drift fails the commit.** This repo chains
  `scripts/check_docs_drift.py` into the guard's `test_command`
  (`.gitreins/config.yaml:18-19`). It fails closed on three things: the README
  release banner versus `pyproject.toml`, every `N tests pass` / `N tests across`
  / `N test files` claim in README **and** CONTRIBUTING versus the live
  collection, and evaluator tool counts versus `EVALUATOR_TOOLS`. An
  unmeasurable collection is a FAIL, never a green. `scripts/check_cli_doc_sync.py`
  covers the CLI surface the same way (subcommand count, table, options) —
  documented *examples* parsing proves nothing about the claims around them.
  **How to know**: the failure output names `FILE:LINE` and both numbers; run the
  script directly to see it.

## Quick triage table

| What you see | Look at | Section |
|---|---|---|
| `✗ secrets` with no finding list; `panic:`/`regexp:` in output | `.gitleaks.toml` `paths` entries | §1 |
| `✓ secrets — clean` that leaks elsewhere | does the line name `gitleaks`? | §2 |
| `✗ tests — pytest: not found` / `bad interpreter` | shebang, activated venv, hook PATH | §3, §13 |
| Hook passes, judge FAILs, same tree | exit 5 / missing runner; build age | §4 |
| exit 2 mid-suite, or flake only under concurrent judges | `-x` + xdist; port/tmp collisions | §5 |
| Tasks missing / YAML unparseable after a wave | concurrent `task complete` | §6 |
| `Verdict saved … not committed`; worktree ref-lock error | `git for-each-ref \| grep gitreins` | §7 |
| Fresh clone: no verdicts, or two old ones | `git for-each-ref \| grep gitreins` | §8 |
| Worktree tests pass, edit had no effect | `python -c "import pkg; print(pkg.__file__)"` | §9 |
| Fleet merge refuses every lane; `.venv` in a commit | `git status --porcelain -uall` | §10 |
| `.git/index` errors in a test that makes a repo | `GIT_*` in the child environment | §11 |
| `Tier 1 Guards: PASS` with no lane lines | lane lines, `allow_skips` | §12 |
| Version wrong / update nag wrong | binary vs pyproject vs METADATA | §13 |

## Verifying your install before you trust any of this

```bash
gitreins --version                        # must equal pyproject + METADATA (§13)
which -a gitreins                         # more than one = PATH shadowing
git for-each-ref --format='%(refname)' | grep gitreins   # §7/§8
command -v gitleaks                       # decides §1/§2 coverage
python -c "import engine; print(engine.__file__)"        # §9, from a worktree
git ls-files .gitreins/tasks.yaml         # must be EMPTY
grep -n 'allow_skips' .gitreins/config.yaml              # §12 exit-code meaning
```

If the answers disagree with the fix status above, your binary predates this
checkout — a green source tree can ship a broken installed package for weeks.
