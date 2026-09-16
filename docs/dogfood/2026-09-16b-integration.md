# GitReins Dogfood — 2026-09-16b (qa lane): wheel-verification run

**Lane:** gitreins-poc-qa · **Verdict:** 🟡 PROMISING-BUT-ROUGH · **Install leg: RUN** (las-bunker-03, agent 696a61d3, spawned + destroyed cleanly)

**Promise:** `pip install gitreins` (now 0.13.0), `install`/`init` in any repo, criteria tasks,
per-criterion agentic judge, guards that block secrets on commit.

**This run's job:** 0.13.0 hit PyPI at 05:41Z today — hours after the morning dogfood (which
skipped the bunker leg: las-bunker-03 host down). This tick verified the *shipped artifact*
instead of repo HEAD, and re-tested every past P0/P1 as regressions.

## Leg 1 — fresh install of the 0.13.0 wheel (bunker agent 696a61d3, bare Debian 13, Python 3.13.5)

- README step 1 `pip install gitreins` → **PEP-668 externally-managed wall** (exit 0 but
  nothing installed). Same docs friction as 09-15; README's "New to GitReins?" onboarding link
  is below the fold. Resourceful-user path: `python3 -m venv` → `pip install gitreins` →
  **13 s** to working CLI. `gitreins --version` = **0.13.0 correct** (DF-015 stays fixed).
- `install` + `init` on an empty repo: init announces `pytest -x --tb=short` and persists
  exactly that (POC-3/D mismatch stays fixed). Static analysis off on unknown language.
- `guard` on empty repo: PASS exit 0, clean-tree skips visible as `✓ tests` + a loud gitleaks-
  fallback warning (POC-11 known gap, unchanged).
- **Regression battery on the wheel:**
  - secrets masking (POC-16): sk- + ghp_ in one file → **2 findings, file+line, rule names** — fixed.
  - hook pin (DF-011): pre-commit calls the **absolute venv path** — fixed.
  - README `--depends-on` example parses verbatim (POC-13) — fixed.
  - **exit codes**: `guard` FAIL → 1; `task complete --skip-tier2` FAIL → 1, PASS → 0 (POC-10 stays fixed).
  - **real pre-commit hook blocked a real-shape ghp_ token** (exit 1) and passed the clean
    commit once pytest was installed.
  - MCP stdio server answers initialize + tools/list on the wheel.

## Leg 2 — HEAD self-host (control box)

- Anti-tamper canary: a **real-shape ghp_ token (40 chars) staged and committed through the
  control hook → BLOCKED (exit 1)**. DF-012 holds.
- **The first canary (33-char `ghp_`+29) sailed through both scanners** — correct behavior:
  gitleaks' official github-pat rule requires the 36-char suffix. My fixture was malformed.
  Lesson: fake secrets in tests must be exact-shape, or you "prove" a hole that isn't there
  (and file a false P0 on yourself).
- Scanner-divergence note (P3, DF-GITREINS-POC-15): on a `ghp_` token that only gitleaks'
  shape-strict rule matches, guard-with-gitleaks FAILs while guard-without-gitleaks (built-in
  fallback) PASSes. Coverage depends on which scanner runs; nothing tells you which one did.

## NEW confirmed at HEAD (both legs): POC-12 is NOT fixed

`task complete` (and `gitreins judge`) tier1 runs the **secrets scan only**
(`stages.tier1.steps == ['secrets']` in verdict.json — verified at HEAD on a scratch repo
and on the 0.13.0 wheel). A failing test (`test_broken.py`, staged) + missing pytest →
`Overall: PASS ✓` exit 0 from the judge on the same tree where `guard` FAILs exit 1.
The 09-15 "fixed at HEAD" conclusion was confounded: the scratch repo had untracked secrets
on disk, and the judge — unlike guard — scans the **whole worktree**, so its secrets step
FAILed for the right reason and made the missing-tests gap invisible. Discriminator verified
this run: untracked leak present → judge FAIL; leak removed → judge PASS with the failing
test still staged.

## Friction count: 8 (PEP-668 README path; clean-tree skip display; judge/guard tier1 divergence; judge misses tests; scanner-coverage ambiguity; bare-`pytest` fresh-env failure; version-serverInfo drift; noisy/loud-but-passing init)

## Board tasks

- DF-GITREINS-POC-15 [P3] scanner-selection ambiguity — gitleaks vs built-in divergence is
  invisible to the user (shape-strict ghp_ only caught when gitleaks runs).
- DF-GITREINS-POC-16 [P1] judge tier1 runs secrets-only — reopens POC-12 as designed
  behavior; guard and judge disagree on the same tree.

## What a fresh user should actually run (working quickstart for Debian 13)

```bash
python3 -m venv ~/.venvs/gr && ~/.venvs/gr/bin/pip install gitreins   # NOT bare pip (PEP-668)
cd your-repo && git init -q && git commit --allow-empty -m init       # hook needs a repo
~/.venvs/gr/bin/gitreins install && ~/.venvs/gr/bin/gitreins init
~/.venvs/gr/bin/pip install pytest                                     # tests guard needs it
```
