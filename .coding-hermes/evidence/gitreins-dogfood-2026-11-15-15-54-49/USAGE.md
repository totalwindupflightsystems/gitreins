# Using gitreins as a consumer — what the dogfood ticks actually hit

Derived from the real-use probes recorded in `realuse/` (first fire of tick
`gitreins-dogfood-2026-11-15-15-54-49`, HEAD `60eb8fac`) and the install battery in
`install-bunker/`. Every claim below is backed by an artifact in this directory; none of it is
copied from a README.

## 1. Warm the index before you trust a verdict

A consumer install that has never run `hilo` will answer a premise question from a stale index.

```bash
hilo init --no-hooks          # required in a fresh clone/worktree
hilo graph warm               # 17,367 ms on a 231-file repo, 3,673 edges, 72 stale pruned
gitreins pre-dispatch gate ... # only now is the manifest trustworthy
```

Cold, the same premise ("does `engine/preflight.py` implement the pre-dispatch premise check?",
ground truth: the file exists at `engine/preflight.py`) came back `probability 0.59`,
`evidence_quality 1`, `missing_kind implementation`, with a 9-file manifest that did not contain
the file. Warm, the same command returned `probability 0.96`, `evidence_quality 2`,
`missing_kind none`, and `manifest[1] = engine/preflight.py` at provenance `ast_exact`, score 1.0.
A consumer who skips the warm step will read a false negative as a real answer.

The root cause is tracked: `.vfs/graph/edges.jsonl` is the only file under `.vfs` in git, and it
holds 1,438 unique edges over **32 of the 53** tracked `engine/*.py` modules (rows `GR-225`,
`GR-226`).

## 2. Pick the surface explicitly

`gitreins pre-dispatch gate` on its default surface and with `--surface cli` are different
answers on the same input (`pf_default.json` vs `pf_cli_true.json`). When you script it, pass the
surface you actually mean.

## 3. `--json` is not uniformly `--json`

`gitreins report --json` emits parseable JSON (`report.json`). `gitreins doctor --json` does not —
`jq` fails at line 1, column 9 (`doctor.json`, row `GR-227`). Parse `report`; scrape or
`--no-color`-style handling for `doctor` until it is fixed.

## 4. Negative premises behave differently — use them as controls

A FALSE premise (`frobnicate`, a subcommand that does not exist) returned `probability 0.34`,
`missing_kind implementation` (`false1.json`). The separation from the TRUE premise is real, which
is why the 0.59 cold-run number above is a defect and not just conservative scoring.

## 5. A fresh machine installs cleanly — the suite does not pass

Verified on `bunker-mvp` (agent `030af8f7`, 2026-11-15): `pip install` of the tree gives
`gitreins-0.21.0`; `ruff check .` is clean; `pip install gitreins==0.20.0` (tag `v0.20.0`) then
HEAD also installs clean. `pytest` is **red on a clean machine**:

```
tests/test_cli_doc_sync.py:107  assert len(top_level) == 25   -> AssertionError: assert 26 == 25
tests/test_cli_doc_sync.py:161  AssertionError: FAIL: docs/cli-reference.md drifts from the live CLI
2 failed, 400 passed, 13 skipped in 47.76s
```

So `gitreins` is installable but not releasable from this HEAD: the CLI has 26 top-level
subcommands and `docs/cli-reference.md` documents 25 (row `GR-230`, CI recurrence
`INT-CI-3722`).

## 6. Serving it

`gitreins serve` returns a 14 KB HTML page at its root (`serve_root.html`); the battery's
`ui-probe` cell is `N/A` because the harness finds no web UI to drive, so treat the served page as
unverified beyond "it renders".
