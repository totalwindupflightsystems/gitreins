# Board id hygiene — one id, one finding (QA-GITREINS-POC-8)

`tasks.jsonl` is append-only history. Ids are never renumbered or deleted, and a
new row continues from the highest suffix **in use** — complete rows included —
so an id keeps identifying the same finding forever. Every row carries `id`,
`title` and `status`.

The rule is enforced by `scripts/check_board_ids.py`, which CI runs as
**Verify board id hygiene**. Run it yourself before hand-appending rows:

```
python scripts/check_board_ids.py .coding-hermes/board
✓ board ids — unique (272 row(s); grandfathered legacy duplicates: QA-GITREINS-POC-1×8, QA-GITREINS-POC-2×2, QA-GITREINS-POC-3×2)
```

It fails (exit 1) on: a **new** duplicated id, a row missing `id`/`title`/`status`,
a baseline entry whose count no longer matches the board, and a baseline entry
that is no longer duplicated at all (the baseline may only shrink — delete the
entry in the same commit that makes it stale). Exit 2 means the board could not
be read.

## The 2026-09-01 … 09-09 QA re-filing (why this file exists)

The QA filing path numbered its per-cycle findings from 1, so every cycle re-used
`QA-GITREINS-POC-1` (later `-2`, `-3`) for a *different* finding. At the audit
(2026-09-17, tick 304) the board held 272 rows, **12 of them bound to a
duplicated id** — 8, 2 and 2 rows respectively, each row a distinct title:

| id | rows | filed | titles (first 60 chars) |
|---|---|---|---|
| `QA-GITREINS-POC-1` | 8 | 2026-09-01 … 09-09 | DF-016 secret-merge fix uncommitted · QA battery recorded zero cells · spawn failed on bunker-las-03 (×2) · CI battery fails in act + native · build/backup artifacts in the tree · bunker-las-03 not registered (×2) |
| `QA-GITREINS-POC-2` | 2 | 2026-09-01, 09-05 | untracked `build/` artifact · battery brittle under the resource cap |
| `QA-GITREINS-POC-3` | 2 | 2026-09-01, 09-05 | stray `.gitreins/tasks.yaml.bak` · truncated state file needs crash verification |

Consequences: title/id dedupe could never match ("continue after the highest
suffix" found `-1` every cycle), the same class of finding was re-filed for days,
and `boardctl validate` reports the duplicate ids as 9 pre-existing errors on
every run.

## The fix

* **Producer side** — `qa.ts` in the hermes-dagger repo
  (`examples/coding-hermes/qa.ts`, QA-DAGGER-GUARD 2026-09-10) scans the board
  once per run: a finding whose exact title is already a **pending** row is
  skipped, and new rows are numbered after the highest suffix *ever used*
  (`max_suffix`). That is where the ids are allocated; the fix lives there, not
  in this repo.
* **Consumer side** — this repo: `scripts/check_board_ids.py` + the CI step, and
  `.coding-hermes/board/id-baseline.json`, which grandfathers the 12 legacy rows
  **by count** so the gate is meaningful today without pretending the history is
  clean.

## Per-row disposition of the cluster (verified live, tick 304)

| id / filed | finding | disposition |
|---|---|---|
| `…-1` 09-01 | DF-016 secret-merge fix uncommitted | **closed — premise false**: committed as `9cec054` (2026-09-04), `engine/guard_manager.py:472` + `tests/test_guard_manager.py:444`, 4 tests pass |
| `…-1` 09-03 | QA battery recorded zero cells | **closed — resolved**: batteries record cells; the 2026-09-17T05:23Z run pulled 11 evidence rows and the ledger holds gitreins-poc rows |
| `…-1` 09-03 | spawn failed on bunker-las-03 | **closed — resolved**: `bunker-las-03` is registered in `~/.bunker/config.yaml` (since 2026-09-10) and a battery launched, ran and collected on it |
| `…-1` 09-04 | spawn: bunker-las-03 not found in config | **closed — resolved** (same evidence) |
| `…-1` 09-05 | CI battery fails in act emulation + native | **left pending, premise corrected**: the fresh battery's `ci-pass` FAIL is contaminated by a live-tree sync race (the synced tree carried an in-flight test file → ImportError → rc 2), and `act` is invoked without a workflow filter, so it runs the tag-gated `release.yml` too and can never pass off a tag ref. Needs the harness's act invocation scoped, not a repo change |
| `…-1` 09-07 | build/backup artifacts pollute the tree | **closed — resolved**: `build/` (`.gitignore:6`), `.vfs/graph/.last_reconcile` (`:73`), `.vfs/graph/.parse_cache.json` (`:74`) are ignored and `git status --untracked-files=all` reports no strays; the two remaining `.bak` files are tracked dated snapshots, not strays |
| `…-1` 09-08 | run_battery: bunker-las-03 not registered | **closed — resolved** (same evidence) |
| `…-1` 09-09 | battery target missing from config | **closed — resolved** (same evidence) |
| `…-2` / `…-3` | earlier cycles' duplicates | both cycles were already closed complete; the ids stay grandfathered in the baseline |

Going forward: a QA cycle that re-files a finding gets a NEW id after the highest
suffix in use, and this gate turns any regression into a red CI step naming the
id and the rows involved.
