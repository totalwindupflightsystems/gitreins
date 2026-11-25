# QA-GITREINS-15 — verification-only closeout evidence

Tick: gitreins-foreman-2026-11-25-22-30-00
Measured against: HEAD 8600a85b (board state as committed on github/main, clean tree, 0 unpushed, 0 behind at tick start).
Reproduction commands: `python3 /tmp/gr-qa15-verify.py` (probes tasks.jsonl + events.jsonl read-only), `grep -c 'RELEASE-025' .coding-hermes/board/tasks.jsonl` -> 10 occurrences live (parent 1d30376c had 2).

## Arm 1 — RELEASE-025 row restored (HEALED)

Live board: RELEASE-025 status=complete, updated_at 2026-11-20T16:40:04Z.
The 2026-11-15 closeout commit 60789922 deleted it from tasks.jsonl; it is present again (10 occurrences).

## Arm 2 — RELEASE-023 carries the final releng readiness state, not the reverted one (HEALED, legitimately superseded)

Live board: RELEASE-023 status=complete, completed_at 2026-11-23T15:20:00Z, ci_result=GREEN, guard_result=PASS — the final v0.22.0 closeout (releng pass 3, commit 64c29443), NOT the stale 2026-11-14 state (GREEN at 9690e956) and NOT the interim RED exact-head state. Restoring the 1d30376c snapshot would REGRESS the row to the RED pre-cut state; the live row is the correct final state.

## Arm 3 — events intact, no id reuse of the rewritten pair (HEALED)

- Historical ids 1794171000000000101 and 1794171000000000102 are both present in events.jsonl (1,399 lines at probe time).
- Tail event ids are 1794171000000000237/238 — no reuse of the rewritten 101/102 pair.
- Historical 101/102 are NOT duplicated.

## Arm 4 — closeout write-path fix (OUT OF THIS REPO — split sliver is live)

The closeout writer that rewrote the board from a stale snapshot is boardctl (~/.local/bin/boardctl) and fleet scripts outside this workdir; engine/board.py is witness-only (load_rows/find_row/closeout_refusal, no write path) and `gitreins task complete` writes only .gitreins state. The write-path fix (re-read before mutate, refuse on snapshot drift, append-only, regression test) is cross-filed in the coding-hermes-tools board as CHT-194, verified live this tick:
`/home/kara/coding-hermes-tools/.coding-hermes/board/tasks.jsonl` carries "CHT-194: pending | boardctl write path must refuse a stale snapshot: re-read board immediately before mutating; never rewrite untouched rows".

## Out-of-scope observation (pre-existing, not this row's arms)

Event-id census (this tick): 6 duplicated event ids — 404 (2x), 642/643/644 (2x each, legacy 2026-10-07 range), 1794171000000000221 (2x, adjacent lines 1380/1381, both empty task_id — looks like an accidental double append by a past tick), plus ~40 legacy events with id=null. The row's AC arm "never reuses an event id" is the regression test owed by the external writer (CHT-194 scope), so these are recorded here as an observation for CHT-194's implementer, not as new in-repo work.

## Close classification

Verification-only close: no repository code change is owed by this row; all in-repo arms healed (verified live above), the remaining arm is external and tracked as CHT-194. commit_hash names the evidence commit on github/main; guard_result/ci_result are scope facts (board/evidence-only commit).

## CORRECTION 2026-11-25T23:35Z (judge run 08abf8b5 falsified Arm 4's routing claim — close retracted to pending)

Judge verdict .gitreins/history/2026-11-25/08abf8b5/verdict.json (tier1 PASS, V1-V4 + artifact-consistency PASS) falsified Arm 4 as written: the writer repo the row points at is /home/kara/coding-hermes-boardctl (not coding-hermes-tools, where CHT-194 was filed), and boardctl ALREADY serializes its read-modify-write against concurrent appends — internal/board/lock.go withWriteLock + real flock(LOCK_EX) since commit a5c3593 (2026-10-23, ancestor of the v0.5.0 that cut 0.22.0), with the untouched-line byte assertion inside the guarded section (DF-BOARDCTL-48/49). Independently re-verified this tick: `go test ./internal/board/ -run TestRewriteVerbs -count=1` -> ok 0.050s (TestRewriteVerbsSerializeAgainstConcurrentAppend + TestRewriteVerbsDropConcurrentAppendWithoutLock).

Consequences:
1. CHT-194's premise ("boardctl write path must refuse a stale snapshot") is stale-false — the exact guard it asks for already ships, with regression tests. CHT-194 should be closed by its owner (coding-hermes-tools board) as superseded-invalid; an audit event with this evidence was appended there.
2. The 2026-11-15 damage commit 60789922 (a board rewrite, not an append) therefore came from a writer that does NOT route through boardctl — the write mechanism is still unattributed (qa_board_append.py is append-only 'ab', engine/board.py has no write path). The open work is attributing that mechanism and closing the unguarded-writer gap; filed as QA-GITREINS-16 in this board.
3. The QA-GITREINS-15 close published at 229bcc65 carried the false Arm-4 claim and is RETRACTED: row reverted to pending with the corrected routing, so the next close rests on true claims. The data arms (Arms 1-3) remain healed — verified live by this tick and by two judge runs (d321c889, 08abf8b5).
