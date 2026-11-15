# gitreins dogfood — 2026-11-15-15-54-49 — evidence index

Diagnostics trail for the two fires of tick `gitreins-dogfood-2026-11-15-15-54-49`.

| fire | when | what it produced |
|------|------|------------------|
| first delivery | 2026-11-15, commit `70af0aa1` | rows `GR-225`..`GR-230`, log section "first delivery" |
| second fire (this one) | 2026-11-15, duplicate fire | this evidence trail + a fresh, re-run install battery; **0 new rows** |

The first fire recorded its `realuse/` measurements only in a disposable scratch tree and left
its install-bunker ledger nowhere that could be re-read. This directory fixes the retention gap
and adds a battery this fire actually walked.

## realuse/ — product probes from the first fire (re-derived here, not re-run)

All files were produced against a hardlink-free clone of HEAD `60eb8fac` with the consumer
install `gitreins` from that same tree. Numbers below were re-read out of the artifacts in this
fire with `jq`; nothing here is quoted from the log prose alone.

| file | what it proves | re-derived value |
|------|----------------|------------------|
| `true1.json` | cold run of the exact-file premise "does `engine/preflight.py` implement the pre-dispatch premise check?" | `probability 0.59`, `evidence_quality 1`, `missing_kind implementation`, 12 seeds, 9-file manifest with no `engine/preflight.py` |
| `true1_warm.json` | same premise after `hilo init --no-hooks && hilo graph warm` | `probability 0.96`, `evidence_quality 2`, `missing_kind none`, 5-file manifest |
| `pf_default.json` | pre-dispatch gate on its default surface | gate output captured, no timing field in-band |
| `pf_cli_true.json` | same gate with `--surface cli` | gate output captured |
| `false1.json` | FALSE premise (a `frobnicate` subcommand that does not exist) | `probability 0.34`, `missing_kind implementation` — the negative control answers differently from the TRUE premise |
| `doctor.json` | `gitreins doctor --json` | **not JSON**: `jq` fails at line 1, column 9 (row `GR-227`) |
| `report.json` | `gitreins report --json` | valid JSON, small summary payload |
| `serve_root.html` | `gitreins serve` root page as fetched | 14 KB HTML document |

Timing note: the log's `5,547 ms` cold / `3,217 ms` warm figures were measured with wall-clock
shell timing around the CLI call. The verdict JSON carries **no** duration field
(`to_entries | select(key|test("ms|time|dur"))` returns `[]`), so those two numbers are
shell-measured and are not re-derivable from the artifacts in this directory.

## install-bunker/ — install leg, walked in THIS fire

Host `bunker-mvp`, agent `030af8f7`, ttl 4h, driven by `bunker-qa.sh launch` + repeated
`bunker-qa.sh collect` (the battery was adopted by collect with
`COLLECT-ADOPT reason=recent tick-death-orphan` on every poll; no cell was lost and all 24 rows
were pulled before teardown). Host census for this fire: `bunker-las-03` reachable and 36 G free
but spawn failed at `slice-limits` (`containment landing: memory.swap.max = "max", want "0"`);
`bunker-las-02` 100 % full; `bunker-las-04` publickey denied; `bunker-mvp` 5.4 G free, spawn OK.

Ledger: `qa-evidence-bunker-mvp-030af8f7.jsonl` (24 rows). Per-cell logs: `cell-logs/`.

| cell | verdict | evidence |
|------|---------|----------|
| `fresh-install` | **OK** | venv + pip install of the synced clone, `Successfully installed ... gitreins-0.21.0 ...` |
| `lint` | **OK** | `ruff check .` rc=0 |
| `upgrade` | **OK** | `pip gitreins==0.20.0` (tag `v0.20.0`) then HEAD install clean |
| `tests` | **FAIL** | rc=2; `cell-logs/tests.log`: `2 failed, 400 passed, 13 skipped ... in 47.76s` |
| `ci-pass` | **FAIL** | native rc=2 on the same two tests; act leg failed earlier on `No such image: catthehacker/ubuntu:act-latest` (**harness infrastructure**, not product) |
| `chaos-resource` | **FAIL** | 3 GiB cap applied (`child_saw=3145728 subshell_saw=3145728`); the only failures under it are the same two doc-sync tests, so this cell is red from the product defect, not memory pressure |
| `chaos-disconnect` | **OK** | `NETWORK_CUT_APPLIED`, fails fast |
| `chaos-errorpath` | **OK** | missing-config probe exits rc=1 naming the missing `.gitreins/config.yaml`, in a scratch repo with `HOME` isolated |
| `docker_build`, `compose_config`, `docker-deploy` | **N/A** | no Dockerfile, no compose file |
| `ui-probe` | **N/A** | no web UI detected by the harness |
| `chaos-shutdown`, `chaos-corruption` | **N/A** | no compose file, no db/state files in repo |

The two red tests, quoted from `cell-logs/tests.log`:

```
tests/test_cli_doc_sync.py:107: >       assert len(top_level) == 25
                                E       AssertionError: assert 26 == 25
tests/test_cli_doc_sync.py:161: E       AssertionError: FAIL: docs/cli-reference.md drifts from the live CLI
```

That is row `GR-230` (and the CI recurrence `INT-CI-3722`) reproduced on a clean machine, not a
new finding.

## UNVERIFIED / superseded

- The first fire's own install ledger (agent `f84c7529`, described as 24 rows) **cannot be
  re-read**: no `bunker-evidence*.jsonl` for that agent exists under the tick scratch tree,
  `~/.hermes/cache` (depth 2) or `~` (depth 2). Its cell verdicts are therefore inherited as
  *reproduced*, not as *retained*: this fire's battery independently reaches the same cell
  verdicts with a retained ledger.
- `5,547 ms` / `3,217 ms` / `17,367 ms`: shell-measured in the first fire, not present in any
  artifact here.
- Teardown of agent `030af8f7`: confirmed absent from `bunker list --server bunker-mvp --status all`
  after collect; port 8699 has zero listeners; no `/proc/*/cwd` under the tick scratch tree.
