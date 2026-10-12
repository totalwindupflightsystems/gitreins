# Traceability doctrine

**Status:** repository contract · v1 · 2026-10-11
**Applies to:** every claim this repository makes — board rows, wave manifests, commits, tests,
documentation, evidence artifacts and verdicts.
**Owner:** this repository. An operational fleet skill exists as a fallback, but the contract that
binds work in GitReins is this file.

**Why it exists.** A board can read "mostly complete" while the deliverable it names does not exist
anywhere outside the machine: rows close by pointing at other rows, a verdict produced against an
unmerged tree is quoted as proof of shipped code, and a success string in a log stands in for a
re-run. None of that is a missing gate — it is a missing **link graph**. One line per claim, cheap
enough for an author to write in ten seconds and mechanical enough for a second party to check, is
worth more than a process that costs a reviewer ten minutes.

## 1. What this repository inherits, and what it defines

The fleet's scheduler tick contract requires the owning repository to carry its own
`docs/traceability-doctrine.md`. This file satisfies that requirement for GitReins and is
authoritative for this repository.

| part of the contract | where it comes from |
|---|---|
| the `ch:trace` token, the eleven field names, the six hard rules (§2), the witness vocabulary (§3) | a fleet-wide doctrine; its operational recipe lives outside this repository at `~/.hermes/skills/coding-hermes-traceability/SKILL.md` |
| **which surfaces carry markers here**, and in which field | defined in §4 from this repository's own layout — nothing outside the repo knows our board shape |
| the `verdict=` and `evidence=` paths | defined here: `.gitreins/history/…`, `.gitreins/verdicts/verdict.json`, the QA ledger — GitReins *is* the harness that produces verdicts, so these are repo-local facts, not fleet conventions |
| the gates a close-out must show (§6) | defined here from this repository's own guard, CI and docs checks |

Rules of inheritance:

- A downstream, mirror or nested repository either copies this file or carries a **one-line pointer
  to its canonical owner**. A pointer must name a path that resolves; an untested pointer is not a
  contract.
- Where this file and the fleet skill disagree, **this file wins for this repository** — and the
  disagreement is a reportable bug, not something to quietly resolve in either direction.
- The fleet skill is operational (real host paths, real recipes) and is deliberately not copied into
  a public file. Only the contract is reproduced here, in this repository's own words.

## 2. The marker

```
ch:trace row=<ID> spec=<path#anchor> wave=<tick>#<step> test=<path::test> doc=<path> prompt=<path> evidence=<path> witness=<locator> verdict=<id> commit=<sha> memory=<key>
```

| field | meaning | rule |
|---|---|---|
| `row` | the work unit (`DOC-18`, `PYTYP-2603`, `DF-GITREINS-POC-84`, …) | **required.** No row, no trace. |
| `spec` | contract path, optional `#anchor` (`specs/04-Guard-System.md#…`) | required when a contract covers the behaviour |
| `wave` | the dispatch manifest plus what arrived in it: `.coding-hermes/waves/<tick>.json` supplies the tick, and the suffix is a `workers[].task_id` (`#GR-140`) or the lane step label (`#audit`, `#reevaluation`) | when the work arrived in a wave |
| `test` | a pytest node id (`tests/test_cli.py::test_name`) | required for behaviour changes; `test=none:<reason>` otherwise |
| `doc` | the documentation the work is described in | when documentation exists |
| `prompt` | the lane prompt the work rode | when a prompt governed it |
| `evidence` | a path to an artifact **we produced** (log, transcript, saved output, verdict document) | never prose, never another row |
| `witness` | a locator on a surface **we cannot write** | preferred whenever the claim is about the world rather than our own output |
| `verdict` | the evaluation verdict id | must be bound to `commit=` |
| `commit` | short sha | must be reachable from the published tip |
| `memory` | a long-term memory key | cited records only; never load-bearing, never the sole citation |

A stored marker is **one line**. The wrapping above is documentation formatting only: markers written
into a board row, a commit footer or an artifact must be greppable as a single logical line.

Partial markers are honest — an author fills what is known and omits the rest. What is never
acceptable is silence: an unsupported claim is a marker that should not have been written, not a
marker with a plausible-looking value.

### Hard rules

1. **`row` is mandatory.** A trace without a work unit is a comment.
2. **Nothing closes by reference.** `evidence=` resolves to an artifact or a witness. A marker whose
   evidence is another row, another verdict, another report or another marker is refused. Nesting
   claims is how a board reaches high completion while the deliverable it names does not exist.
3. **No witness means saying so.** `witness=none:<reason>`. A stated reason is acceptable; an
   omission is not.
4. **The memory field never carries a claim on its own.** It may accompany a repo-local reference; it
   may not replace one; only records that resolve may be cited.
5. **A verdict counts only when it is bound to the commit it judged**, and that commit is reachable
   from the published tip. A verdict produced against an unmerged worktree is not evidence of shipped
   code. Pre-merge verdicts are cited as work-in-progress, never as a close-out.
6. **The checker is never the author.** Whoever writes the claim does not satisfy it. Where an
   independent reading is available (a second reviewer, a judged verdict, an out-of-band scan), it
   outranks any artifact we wrote about ourselves.

## 3. Witness forms

```
tag:<version>@origin · release:<version> · http:<code>+sha256:<digest> · path@origin:<path> ·
ledger:<predicate> · provider:<usage-page> · live:<endpoint resolves with >=1 hop>
```

Prefer a witness over prose whenever the claim is about the world rather than about our own output.
Where several forms apply, cite the one a stranger could re-take without our cooperation.

## 4. Where markers go in this repository

| surface | path | where the marker rides | typical fields |
|---|---|---|---|
| board rows | `.coding-hermes/board/tasks.jsonl` | the row's prose fields (`reasoning`, `foreman_note`, `worker_summary`, `close_note`) or the dedicated `ch_trace` field | `row` `spec` `evidence` `witness` `verdict` `commit` |
| board events | `.coding-hermes/board/events.jsonl` | the event's `detail` string | `row` `commit` `evidence` |
| wave manifests | `.coding-hermes/waves/<tick>.json` | not marked — the manifest **is** the `wave=` target (`tick` + `workers[].task_id`, `base_sha`, `status`) | — |
| commits | commit message | a footer line per row closed; repeat for multi-row work | `row` `spec` `test` `evidence` |
| tests | `tests/test_*.py` | module docstring or the test's own docstring | `row` `spec` |
| source | `engine/**`, `gitreins/**`, `gitreins_mcp/**` | module or function docstring where the behaviour is defined | `row` `spec` `test` |
| specs | `specs/00-PRD.md` … `specs/10-Deployment.md` | the section that defines the behaviour, linking **down** to the work | `row` `doc` |
| docs | `docs/*.md` | the page that describes the behaviour | `row` `spec` |
| run records | `docs/dogfood/*.md` | the run's own transcript section | `row` `wave` `evidence` |
| evidence artifacts | `docs/dogfood/evidence/**` | the artifact's README or header, naming what produced it | `row` `test` |
| verdicts | `.gitreins/history/<YYYY-MM-DD>/<short-sha>/verdict.json`; merge gate `.gitreins/verdicts/verdict.json` | not marked — these **are** the `verdict=` target, and the directory name carries the commit they judged | — |
| QA ledger | `.gitreins/qa-ledger.jsonl` (read: `gitreins qa list`) | the recorded run row | `row` `evidence` `commit` |
| lane skills | `skills/gitreins-*/SKILL.md` | the procedure that was followed | `prompt` |
| long-term memory | a memory namespace key | never alone: `memory=` always accompanies a repo-local reference | `memory` |

`board.jsonl` (the single-line status rollup: tick counters, `git_branch`, `git_remote`,
`last_commit`) is a **state** surface, not a claim surface. It names the published tip that `commit=`
must be reachable from; it never carries a marker of its own. This repository has no `reports/`
directory: report-shaped output lives in `docs/dogfood/**` and in the verdict documents above.

Nothing in this tree is exempt from a marker. `docs/`, `specs/`, `tests/` and the board are all
claim surfaces; a behaviour described in a page nobody can tie back to a row is a gap, not
documentation.

## 5. Evidence rules

- **Evidence is an artifact we produced, at a repo-relative path that exists.** Saved command output,
  a run transcript, a verdict document, a QA ledger entry, a guard log quoted in the row. A path that
  does not resolve is not evidence, whatever it claims to contain.
- **The machine-readable floor already exists.** `guard`, `judge` and `report` accept `--json` and
  emit documents conforming to `schemas/evidence-v1.schema.json`, described in
  `docs/evidence-contract-v1.md`. When a claim is about a gate result, prefer that document over
  pasting output by hand.
- **Text is not state.** A success string, a log grep or a status word is a diagnostic, never proof.
  Re-run the command and keep its output; never quote a stored digest as if it were the run.
- **Absent means absent.** An unexercised surface is recorded as unexercised, with the reason. A
  missing number with a stated reason is honest; an inferred number is not. A claim that a surface
  was not reached is itself a claim worth recording.
- **Redaction is part of the evidence.** Artifacts committed here are public. Record the shape of a
  credential, never its value; the rules in `docs/data-protection.md` apply to evidence artifacts,
  commit messages and board rows alike.
- **Attribution.** Evidence names what produced it (command, worktree, commit, lane) so a reader can
  re-take it. Unexplained output is not evidence.

## 6. Audit close-out requirements

A row may be closed only when **all** of the following are recorded on the row and reproducible from
a clean checkout:

1. **A marker with `row=`** (§2), on the row and in the commit footer.
2. **A witness or a stated absence** — `witness=<locator>` or `witness=none:<reason>`.
3. **A verdict bound to the commit it judged**, with the persisted verdict path recorded
   (`.gitreins/history/<date>/<hash>/verdict.json`, or `.gitreins/verdicts/verdict.json` for a
   judge-gated merge). Pre-merge verdicts do not close a row (hard rule 5).
4. **The commit reachable from the published tip** named in `.coding-hermes/board/board.jsonl`
   (`git_remote` / `git_branch` / `last_commit`) — with push parity shown, not assumed.
5. **The guard result quoted** (`gitreins guard`). A `DEGRADED` pass states its reason in the row; a
   skip is never reported as a graded pass. This repository sets `allow_skips: true` because its own
   gate is graded by CI on a clean checkout, so an unqualified "PASS" is not sufficient.
6. **The repository's own executable documentation gates run** when the change touches documentation:
   `scripts/check_docs_drift.py`, `scripts/check_cli_doc_sync.py`, `scripts/check_cli_examples.py`.
   These execute the claims rather than reading them, so their output — not a reviewer's opinion — is
   the evidence.
7. **Board-id hygiene** — `python scripts/check_board_ids.py .coding-hermes/board` passes. A close
   that appends a row must not reuse an id, leave a row without `status`, or leave a stale entry in
   `.coding-hermes/board/id-baseline.json` (the baseline may only shrink, in the same commit).
8. **A verification-only close cites the landed commit, the verdict id and the CI run** it is
   verifying, and states plainly that it produced no new change.
9. **A re-evaluation row never closes by reference.** Rows whose job is to re-check other rows carry
   their own evidence; "row X is done, therefore this is done" is refused by hard rule 2.
10. **Residuals are named.** What was not exercised, what was assumed, and what a follow-up would need
    to check. A close-out with no residual section is claiming completeness, which is a stronger
    claim than most work supports.

## 7. Verification — one recursive grep

Every marker is one line, so one command finds every marker in every surface:

```
grep -rIn "ch:trace" .coding-hermes docs specs
```

Recorded output (2026-10-11, this checkout):

```
$ grep -rIn "ch:trace" .coding-hermes docs specs | wc -l
41
$ grep -rIc "ch:trace" .coding-hermes/board/tasks.jsonl .coding-hermes/board/events.jsonl docs/traceability-doctrine.md
.coding-hermes/board/tasks.jsonl:23
.coding-hermes/board/events.jsonl:10
docs/traceability-doctrine.md:8
```

Reading it: every other file under `.coding-hermes`, `docs/` and `specs/` returns 0 — the board is
the only claim surface that carries markers today, and the eight remaining hits are this file's own
text: the grammar, the command above, the token in prose, and this document's marker. The
board's 33 token occurrences sit on 23 lines (one line can carry more than one marker). Two rows put
a bare field list in the `ch_trace` slot with no `ch:trace` prefix (`row=PYTYP-GITREINS-01 …`,
`row=REVIEW-006 …`); they are grandfathered below and are why the prefix rule is stated explicitly.

Two consequences of the check that are worth stating as rules:

- **A marker that cannot be found by that command is not a marker.** Board rows have historically
  carried a bare field list in the `ch_trace` JSON slot with no `ch:trace` prefix; those legacy rows
  are grandfathered (this document does not rewrite history) but every **new** marker carries the
  token so a single grep stays complete.
- **The grep proves presence, not truth.** Finding a marker says a claim was written down; it says
  nothing about whether the claim holds. Truth is the job of the rules in §2 and §5 and of a reader
  who is not the author (hard rule 6).

## 8. Known gaps in this repository

Stated so that a reader does not mistake the absence of a failure for compliance:

- **No marker validator ships here.** The six hard rules are enforced by the documentation audit lane
  and by the reviewer, not by CI. A validator that refuses a marker without `row=`, a
  reference-closure in `evidence=`, a `memory=` used as the sole citation, and a verdict without a
  bound reachable commit is the obvious next step; until it exists, this section is the honest
  description of the enforcement.
- **The doctrine's own checks are manual.** Running the §7 grep and the §6 gate list is a step a
  human or a lane performs — nothing here automates it, and nothing fails if it is skipped.
- **Legacy markers predate this file.** Rows closed before 2026-10-11 were not written against these
  rules and are not being retro-fitted.

<!-- ch:trace row=DOC-18 spec=docs/traceability-doctrine.md#2-the-marker doc=docs/traceability-doctrine.md test=none:not-a-code-change witness=none:repository-document -->
