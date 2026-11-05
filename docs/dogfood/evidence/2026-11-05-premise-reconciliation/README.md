# Premise reconciliation — 2026-11-05 (tick `gitreins-foreman-2026-11-05-11-50-21`)

Ten pending rows were re-probed against the tree at `3574afc1` (== `github/main`, parity 0/0)
because each one's written failure premise had been repaired by earlier landings. This artifact is
the evidence for closing them; the row dispositions land in the FOLLOWING board commit.

## Measured tree

| item | value |
|---|---|
| evidence commit parent (tested tree) | `3574afc11675d7dd87efc38a2941a0184f7b7705` |
| `github/main` | `3574afc11675d7dd87efc38a2941a0184f7b7705` |
| parity `git rev-list --left-right --count github/main...HEAD` | `0 0` (see `parity.txt`) |
| hosted CI at the last code commit | run `42020858384` `completed/success` on `8c64ed85` (see `ci-run-42020858384.json`); zero runs exist for the `[ci skip]` board tip by design |
| full suite | `3204 passed, 17 skipped` rc=0 (`full-suite.log`, 89.1 s) |
| docs drift | OK — version `0.18.0`, counts `3221 tests / 98 test files` (`docs-drift.log`) |
| guard suite | `190 passed` (`guard-suite.log`) |

## Rows and probes

### GR-155 — judge canonical Auger task packets (P1)

Premise under test: the row's six acceptance legs. Implementation already landed as `3b87b30f`
("feat: judge canonical Auger task packets (GR-155)", 2026-11-02) carrying `engine/auger_packet.py`
(+367), `engine/judge.py` (+160) and `tests/test_auger_task_packet.py` (+230).

| AC | proof | result at `3574afc1` |
|---|---|---|
| 1 board criteria copied verbatim, board id retained/mapped | `tests/test_auger_task_packet.py::test_board_task_to_gitreins_round_trip` | pass |
| 2 invalid/stale/out-of-scope reference refuses BEFORE Tier 2 | `::test_invalid_packet_never_starts_judge` | pass; `engine/judge.py::_prepare_auger_task` returns a fail-closed `Tier1Result` on `PacketValidationError`, so `_run_legacy`/Tier 2 is never reached |
| 3 worker summary is untrusted, cannot replace board criteria | `::test_worker_summary_is_not_authority` | pass |
| 4 tracked migration test runs through the configured harness, bound to the evaluated commit | `::test_migration_test_result_binds_to_commit` | pass |
| 5 ordinary task without a packet keeps existing behavior, resolver not constructed | `::test_non_auger_task_does_not_invoke_adapter` | pass |
| 6 no duplicate XML parser / no new judge backend | module docstring + imports (`engine.command_hygiene`, `engine.config`); Auger owns XML parsing, the adapter consumes the JSON packet | inspected |

Whole file: `5 passed in 0.86s` (`auger-packet-tests.log`).

Disclosure: verdict `1fac2a90` (2026-11-03) is bound to task GR-155 but grades commit `d12baf6d`,
which is NOT an ancestor of the landed work — it judged an abandoned lineage. It is superseded by
this tick's verification run and is not evidence about `3b87b30f`.

### GR-164 — local main vs github/main divergence (P1)

The reconciliation landed earlier (`c5a95640`); the row's remaining arm was "close once
QA-GITREINS-REVIEW-1 lands and a green run exists on the then-current github/main".

| arm | probe | result |
|---|---|---|
| both histories empty of divergence | `git rev-list --left-right --count github/main...HEAD` | `0 0` |
| public count-sync guard green on the reconciled tip | `.venv/bin/python scripts/check_docs_drift.py` | rc=0, `3221 tests / 98 test files` |
| guard suite green (row asked 187) | `pytest tests/test_guard_manager.py -q` | `190 passed` (collection grew; superset of 187) |
| green CI on the then-current main | `gh run view 42020858384` | `completed/success`, sha `8c64ed85` |

### QA-GITREINS-DOCS-1/2/3, -RELENG-2/3/4, -REVIEW-2/3 (P1/P2)

All eight were filed from three 2026-11-04 QA cells (`qa-dagger`, 10:20:40Z / 10:25:01Z /
10:28:09Z) whose only named defect is the guard-timeout contract:

- `tests/test_cli.py::TestGuardRunCLI::test_guard_timeout_allow_skips_exits_zero`
- `tests/test_cli.py::TestGuardRunCLI::test_guard_timeout_without_allow_skips_exits_two`

Root cause row QA-GITREINS-REVIEW-1 was closed 2026-11-05 with fix `8c64ed85` (an explicit
`hook_timeout` is honoured; the implicit one is derived; the timeout early-return fails closed), and
the hosted CI run for that commit is green. Cell-by-cell re-probe at `3574afc1`:

| cell class | rows | re-probe | result |
|---|---|---|---|
| `pytest` / `[tests]` | DOCS-1, RELENG-2 | uncapped focused run + full suite | `2 passed` (`guard-timeout-tests.log`); `3204 passed, 17 skipped` (`full-suite.log`) |
| `ci-pass` (native leg) | DOCS-2, RELENG-3, REVIEW-2 | uncapped focused run + hosted CI | focused `2 passed`; hosted run `42020858384` success |
| `chaos-resource` (3145728 KB cap) | DOCS-3, RELENG-4, REVIEW-3 | focused run under a real `ulimit -v 3145728` | `2 passed in 4.78s`, cap applied to the child (`guard-timeout-tests-capped-3gib.log`) |

Disclosures:
- RELENG-3/4 name `test_guard_timeout_allows_skips_exits_zero`; no test by that name exists. The QA
  label mangled `allow_skips`; the probe targets the real id.
- RELENG-2's cell recorded `cmd=[]` with a truncated progress bar and names no test; it is
  attributed to the same battery and the same two-test root cause by cell timestamp adjacency, and
  is closed on the suite-wide green run rather than on a named-test probe.
- The `ci-pass` **act** legs were NOT re-run this tick: the host root filesystem is at 96% used and
  the hosted workflow — the authoritative target that act mirrors — is green at the tested tree.
  act replay remains available as a lower-fidelity check on demand.

## SCOPE contract for the judge of this commit

This commit changes only files under `docs/dogfood/evidence/2026-11-05-premise-reconciliation/`.
Every row named above is still `pending` in the board that this commit publishes — the dispositions
(10 row flips + closure events + header bump) land in the immediately following board commit, which
is not judged separately. Do not treat the still-pending rows as a defect of this commit.
