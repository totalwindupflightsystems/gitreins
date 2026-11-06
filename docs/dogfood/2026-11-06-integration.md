# GitReins integration dogfood — 2026-11-06

Tick: `gitreins-dogfood-2026-11-06-12-08-29`
Lane workdir: `/home/kara/.hermes/stand-in/dogfood/gitreins` (stand-in board symlink only)
Product checkout: `/home/kara/gitreins` — HEAD `d7586fe283f4a427202ac27d662f53bc64955c79`,
`git rev-list --left-right --count HEAD...github/main` = `0	0`, dirty paths limited to the
generated `.vfs/graph/.symbols_cache.json`. A leftover `.git/REBASE_HEAD` (dated 2026-11-05,
no `rebase-merge`/`rebase-apply` directory) marks the live tree as an operation-marker boundary,
so all real-use probes below run in a fresh consumer repository, not in the product tree.
CLI under test: `/home/kara/gitreins/.venv/bin/gitreins` 0.18.0 (source HEAD).
Shipped-CLI note: the pipx `gitreins` on this host is 0.17.1 and its subcommand set lacks
`setup`, `disable`, `enable`, `uninstall`, `config-validate` — the release/deploy drift is
already covered by `scripts/check_deployed_surface.py` (README, v0.18.0 notes), not re-filed.

## Verdict: SHIPPABLE core, PROMISING-BUT-ROUGH on the new security_review lane

The gate does its job — a staged hardcoded token was refused, and the installed pre-commit hook
blocked the commit with HEAD unmoved. The v0.18.0 `security_review` pipeline, exercised as a
stranger would, has two real defects (a stage-wide tool gating rule and a silent-green coverage
gap) and two smaller reporting ones.

## The promise (from README + docs, not from tests)

A team installs GitReins into a repo, gets a versioned task with acceptance criteria, and every
commit passes static guards (secrets, lint, tests, static analysis) that the hook cannot be skipped
for. v0.18.0 adds a staged security pipeline (`rules` → `localize` → `judge`) that is off by
default and claims `semgrep and/or ast-grep` at stage 1 with "neither required".

## What was actually done (real use, not a test suite)

Fresh consumer project `/home/kara/.hermes/cache/scratch/gitreins-dogfood-20261106/consumer`
(git repo, `ledger` package with `store.py`, `report.py`, `tests/test_store.py`):

1. `gitreins install` → 0.24 s, wrote `.gitreins/config.yaml`, pre-commit + pre-push hooks,
   `.gitleaks.toml`, `.gitignore` entries.
2. `gitreins init` → 0.50 s, detected Python, `test_command: python3 -m pytest -x --tb=short`,
   enabled `static_analysis: [mypy, pyright]`. **On the file install had just written it printed
   three unknown-key warnings** (see finding GR-GAP-069).
3. Staged a realistic "ops helper" diff: hardcoded `ghp_…` token, `%`-formatted SQL,
   `subprocess.run(..., shell=True)`, `os.path.join` on a user segment, and later `eval()`.
4. `gitreins guard` (security_review **off**) → `Tier 1 Guards: FAIL`, rc 1:
   `✗ secrets — FAIL (gitleaks: 2 findings; builtin cross-check: 1 finding)` at
   `ledger/store.py:5`; lint ok; static_analysis ok; tests skipped (`pytest` not in `python3`).
5. Enabled `security_review: mode=staged, min_confidence=0.7` and ran the documented contract
   both ways (`missing_tool_policy: fail` then `skip`) with `semgrep` on PATH and `ast-grep`
   absent (a cargo-only install): both arms lose the graded rules scan (GR-GAP-067), and the
   arm that found the real `eval()` never names it on the console (GR-GAP-070).
6. Staged a diff with **no** rule-table pattern (SQLi, `shell=True`, traversal) →
   `Tier 1: DEGRADED PASS`, rc 0 (GR-GAP-068).
7. Attempted `git commit` through the installed hook → refused, rc 1, HEAD unmoved
   (`ac01c25`), which is exactly the promised behaviour.
8. `gitreins security-scan` on the staged set → `CVE-SIMULATED conf=0.00` with the
   `[heuristic mode (no ML stack installed) — keyword fallback, NOT a full scan]` suffix.
   **This documented limitation reproduced exactly as written** — the heuristic flagged a
   *comment* containing "vulnerability" while the real injection lines produced nothing.
   GR-157's premise is therefore documented-and-honest at the CLI surface, not a new finding.
9. Measured the commit gate (Step 2b): see `PERF-3`.

## Findings filed this tick

| Row | What |
|---|---|
| GR-GAP-067 | P1 — a missing *optional* stage-1 tool degrades the **whole** rules stage; semgrep-only host can never get a graded scan (`fail` blocks every commit, `skip` reports the stage skipped while semgrep ran) |
| GR-GAP-068 | P2 — security_review green on a diff carrying SQL injection, `shell=True` and path traversal; bundled rule inventory is 4 semgrep + 1 ast-grep pattern and is undocumented |
| GR-GAP-069 | P2 — install writes config keys its own validator rejects on every command, and two of them (`defaults.worktree_venv_source/_name`) are never read by the loader |
| GR-GAP-070 | P3 — console summary hides security_review findings (log-only) and truncates the lane reason mid-word |
| PERF-3 | P3 — commit gate 22.05 s ± 3.57 s warm on a 6-file repo at loadavg 33; ~2.0 s attributable to pyright+mypy, remainder unattributed; 63 child execs, 1113 failed execve probes |
| SKIPPED-INSTALL-BUNKER-1 | blocked — install leg skipped, no eligible isolated bunker host (see below) |

## Install leg (never a silent pass)

`bunker status --server bunker-las-03` → ONLINE (v0.1.4, `health` 404 = alive) but
`/tmp: HOST-SHARED` (no pam_namespace) and "Net mode: not reported". `bunker-mvp` → ONLINE
(v0.2.1, private /tmp) but `Net mode: shared … NOT tenant isolation`, and 3 agents running
including one created 12:39Z by another lane. Wrapper transport was healthy
(`BUNKER_QA_SERVER=bunker-las-03 bunker-qa.sh status` → `bunkerd=active docker=yes act=yes uidmap=yes`).
Per the skill, an ONLINE host with shared/unreported network isolation is not a valid
safety-proof host, so the leg is **SKIPPED-install-bunker: no eligible isolated host** —
no agent was spawned and none was destroyed. Row: `SKIPPED-INSTALL-BUNKER-1`.

## What a user would feel

- The gate itself: trustworthy. It blocked a real token and refused the commit; the hook is
  installed and armed with no extra step.
- `init` + first `guard`: 0.24 s / 0.50 s — fast, and the DEGRADED accounting is honest
  (skipped tests are named, not silently green).
- The new security lane: the weakest part. A semgrep-only host cannot get past it, a clean green
  covers far less than the name implies, and when it does block, the finding is in a log file.
- Cost: ~22 s per commit on a six-file repo is on the edge of what a person tolerates in a
  pre-commit hook; it is worth one attribution pass before it ships enabled anywhere.

## Evidence

Tick scratch: `/home/kara/.hermes/cache/scratch/gitreins-dogfood-20261106/`
(`consumer/` fixture, `perf/guard-warm.json`, `perf/components.json`, `perf/guard-strace.txt`,
`perf/execve.txt`, `file_rows.py`). Transcripts: `/tmp/gr-dogfood-guard-off.out`,
`/tmp/gr-dogfood-guard-staged.out`, `/tmp/gr-dogfood-guard-skip2.out`,
`/tmp/gr-dogfood-guard-skip-eval.out`, `/tmp/gr-dogfood-commit-attempt.out`,
`/tmp/gr-dogfood-secscan.out`.

No product code was changed by this tick. No repo visibility or permission was touched. No
credential was minted.
