# QA-GITREINS-16 — Attribution of the 2026-11-15 stale-snapshot board rewrite + write-path hardening

Covers gitreins board rows QA-GITREINS-16 (attribution + hardening) and QA-GITREINS-15
(closeout append must never rewrite untouched rows). All experiments ran on SCRATCH
copies; the live board at /home/kara/gitreins/.coding-hermes/board/ was never modified
by this worker.

## 1. The damage (commit 60789922)

- Commit: `60789922bd659585be95a5b1739ebbef185202cb` "qa: record gitreins battery
  finding QA-GITREINS-14", author totalwindupflightsystems, date
  `Sun Nov 15 05:48:43 2026 -0500`. Parent `1d30376c` holds the pre-damage board.
- `git show 60789922 --stat`: `.coding-hermes/board/events.jsonl | 4 ++--`,
  `.coding-hermes/board/tasks.jsonl | 4 ++--` — 2 lines substituted IN PLACE in each
  file, line counts unchanged.
- tasks.jsonl: the RELEASE-025 row (added by `b57b0665` "board(releng): file RELEASE-025
  exact-head wiring CI blocker", 05:38:36 -0500) was DELETED, and the RELEASE-023 row
  was reverted to its 2026-11-14 state (stale `ci_result GREEN` payload, foreman_note
  erased) — proven by the re-dispatch QA tick (session
  `590634fc-0967-4c2b-9469-56a20e056419`, FINDINGS message 8109434: "2 occurrences in
  parent 1d30376c, 0 on main").
- events.jsonl: event ids `1794171000000000101` (was RELEASE-023 task_updated,
  ts 10:22:03Z) and `1794171000000000102` (was RELEASE-025 task_created, ts 10:36:36Z)
  were REPLACED by a QA-GITREINS-14 task_created (ts 10:32:04Z) and a gitreins-qa audit
  event (ts 10:32:22Z) reusing the same ids.

## 2. Writer census (classification evidence)

Method: for every candidate, grep of `open(...)` write modes against tasks.jsonl /
events.jsonl paths, plus source read of the write site. Scope: the scripts directory
the gitreins foreman lanes deploy from, the boardctl binary, ~/.hermes/scripts, and the
gitreins repo scripts/.

### The canonical writer set — /home/kara/coding-hermes-skills/coding-hermes-foreman/scripts/

| Script | Classification | Evidence (pre-fix line numbers) |
|---|---|---|
| append_board_task_completed.py | REWRITE (tasks.jsonl, board.jsonl) + APPEND (events.jsonl, id from full read) | read all L164-165, mutate row, `open(tk_path,"w")` write-all L180-182; events append L159 with id from stale-snapshot read L149-151; header rewrite L192-193 |
| append_board_task_dispatched.py | REWRITE (tasks.jsonl) + APPEND (events.jsonl, id from full read) | tasks read-all L155-156, write-all L162-164; events append L152, id from read L130-131 |
| update_board_task_notes.py | REWRITE (tasks.jsonl) | read-all L100-104, write-all L113-115; docstring even says "tasks.jsonl full rewrite is fine" |
| append_board_event.py | APPEND (events.jsonl) + REWRITE (board.jsonl header) | events append L138, id from full read L112-127; header read-modify-write L143-157 |
| create_board_tasks.py | APPEND-ONLY (tasks L180, events L198) but id derivation from full reads L149/L185 | unguarded read-derive-append window can reuse ids under concurrency |
| append_e2e_event.py | APPEND (events.jsonl), id from full read L93-96, append L109 | top-level script (no argparse) |
| totalstack-board-update-jsonl.py | APPEND (events) + REWRITE (header) | events append L61, id from full read L49-51; header write L72-74 |
| board_status.py, read_board_jsonl_state.py, read_board_fixtures.py | READ-ONLY | all `open()` calls without write mode (grep: zero `open(...,'w'/'a')` hits) |

### boardctl (Go binary, /home/kara/.local/bin/boardctl)

- GUARDED: exclusive advisory flock on `<board dir>/.lock` covering the WHOLE
  check-then-act window. Source: coding-hermes-boardctl `internal/board/lock.go`
  (`boardLockFileName = ".lock"`, `withWriteLock` at lock.go:128-132; comment block
  documents the read-inside-the-guard requirement added by DF-BOARDCTL-48,
  commit a5c3593 2026-10-23; original guard BT-050 commit 6a3981e 2026-09-27).
- Every write verb wraps its body in withWriteLock: internal/board/write.go:392, 1203,
  1951, 2110, 2230, 2448; normalizeboard.go:227; iddupe.go:119, 353;
  statusnormalize.go:41; repair_events.go:69.
- CRITICAL LIMIT (documented in lock.go itself): "Advisory means it serializes only
  writers that acquire it: raw >>/jq rewrites from outside boardctl stay unprotected."
  The python writer set above acquired NO lock before this fix — cross-writer
  serialization did not exist.

### ~/.hermes/scripts/ (lane/one-off scripts mentioning board paths)

Census method: recursive grep of every live *.py/*.sh (excluding *.bak) for
tasks.jsonl/events.jsonl/coding-hermes/board, then open-mode classification of each
hit. Results relevant to gitreins board writes:

| Script | Classification | Evidence |
|---|---|---|
| board_append.py | APPEND-ONLY | open(...,'a') L34, L118 only |
| normalize-board-results.py | REWRITE (tasks.jsonl, path arg) | read L113, `open(path,"w")` L179 — one-off repair writer, NO lock |
| fix_tr253_rows.py | REWRITE (tasks.jsonl path arg) | `open(path,"w").write(...)` L31 — one-off, NO lock |
| consolidate-cx53-rows.py | REWRITE — targets the MONITORING board (/home/kara/monitoring/...), not gitreins | BOARD_PATH L13, `open(BOARD_PATH,'w')` L68 |
| file_retirement_rows.py, refile_tr260.py, trace_*.py, burndown_*.py, pm-board-scan.py, board_criteria_census.py, ghost_board_check.py, unblock_scan.py, fabrication_check.py, board-open-slice.py, fleet_status_now.py, progress_since_satellites.py, verify_foreman_sample_6h.py | READ-ONLY w.r.t. tasks/events (writes go to their own STATE/OUT/report paths) | open-mode grep: no 'w'/'a' on board files |
| push_proposals.py | APPEND-ONLY for its ledger (`open(path,"ab")` — "O_APPEND; never w" comment L170); its 'w' opens are .tmp/.bak proposal files, not board files | L170-206 |
| bunker-qa.sh | NO direct board writes (grep "tasks.jsonl|events.jsonl" = zero hits); the QA lane it feeds files rows via boardctl (see attribution) | grep over script |
| tr253_close.py, daily_review.py, fleet-board.py, fleet-sre-daily.py, foreman-tick.sh, pm-standin-tick.sh, dogfood-scheduler-tick.sh etc. | write only their own summary/proof/STATE paths; no tasks.jsonl/events.jsonl mutation | open-mode grep per file |

### gitreins repo /home/kara/gitreins/scripts/

| Script | Classification | Evidence |
|---|---|---|
| check_board_ids.py, review_stale_bugs.py | READ-ONLY | no write-mode opens on board files |
| judgment_viewer.py | writes only its own `args.out` report export | L324 |
| check-board.sh | writes only a /tmp SELF-TEST fixture board (`$tmp/bad/...`) | L185-196 |

## 3. Attribution: which writer produced 60789922

DIRECT PROOF (session store, Hermes state.db messages table, session
`590634fc-0967-4c2b-9469-56a20e056419`, the `gitreins-qa-2026-11-15-10-07-44` lane,
model openai/gpt-6.1-terra):

- message 8095460 (ts 1794738724 = 05:32:04 -0500): `boardctl -C /home/kara/gitreins
  create --id QA-GITREINS-14 --title "[P2] CLI reference documentation drifts..." ...`
- message 8095495 (ts 1794738742 = 05:32:22 -0500): `boardctl -C /home/kara/gitreins
  event --type audit --actor gitreins-qa --detail
  @/home/kara/.hermes/cache/scratch/gitreins-qa-2026-11-15-10-07-44-audit-event.json`
- message 8099658 (ts 1794739694 = 05:48:14 -0500): `git -C /home/kara/gitreins add
  .coding-hermes/board/tasks.jsonl .coding-hermes/board/events.jsonl && git -C
  /home/kara/gitreins commit -m 'qa: record gitreins battery finding QA-GITREINS-14' ...`
- The QA ledger row written by the same cycle
  (`gitreins-qa-2026-11-15-10-07-44-ledger-row.json`, session message 8099612) records
  `"board_event": "1794171000000000102"` — the reused id.

CULPRIT: the board writes in commit 60789922 were made by **boardctl** (`create` +
`event` verbs) invoked by the QA lane session, which wrote back a board view that
predated the concurrent releng lane's board commits (9cdfa1c4 05:23:10, b57b0665
05:38:36, 1d30376c 05:39:41 — all landing RELEASE-023 re-anchors and the RELEASE-025
row in the same files). The releng commits were in git history but the QA writer's file
view at write time did not contain them; its write-back substituted the tail lines and
reused event ids ...101/102.

WHY the guard did not save it: boardctl's own flock serializes only boardctl writers
(lock.go: "raw >>/jq rewrites from outside boardctl stay unprotected"). The releng lane
landed its rows/events through the fleet's python writer scripts, which acquired NO
lock — and the two lanes' git operations (commits/rebases of board files in one shared
checkout) moved the working-tree files outside any lock. Mechanism class: a writer
publishing a read-modify-write-all result from a view that another writer has already
made stale. The unguarded python writer set is the same defect class the fix below
closes; per-writer micro-attribution of the releng lane's exact commands is NOT
reconstructable from the available logs (the releng session's board-write commands are
not in the retained session windows) — the census above shows every unguarded writer
that could produce it, and the fix locks all of them to the same sidecar boardctl
already uses.

## 4. Reproduction (scratch copy — live board untouched)

Scratch: /tmp/qa16-repro/.coding-hermes/board — a byte copy of the live board
(tasks.jsonl 692 lines, events.jsonl 1407 lines) taken 2026-11-26. Driver:
/tmp/qa16-repro/run_repro.py; writer: pre-fix append_board_task_completed.py (git
HEAD before the fix) with two 20s sleeps injected between its events READ→APPEND and
tasks READ→WRITE-BACK to widen the race window deterministically (the injection only
simulates scheduler delay; no code path other than timing changed). While the writer
held its snapshots, a simulated releng-style unguarded concurrent writer appended a
tasks row (RELEASE-REPRO) + two events (ids minted from a fresh read) — twice, once in
each window.

Transcript:

```
CONCURRENT-OUT: concurrent writer: appended row + events 1794171000000000247 1794171000000000248
CONCURRENT-OUT-2: concurrent writer: appended row + events 1794171000000000249 1794171000000000250
WRITER-OUT:
REPRO: events snapshot taken, sleeping 20s while a concurrent writer lands...
REPRO: tasks snapshot taken, sleeping 20s...
OK: events [1794171000000000247,1794171000000000248] tick 99 commit deadbeefcafe task QA-GITREINS-13

=== VERDICT ===
tasks.jsonl lines: pre=692 expected_after_2_concurrent_appends=694 actual=693
duplicate event ids: [1794171000000000247, 1794171000000000248]
events tail (last 4 ids): [1794171000000000247, 1794171000000000248, 1794171000000000247, 1794171000000000248]
```

Both halves of the 60789922 signature reproduced:
- a concurrently-appended tasks row SILENTLY DELETED by the write-back (694 expected,
  693 actual) — the RELEASE-025 loss;
- the same event ids minted by both writers (...247/248 duplicated) — the
  ...101/...102 reuse.

(Honesty note: the live gitreins board already carries pre-existing duplicate event ids
...221, 404, 642, 643, 644 from earlier history — observed during this run, pre-existing,
not caused by this experiment, not repaired by this task.)

## 5. Fix (skills repo, branch fix/board-write-lock, commit 6b48325)

All seven writer scripts in coding-hermes-foreman/scripts/ now:
1. acquire `acquire_board_write_lock(bdir)` — exclusive `fcntl.flock(LOCK_EX)` on
   `<board dir>/.lock`, the SAME sidecar file boardctl's Go guard uses — BEFORE any
   board read, and hold it through the last write AND the staging/commit;
2. re-derive everything (event ids, row scans, header) from files read UNDER the lock —
   no pre-lock snapshot is ever written back;
3. expose optional `--snapshot-tasks-lines` / `--snapshot-events-lines` drift refusal:
   after acquiring the lock, if the caller's pre-lock line count no longer matches
   disk, print `DRIFT REFUSED: ... re-read the board and retry` and exit 2 writing
   nothing. CLI contracts otherwise unchanged (flags are optional; existing
   invocations behave identically).

Lock acquire/release/drift-check sites (post-fix line numbers):

| Script | lock acquire | drift check | lock release |
|---|---|---|---|
| append_board_task_completed.py | L199 | L201-204 | L256 |
| append_board_task_dispatched.py | L168 | L170-173 | L215 |
| update_board_task_notes.py | L138 | L140-141 | L163 |
| append_board_event.py | L148 | L150-151 | L202 |
| create_board_tasks.py | L188 | L190-193 | L251 |
| append_e2e_event.py | L109 | (no argparse; lock covers read-derive-append) | L130 |
| totalstack-board-update-jsonl.py | L63 | (internal tool, lock covers read+writes) | L92 |

The helper is replicated per script (not imported) because every board script in that
directory deploys standalone — the same convention as the numeric_event_ids helpers
(DF-CRIER-511/512 comments). Lock files land in the TARGET repo's board dir and are
data, never staged; test_append_close_commit.py's everything-staged and clean-board-dir
assertions now exempt `.lock` (4 call sites annotated `QA-GITREINS-16`).

Deployment: skills repo branch `fix/board-write-lock` pushed to origin
(git@github.com:coding-hermes/skills.git), push verified
`git fetch origin && git rev-list --count origin/fix/board-write-lock..fix/board-write-lock`
= 0. Deployed via `bash ~/.hermes/scripts/skill-lock-enforce.sh` (log line
`reverted coding-hermes-foreman to 6b48325`); deployed copy verified byte-identical:
`diff -q` repo vs ~/.hermes/skills/coding-hermes-foreman/scripts/append_board_task_completed.py
→ identical, and `grep -c acquire_board_write_lock` = 2 in the deployed file.

## 6. Tests (coding-hermes-foreman/scripts/test_board_write_lock.py)

- `test_two_writer_append_preserves_lines` (QA-GITREINS-15 criterion 1, exact name):
  two concurrent create_board_tasks.py processes on one scratch board — both land,
  every pre-existing line byte-identical, event ids unique. PASS.
- `test_two_concurrent_closes_preserve_rows_and_ids`: two concurrent close writers
  (the 60789922 writer class) on different rows — both rows complete, untouched row
  preserved, no id reuse. PASS.
- `test_drift_refusal_tasks_snapshot` / `test_drift_refusal_events_snapshot`: stale
  pre-lock snapshot → exit 2, `DRIFT REFUSED` message, files byte-identical (nothing
  written). PASS.
- `test_fresh_snapshot_still_writes`: matching snapshot writes normally — the refusal
  cannot wedge a healthy close. PASS.
- `test_every_writer_source_contains_the_flock` (7 subtests) +
  `test_lock_helper_matches_boardctl_sidecar_name`: structural. PASS.

- RED proof (same test file against the PRE-FIX scripts at git HEAD, run in
  /tmp/qa16-redproof): 11 failed, 3 passed, exit 1 — every structural lock check red
  for all 7 writers, both drift refusals red (pre-fix writer accepted no flag and
  wrote through the stale view), fresh-snapshot test red for the right reason
  (unknown-flag exit without the DRIFT message). The two concurrency tests are green
  in both states (the race window is timing-dependent without the injected delay) —
  they pin the fixed behavior; the deterministic red arms are the structural + drift
  tests, and the mechanism itself is red-proven by the section-4 reproduction.
- FULL suite over the directory (post-fix): `python3 -m pytest . -q` →
  **40 passed, 7 subtests passed, exit 0** (includes the pre-existing
  test_append_close_commit.py 22 tests and test_create_board_tasks_max_event_id.py,
  both still green with the .lock sidecar exemption).

## 7. Restore-leg readback (QA-GITREINS-15 leg — recorded, not redone)

Later ticks restored the damage and closed both rows; verified read-only from
/home/kara/gitreins:

```
$ jq -r 'select(.id=="RELEASE-025" or .id=="RELEASE-023") | .id + "=" + .status' .coding-hermes/board/tasks.jsonl
RELEASE-023=complete
RELEASE-025=complete
```

## 8. Commits

- Skills repo (git@github.com:coding-hermes/skills.git):
  branch `fix/board-write-lock`, commit `6b48325233891ecd98cf8aac789a2e5d2139e004`
  "fix(board): flock-guard every foreman board writer; drift refusal for stale caller
  snapshots. Addresses QA-GITREINS-16." — 9 files, +849/−255. Pushed; ahead-count 0.
- gitreins repo (worktree /home/kara/worktrees/gitreins-QA-GITREINS-16, branch
  wt/QA-GITREINS-16): this evidence artifact only.
