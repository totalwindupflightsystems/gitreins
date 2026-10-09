## 2026-10-09: Disposable Worktree Verification + Fresh-Machine Install

**What we tested:** The `worktree fresh` / `repro` / `clean` verification
surfaces on the control host, and the documented fresh-machine install path
end-to-end on an ephemeral bunker agent (las-02, after las-03 was down).

**What worked:**
- `worktree fresh` is the right primitive for "does this command pass at HEAD
  in a tree nothing else has touched": real detached worktree, evidence JSON
  with per-run exit codes and durations, auto-reap on pass. Full pytest
  collect ran in 4.9s cold.
- `worktree repro -k 3 --concurrency 2` gives honest flake attribution:
  3/3 pass recorded with pass_rate in JSON; when a command fails by design,
  rc=1, 0/3, and (with `--keep-failures`) trees survive for inspection.
- Composes with real repo tooling: `fresh` ran the repo's own
  `check_docs_drift.py` (5.0s) and a targeted 36-test suite (7.7s) — both
  green, evidence JSON written.
- Fresh install: clone → venv → `pip install -e .` in **17s**, then
  install/init/doctor all conform to docs/onboarding.md. `guard` on the
  fresh clone grades the builtin-secrets fallback correctly (never a fake
  green on an ungraded lane — the allow_skips DEGRADED contract from the
  README holds in behavior).
- Transient spawn failure (`slice-limits: containment landing did not
  converge`) on bunker-las-02 succeeded on a straight retry — matches the
  documented spawn-retry rule; don't attribute it to the host on first sight.

**What did not (rows filed):**
- **POC-80 (P2):** `repro --keep-failures` trees are invisible to
  `worktree clean`. Kept trees stay registered in `.gitreins/disposable.json`
  and appear in `git worktree list`, but a later `worktree clean` reaped
  nothing and reported nothing about them — only manual
  `git worktree remove` clears them. Same blind spot for a repro run killed
  pre-write (disposable.json pre-registered, empty results).
- **POC-81 (P2):** `gitreins preflight` hard-codes the `predispatch`
  resolution surface. In this repo (`cli: true, predispatch: false` by
  JEVRES-006 design) every question ABSTAINs with decision=dispatch, while
  `gitreins resolve` answers the identical question RESOLVED 0.85. A foreman
  following preflight's help text gets a silently dead signal.
- **POC-82 (P2):** fresh-install `task complete` without LLM config exits 0
  and leaves the task in_progress. `--skip-tier2` is named only inside the
  hint; the exit code says success while the task state says otherwise
  (follow-up to POC-78; CLI side still dead-ends a fresh user).
- **POC-83 (P3):** the fresh-clone guard's aggregate line reads plain
  `Overall: PASS ✓` with 3 skipped lanes + a builtin fallback; per-lane
  markers are honest but the last line a script reads lacks DEGRADED.

**The right way (what the next agent should do):**
- Flake attribution: `gitreins worktree repro --cmd '<cmd>' -k 3
  --concurrency 2 --json /tmp/out.json` — read the JSON, don't parse stdout.
- Pre-dispatch checks in repos that keep `predispatch` off: use
  `gitreins resolve --json` (cli surface), not `preflight`.
- Fresh-machine testing: bunker-las-03 has NO GitHub credentials — clone via
  https from a host that can (las-02 confirmed working), or tar-over-ssh.
- Always end with `bunker destroy <id> --server <name>`, even after failures;
  spawn failures are retried once before blaming the host.

**Install leg:** RUN on sibling host bunker-las-02 (las-03 offline, bunker4
offline). 17s install, full lifecycle green, agents destroyed. Provenance row:
SKIPPED-install-bunker-2026-10-09-las-03.
