# GitReins Dogfood — PyPI Consumer Integration Report (2026-08-14)

**Verdict: 🔴 DOES-NOT-DELIVER (as released on PyPI) / 🟡 PROMISING-BUT-ROUGH (repo HEAD)**
**Run by:** coding-hermes-dogfood cron (deepseek-v4-flash)
**Repo:** /home/kara/gitreins-poc · **Package tested:** PyPI `gitreins==0.11.0` (2026-07-23) + repo HEAD (0.11.0+201 commits)
**Scratch area:** /tmp/dogfood-gitreins2/ (fresh venv, two consumer repos, throwaway secrets)

## Promise (null hypothesis)

> "A developer can `pip install gitreins`, run `gitreins install` / `init` in any
> repo, create tasks with completion criteria, have an agentic LLM evaluator judge
> them per-criterion, and commit through Tier-1 guards (secrets/lint/tests) that
> BLOCK secrets — with the fixes from the 08-03 dogfood shipped to users."

## What was actually done (real use, not tests)

1. **Fresh PyPI install**: `python3 -m venv fresh-venv && pip install gitreins` →
   clean, 0.11.0, mcp 2.0.0, pydantic 2.13.4 / pydantic_core 2.46.4. `gitreins --help` works.
2. **Consumer repo A (PyPI package)**: `git init`, wrote `calc.py` + `test_calc.py`,
   `gitreins install` (clean), `gitreins init` → generated `.gitleaks.toml` with
   BARE GLOBS (`*.log`, `*.spec.md`, `*.md`) → `gitreins guard` →
   **`✗ secrets — ○`** (gitleaks v8.30.1 regexp panic, exit 1) — the exact DF-001
   P0, on the released package. Lint/tests passed; the secrets guard is broken.
3. **Consumer repo B (repo HEAD, .venv)**: same steps → valid `.*\\.log` regexes,
   **guard PASS** (secrets clean + lint + tests + static_analysis).
4. **Secrets blocking**: committed `sk-...` key and `ghp_...` token through the
   hook as installed → **both PASSED** (hook resolved `gitreins` to a stale
   0.8.1 on PATH, not the 0.11.0 that installed). With `.venv/bin` first on PATH
   the same sk- commit was **BLOCKED** (`✗ secrets — sk.txt:1`, exit 1, redacted).
5. **Task lifecycle + judge**: `task create demo-task "Add calc feature" ...` →
   `task start` → `task complete` → tier1 PASS + tier2 LLM verdict per-criterion
   with file evidence (verdict bdcb3a41). Works end-to-end (repo HEAD).
6. **GR-099 live check**: `pip install --dry-run gitreins pydantic-core>=2.47.0`
   → `ResolutionImpossible` (pydantic 2.13.4 pins core==2.46.4 exactly). The
   blocked task is correct; the 13-day re-verification loop is the problem.

## Time-to-first-success & friction

- **PyPI path: NO first success.** First `gitreins guard` on a fresh install
  fails with `✗ secrets — ○` and no actionable message. A new user is blocked
  at their first commit, and the failure looks like the tool is broken.
- **Repo-HEAD path: ~10 min** to green guard + working judge (vs ~20 min on
  08-03 — the DF-001/DF-005 fixes removed the config-fighting).
- **Friction count: 7** — (1) P0 secrets guard broken on PyPI build; (2) P1 hook
  runs wrong-version gitreins via PATH; (3) P1 gitleaks-clean misses sk-/ghp_
  secrets (quiet coverage hole); (4) P2 `Language: unknown` on PyPI build
  (GR-GAP-026 fix also unreleased); (5) P2 GR-099 re-verified 13 days straight;
  (6) P2 no release pipeline / sdist-vs-HEAD drift undetected for 11 days;
  (7) P3 `gitleaks version` prints "version is set by build process" (no
  version string) — made the version pinning investigation harder.

## What held up (verified live)

- `pip install gitreins` — clean install, working CLI, correct `--help`.
- `gitreins install` — helpful output, exit 0, hook + config + .gitignore.
- `gitreins init` (HEAD) — smart language/size detection, backup files.
- Task lifecycle create/start/complete — works, persists across runs.
- **Tier-2 agentic judge — works end-to-end** (tier1 + tier2, per-criterion
  evidence, persisted verdict).
- **Secrets guard BLOCKS when the right code runs** — the flagship promise is
  real (repo HEAD, sk- key commit → exit 1 with redacted file:line finding).
- Lint and tests guards gate commits correctly.

## What fell apart

- **The released package ships the P0 bug the 08-03 dogfood found** (DF-010):
  201 commits, 166 fix/feat, zero releases since 07-23. Board is green, CI is
  green, users are broken. This is the "premature completion" anti-pattern at
  the release boundary: "all tests green" ≠ "users get the fix".
- **The hook is not hermetic** (DF-011): bare `gitreins` in the hook + PATH
  shadowing = a DIFFERENT harness version ran and let real secrets through.
  The user sees "guards PASS" while the guard is the wrong one.
- **gitleaks-first coverage is narrower than the fallback** (DF-012): with
  gitleaks installed, sk-/ghp_ patterns can pass; without it, the built-in
  scanner catches them. Binary coverage is a trap.
- **Idle ticks manufacture diligence** (DF-013): 14 consecutive ticks
  re-verifying a fact already proven blocked.

## Board tasks written (this run)

- **DF-010 (P0)** — release lag: 0.11.0 predates the DF-001 fix; 201 commits
  unreleased; no release pipeline in CI; sdist-vs-HEAD drift undetected 11 days.
- **DF-011 (P1)** — pre-commit hook calls bare `gitreins`; PATH can resolve a
  different version; real secrets committed through the stale hook (repro).
- **DF-012 (P1)** — gitleaks reports "no leaks found" for sk-/ghp_ patterns;
  cross-check with the built-in scanner; add regression tests.
- **DF-013 (P2)** — GR-099 parked-block semantics: verified_at + recheck
  condition; idle ticks should skip recently-verified blocked tasks.
- **DF-014 (P2)** — `Language: unknown` on PyPI 0.11.0 for plain-Python repos
  (GR-GAP-026 fix unreleased; ships with DF-010's release).

## How to reproduce (for the foreman)

```bash
# DF-010 (P0) — the user experience today
python3 -m venv /tmp/fv && /tmp/fv/bin/pip install gitreins
mkdir /tmp/scratch && cd /tmp/scratch && git init
printf 'def add(a,b):\n    return a+b\n' > calc.py
printf 'from calc import add\ndef test_add():\n    assert add(2,3)==5\n' > test_calc.py
/tmp/fv/bin/gitreins install && /tmp/fv/bin/gitreins init
grep -n "spec.md\|\.log" .gitleaks.toml          # bare globs present
PATH="$HOME/go/bin:$PATH" /tmp/fv/bin/gitreins guard   # ✗ secrets — ○ (panic)

# DF-011 — hook resolves a different gitreins
which gitreins                                    # ensure a different version precedes
git add -A && git commit -m "test"                # watch WHICH version the hook runs

# DF-012 — gitleaks misses the patterns the built-in scanner catches
printf 'T = "ghp_..."\n' > tok.txt && gitleaks detect --no-banner --source .
```

## Verdict reasoning

- **Does it work?** The promised workflow completes on repo HEAD (10 min to
  green guard + real judge verdict). On the released PyPI package it does NOT —
  first guard run fails permanently.
- **Is it useful?** Yes — the harness concept (criteria tasks + agentic judge +
  commit gates) demonstrably works; the judge output is genuinely useful
  evidence. This is the most useful piece of the fleet's tooling.
- **Is it usable?** For new users: NO right now (broken PyPI build). For
  repo users: good (pitfalls now documented in the skill).
- **Is it trustworthy?** Not yet: the hook can silently run a different
  version, and gitleaks-clean doesn't mean clean. The data (tasks, verdicts)
  survives restarts — that part is solid.

**Label:** 🔴 **DOES-NOT-DELIVER** for the released artifact (the thing users
actually get), 🟡 **PROMISING-BUT-ROUGH** for repo HEAD. The gap between them
(DF-010) is the finding.
