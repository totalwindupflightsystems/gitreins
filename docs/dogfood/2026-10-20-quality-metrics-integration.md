# Dogfood integration report — repo-produced quality metrics (`quality:`)

- **Run**: 23 · 2026-10-20 · lane `gitreins-dogfood` (skill `coding-hermes-dogfood`)
- **Target**: gitreins, HEAD `672bcfc` (also re-proved on the released **0.17.0** wheel)
- **Surface**: `docs/quality-metrics.md` — the one v0.17.0 surface no dogfood run had
  touched (0 mentions in `.coding-hermes/dogfood-log.md` before this run)
- **Verdict**: 🟡 **PROMISING-BUT-ROUGH** — the feature works as documented on the happy
  path and the machine surfaces are exact, but the surface a user actually reads when the
  gate fires (the console ✗ line) does not name the metric, and the artifact is the sole
  authority for its own target.
- **Rows**: DF-GITREINS-POC-94 … -98, PERF-1 (see "Findings")
- **Install leg**: RUN on `bunker-mvp` (fresh Ubuntu 24.04 / Python 3.12.3, agent
  `8fbf67db`) — `pip install gitreins` (PyPI 0.17.0) in a venv, then the documented
  workflow verbatim. Agent destroyed and verified gone.
- **No product code and no board row contents were changed** (append-only).

## The promise (null hypothesis)

From `docs/quality-metrics.md`: *a repository can point GitReins at one repo-owned
measurement command; GitReins runs it and consumes the JSON artifact it writes, showing
the same snapshot — value, target, stage and producer — on the guard, the judge,
`doctor`, the CLI and the MCP surfaces, with `warn` never failing anything and `block`
failing only a present numeric metric below its declared target.*

**Observed:** the promise holds — with one important exception on the reading surface,
and one trust assumption the doc asserts but the code does not enforce.

## What was actually done (real use, not the suite)

Scratch consumer repo outside the product tree
(`~/.hermes/stand-in/dogfood/gitreins/qm-scratch-20261020`): a `src/`+`tests/` Python
package with a `scripts/quality_metrics.py` producer and `quality/ladder.json`, exactly as
the doc's "Minimal `type_hint_pct` example" prescribes, then `.gitreins/config.yaml`
edited to the doc's `quality:` block (the block already exists after `gitreins install` —
see F6).

Working configuration (verified live):

```yaml
quality:
  enabled: true
  command: "python3 scripts/quality_metrics.py"
  artifact_path: ".gitreins/quality.json"
  targets_source: "quality/ladder.json"
  per_metric_mode:
    type_hint_pct: warn        # or: block
  timeout: 300
```

Working output (guard, warn mode, `gitreins 0.17.0`):

```
  ✓ quality — quality: type_hint_pct=72.3% (target 80%, warn, stage current, via python3 scripts/quality_metrics.py)
diff-scope: unavailable (producer artifact has no surfaces model)
```

Probe table (all commands run in the scratch repo unless stated):

| # | Probe (exactly what a user does) | Expected by the doc | Observed |
|---|---|---|---|
| Q1 | doc producer + config + `gitreins guard` | the line above | ✓ exactly as documented, exit 0 |
| Q2 | `gitreins doctor` | the same snapshot | ✓ same line, same numbers |
| Q3 | `per_metric_mode: {type_hint_pct: block}` with 72.3 < 80 → `gitreins guard` | guard FAILS naming the metric | ✗ **FAILS (exit 1) but the ✗ line reads `diff-scope: unavailable (…)`** — no metric, no target, no mode (**POC-94**) |
| Q4 | same, with a `surfaces` model in the artifact | — | ✗ the ✗ line becomes `✗ quality — surface COV-1: test_links=linked, …` — a *surface* row explains a blocked commit |
| Q5 | same run, `gitreins guard --json` | — | ✓ `checks[quality].summary` carries the full metric line; `outcome: fail` (so the loss is console-only) |
| Q6 | block mode, value above target (95 ≥ 80) | guard PASSES | ✓ `✓ quality … target 80%, block …`, exit 0 |
| Q7 | artifact target 10 while `quality/ladder.json` says 80, block mode | ladder is the authority | ✗ **guard PASSES, printing `target 10%`**; the ladder file is never opened (**POC-95**) |
| Q8 | artifact declares no `target` (block mode) | — | ✗ `target n/a` → `met_target: null` → never blocks |
| Q9 | `per_metric_mode: {}` with an unlisted metric | defaults to warn | ✓ `wiring_pct=10% (target 90%, warn, stage beta)` — displayed, no failure |
| Q10 | producer exits 1 / malformed JSON / no artifact / non-numeric value | `quality: unavailable` with a reason, never 0, never fails | ✓ all four exact: `command failed: boom`, `artifact unreadable: …`, `artifact not found: …`, `artifact contains no numeric metrics`; guard exit 0 |
| Q11 | producer hangs, `quality.timeout: 1` | bounded | ✓ `quality: unavailable (command timed out after 1s)`, guard wall 1.365 s |
| Q12 | `surfaces` + `class_floor` + one staged change | `diff-scope: N changed files, M surfaces touched` + per-surface line + warning for no test link | ✓ `diff-scope: 1 changed files, 1 surfaces touched` / `surface COV-1: test_links=linked, classes=unit, missing_classes=integration` / zero-link surface emits `WARNING: surface … has no test link` |
| Q13 | no surface matched | reported as `no_diff` | ✓ human line `diff-scope: no artifact surfaces touched`; JSON `diff_scope.status = "no_diff"` |
| Q14 | producer that stamps `produced_at` (the artifact contract's own example) + commit + run | — | ✗ tracked ` .gitreins/quality.json` modified after **every** run; `check-ignore` rc=1 (**POC-96**) |
| Q15 | diff-scope change count with that producer | count the user's change | ✗ `2 changed files` for one real change; the extra file is the harness's own artifact; the secrets lane prints `(excluded harness state: .gitreins/**)` (**POC-97**) |
| Q16 | `gitreins judge <task> --skip-tier2`, warn mode | PASS (warn cannot fail) | ✓ exit 0 (control) |
| Q17 | same, block mode | FAIL, reason visible | ✓ exit 1 — but the stage prints the quality block **twice**, marks none of the gates ✗, and `judge --json` has **no** `quality_snapshot` field (**POC-98**) |
| Q18 | MCP `quality.status` before any run | `not-computed`, *never* triggers the producer | ✓ exact, producer counter stayed 0 |
| Q19 | MCP `guard.run` then `quality.status` | identical snapshot, producer not re-run | ✓ identical dict, counter stayed 1 |
| Q20 | MCP `quality.status` with `quality.enabled: false` | `{"status": "disabled"}` | ✓ exact |
| Q21 | MCP `quality.status` with a nonexistent workdir | the documented refusal | ✓ `{"error": "workdir '/tmp/gitreins-typo' does not exist", …}`, nothing written |
| Q22 | pre-commit hook (`git commit`) with quality enabled | hook is the same gate | ✓ hook prints the quality line; a `block` miss refuses the commit (HEAD unchanged) |
| Q23 | fresh box: `pip install gitreins` (README quickstart) | works | ✗ PEP 668 refusal on fresh Debian/Ubuntu (known: POC-64) → venv route used |
| Q24 | fresh box: doc producer + config + `gitreins guard`, released 0.17.0 | documented line | ✓ `✓ quality — quality: type_hint_pct=72.3% (target 80%, warn, stage current, via python3 scripts/quality_metrics.py)` |
| Q25 | fresh box: block mode | blocking line names the metric | ✗ same console defect as Q3 on the **released wheel**, while the same run's `--json` carries the metric |

## Findings

- **POC-94 (P2)** — a quality FAIL prints the *last* line of the quality block on the
  console ✗ line instead of the metric line, so a blocked commit never names the metric,
  its value, target, mode or producer (console-only; `--json`/`doctor`/`verdict.json`
  carry it). `engine/types.py:646-662` (failed-check detail = last non-empty output line)
  × `engine/quality_metrics.py:249-291` (metric first, diff-scope after).
- **POC-95 (P2)** — `quality.targets_source` is parsed, echoed in the snapshot and never
  read: blocking follows the **artifact's own** target, so a block gate passes when the
  artifact lowers it (`target 10%` vs ladder 80 → PASS) or omits it (`target n/a`).
- **POC-96 (P3)** — `install` ignores 11 `.gitreins/` artifacts but not the quality one;
  with a timestamped `produced_at` the tree is dirty after every run and `git add -A`
  commits build output.
- **POC-97 (P4)** — the new diff-scope "N changed files" counts the harness's own state
  (`.gitreins/quality.json`); the secrets lane in the same run excludes `.gitreins/**`.
- **POC-98 (P4)** — judge surface: the quality block prints twice, the failing quality
  gate carries no marker, and `judge --json` exposes no structured `quality_snapshot`
  (only the persisted `verdict.json` has it), while the doc names `tier1.extra`.
- **PERF-1** — the producer runs synchronously on the commit path (numbers below).

## Perf (Step 2b) — measured, not guessed

Host: 16 cores, load avg 8.25 (shared fleet box) — the doc-producer delta is inside the
noise band; the third arm is far outside it.

```
hyperfine --warmup 3 --runs 20 'gitreins guard'        # quality disabled
  Time (mean ± σ):     480.5 ms ± 116.0 ms   [User: 196.9 ms, System: 73.3 ms]
hyperfine --warmup 3 --runs 20 'gitreins guard'        # quality enabled, doc's producer (~20 ms)
  Time (mean ± σ):     500.3 ms ± 114.5 ms   [User: 247.6 ms, System: 90.9 ms]   ← NULL (noise)
hyperfine --warmup 1 --runs 5  'gitreins guard'        # quality enabled, 2 s repo-owned producer
  Time (mean ± σ):      2.586 s ±  0.239 s   [User: 0.264 s, System: 0.128 s]    ← the wait is the child
cold (page cache dropped once, n=1): quality disabled 0.59 s / doc producer 0.47 s — no cold penalty
wedged producer, quality.timeout=2 (producer sleeps 30): guard wall 2.28 s,
  "quality: unavailable (command timed out after 2s)"  → at the default timeout=300 the
  same wedge occupies the commit path for up to 5 minutes.
```

Verdict: **the documented example producer is a null** (0.48 s → 0.50 s, inside noise) —
no PERF claim, no row. The envelope is the finding: a repo-owned measurement (the
documented use case: coverage / type-check / wiring) is added to **every** guard
invocation, and the pre-commit hook runs the same command, so a user doing
`gitreins guard && git commit` pays the producer twice. Hot path:
`engine/guard_manager.py:1756` → `engine/quality_metrics.py:47-55` (`subprocess.run`).

## Install leg (fresh machine)

- Host: `bunker-mvp` (ONLINE, 4/50 agents) — `bunker-las-03`, `-02` and `-04` were all
  OFFLINE (`deadline_exceeded` on their daemon API) at probe time; `bunker-mvp` was the
  substitute, recorded as such.
- Agent `8fbf67db`, TTL 1 h, fresh Ubuntu host, Python 3.12.3.
- Transferred 9 staged files per-file over ssh with `stat -c%s` size verification both
  sides (ok=7 at first transfer, 2 more for the block probe), then ran the doc's workflow.
- Results: README's literal `pip install --user gitreins` → PEP 668
  `externally-managed-environment` refusal (known, POC-64); the venv route installs
  **gitreins 0.17.0 in 16 s**; `gitreins install` → producer → config → `gitreins guard`
  reproduces the documented line, and the block-mode arm reproduces POC-94 on the
  **released wheel** (exit 1, `✗ quality — diff-scope: …`, while `--json` carries
  `quality: type_hint_pct=72.3% (target 80%, block, …)`).
- pytest is not installed by the wheel → the tests lane is skipped (documented DEGRADED
  behaviour, honest).
- Teardown: `bunker destroy 8fbf67db` → "Agent 8fbf67db destroyed", local SSH key removed;
  residue re-read after the ~90 s reconciler cadence shows no agent row.
- No visibility/permission change, no credential minted: the agent never cloned the repo;
  everything it needed was staged from the control host.

## The doc's own generator (three corrections a reader should have)

1. `install` already writes a `quality:` block with `enabled: false` — the doc says "add
   the following block", so a reader who appends instead of editing ends up with two
   `quality:` keys in one YAML file (PyYAML keeps the last, so the behaviour becomes
   edit-order dependent). Say "edit the block `install` wrote".
2. The sample line `via repo-producer` (line 58) is not what any build prints; line 70
   documents `via <command>`, which is what every real run prints.
3. `targets_source` appears in the doc's config with no statement of what it does — and
   the answer today is "nothing beyond an echo in the snapshot" (POC-95).

## Reproduction kit

Everything below is in `docs/dogfood/evidence/quality-metrics-2026-10-20/`:

- `probe-qm-A-J.txt`, `probe-qm2.txt`, `probe-qm3-surfaces.txt`, `probe-qm4-mcp.txt`,
  `probe-qm5-judge.txt`, `probe-qm6-refined.txt`, `probe-qm7-judge-clean.txt` — raw
  captures of every probe above, in order.
- `perf-quality-metrics.txt` — the hyperfine/timing runs verbatim.
- `install-leg-bunker-mvp.txt`, `install-leg-bunker-mvp-block.txt` — the fresh-box leg.
- `scratch-setup.md` — the scratch repo, the exact config and the producer used.
