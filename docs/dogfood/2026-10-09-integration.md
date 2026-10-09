# Dogfood Run 19 — 2026-10-09: disposable worktree verification surfaces + fresh-machine install

**Surfaces (new this run, untouched by runs 1-18):** `gitreins worktree fresh`,
`gitreins worktree repro`, `gitreins worktree clean`, and the fresh-machine
install path (clone → venv → `gitreins install` → `init` → `guard` → task
lifecycle) on an ephemeral bunker agent.

## Promise

"A user verifies a command in a clean detached worktree (`worktree fresh`),
repeats it N times for flake attribution (`worktree repro`), and a fresh user
installs gitreins from a clean clone and runs the full lifecycle."

## What actually happened (control host, HEAD e6d081d, binary `gitreins 0.16.0` from the repo venv)

- `worktree fresh --cmd 'pytest --collect-only -q ...'` — exit 0 in 4.9s,
  2900 tests collected. Evidence JSON written and correct (`passes`/`exit_code`/`duration_s`).
- `worktree repro -k 3 --concurrency 2` — 3/3 passed, 4.6-5.1s per run, trees
  reaped automatically, pass_rate 1.0 in the JSON.
- `repro --keep-failures` with a command forced to fail on run 2: 0/3, rc=1,
  three trees kept for inspection. **Finding POC-80:** `worktree clean` then
  reported nothing about them — kept trees are invisible to the reaper.
- `fresh` running the repo's own docs-drift check and a targeted 36-test
  suite: both exit 0 (7.7s). The surfaces compose with real repo tooling.
- `preflight` in a repo where only `resolution.enabled.cli` is on (this repo,
  per JEVRES-006): ABSTAIN/surface-disabled for EVERY question, decision
  always dispatch, while the identical question resolves RESOLVED 0.85 via
  `gitreins resolve`. **Finding POC-81** — the surface key is hard-coded.

## Fresh-machine leg (ephemeral bunker)

Named host bunker-las-03 offline (ssh timeout); bunker4 also down; bunker-las-02
and bunker-mvp healthy. First spawn on las-02 failed transiently
(`slice-limits: containment landing did not converge`) and succeeded on retry
per the spawn-retry rule; a second agent on bunker-mvp was spawned and
destroyed unused.

On agent d1ab64e0 (fresh Debian user, Python 3.13.5, no pip3 on PATH):
- `git clone https://github.com/totalwindupflightsystems/gitreins.git` — OK, HEAD e6d081d
- `python3 -m venv .venv && .venv/bin/pip install -e .` — **17s**
- `gitreins install` → rc 0, pre-commit hook installed; `gitreins init` →
  detected Python, static analysis disabled-by-default; `gitreins doctor` →
  all 19 allowlist entries compile.
- `gitreins guard` on fresh clone: PASS with secrets=builtin fallback
  (gitleaks not on PATH), lint/tests/lsp skipped. **Finding POC-83:** the
  aggregate line says plain PASS with no DEGRADED marker.
- Task lifecycle: `task create`/`start` OK. `task complete` without LLM key
  exits 0, leaves the task in_progress, names `--skip-tier2` only in the
  hint (POC-82, follow-up to POC-78). `--skip-tier2` then completes with
  verdict 663fc8af.

Both agents destroyed; keys removed.

## Verdict

🟢 SHIPPABLE for the surfaces tested. The verification surfaces do exactly
what a flake-attribution user needs and their JSON evidence is honest
(exit codes, durations, per-run trees). Install path is fast and the docs
match reality. The rough edges are lifecycle UX (POC-80/81/82/83), not
core function.

Perf: cold `worktree fresh` full collect 4.9s; warm disposable runs 4.6-5.1s;
docs-drift 5.0s; 36-test targeted suite 7.7s. Nothing a user would notice —
no PERF rows filed.
