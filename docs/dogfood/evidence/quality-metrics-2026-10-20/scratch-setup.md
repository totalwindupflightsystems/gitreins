# Scratch consumer repo for run 23 (quality-metrics dogfood)

Location on the control host: `~/.hermes/stand-in/dogfood/gitreins/qm-scratch-20261020`
(outside the product tree; nothing here is committed to gitreins).

## Layout

```
qm-scratch-20261020/
  .gitreins/config.yaml         # install baseline + the doc's quality: block
  .gitreins/quality.json        # the producer artifact (written by the producer)
  conftest.py                   # so pytest puts the repo root on sys.path (src/ layout)
  quality/ladder.json           # {"type_hint_pct": 80}
  quality/artifact.json         # the fixture the `cp` producer copies (probe control)
  scripts/quality_metrics.py    # the doc's example producer, verbatim
  src/calc.py
  tests/test_calc.py
```

## How it was built (the exact user path)

```bash
mkdir qm-scratch-20261020 && cd qm-scratch-20261020 && git init -q .
git config user.email dogfood@example.test && git config user.name "Dogfood User"
git config core.hooksPath .git/hooks          # the host's global core.hooksPath would hijack commits
mkdir -p src tests scripts quality .gitreins
# src/calc.py, tests/test_calc.py, conftest.py            (see the files in this repo's evidence run)
# quality/ladder.json = {"type_hint_pct": 80}
# scripts/quality_metrics.py = the doc's "Minimal type_hint_pct example", verbatim
export PATH="/home/kara/gitreins/.venv/bin:$PATH"         # gitreins 0.17.0 (editable checkout)
gitreins install                                          # writes .gitreins/config.yaml + hooks
python3 scripts/quality_metrics.py                        # the doc's step: run the producer once
# edit the quality: block that install wrote (do NOT append a second one):
#   enabled: true / command: "python3 scripts/quality_metrics.py"
#   artifact_path: ".gitreins/quality.json" / targets_source: "quality/ladder.json"
#   per_metric_mode: {type_hint_pct: warn|block} / timeout: 300
gitreins guard
```

## Probe controls used

> The archived copies of the probe scripts in this directory were re-formatted once by the
> repo's own `ruff` (line wrapping plus five `# noqa: E501` on over-long string literals)
> so that committing them does not fail the repo's lint gate; the logic is unchanged, and
> the raw output captures next to them are the runs these scripts produced.

- **Deterministic producer** (used to isolate the artifact-pollution effect):
  `command: "cp quality/artifact.json .gitreins/quality.json"` — the artifact's bytes are
  whatever `quality/artifact.json` holds, so the value/target/`surfaces` under test are
  controlled by the probe instead of by the example producer's hardcoded 72.3.
- **Timestamped producer** (the artifact contract's own `produced_at`):
  `datetime.datetime.now().isoformat()` written each run — used for POC-96/POC-97.
- **Failure producers**: `sh -c 'echo boom >&2; exit 1'` (non-zero exit),
  `sh -c 'echo {not-json > .gitreins/quality.json'` (malformed), `true` (no artifact),
  `sh -c 'sleep 5; …'` with `timeout: 1` (timeout).
- **Counting producer** (MCP leg): increments `quality/counter.txt` in addition to writing
  the artifact, so "never triggers the producer" is a number, not a claim.

## Measured environment

Control host: 16 cores, load average 8.25 during the perf runs (shared fleet box) — see
`perf-quality-metrics.txt` for the caveat. Fresh box: `bunker-mvp` agent `8fbf67db`,
Ubuntu host, Python 3.12.3, PyPI `gitreins==0.17.0`.
