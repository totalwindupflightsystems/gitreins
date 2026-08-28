# GitReins 0.12.0 — Fresh-User Integration Report (2026-08-27)

**Verdict: 🟡 PROMISING-BUT-ROUGH** — the core loop works end-to-end and the
harness provably catches real failures, but the published wheel has a lying
version string (P0), the secrets scan masks findings beyond the first per file
(P1), and the uv test-command path regresses the DF-002 root-package fix (P1).

This run used the **PyPI release path** exactly as a new user would: fresh
venv, `pip install gitreins`, install → init → guard → task → judge → commit.
No source checkout, no internal helpers. The consumer app is a small weather
CLI (`weather.py` + `tests/`, stdlib-only, injectable transport).

## The promise

> "Install gitreins into any git repo, create criteria tasks, work with your
> AI agent, complete tasks → Tier-1 guards (secrets/lint/tests) + a Tier-2
> agentic LLM evaluator that judges per-criterion, and commit through a
> pre-commit hook that blocks secrets."

## What worked (evidence)

| Step | Result | Time |
|---|---|---|
| `pip install gitreins` | 0.12.0 wheel installs clean (py3.11) | ~30s |
| `gitreins install` | writes `.gitreins/config.yaml`, pre-commit hook, `.gitignore` entry | 1s |
| `gitreins init` (empty repo) | honest "Language: unknown" warning, re-run after adding sources detects Python | 1s |
| First `gitreins guard` | 4/4 PASS after two genuine code fixes the harness caught | ~4 min total (incl. fixes) |
| Secrets guard | `sk-` key in staged file → FAIL, pre-commit hook **blocked the commit** (no commit created) | 1s |
| Tests guard | failing test → FAIL, commit blocked; after fix → PASS | 1s/run |
| `gitreins task create/start` | task lifecycle works, criteria list echoed | 1s |
| `gitreins task complete` | Tier 1 PASS + Tier 2 judge: 4/4 criteria PASS with code+tests evidence, verdict persisted | ~3.5 min |
| `gitreins commit` | guards run, commit created only on PASS | 1s |
| `gitreins report` | verdict history renders (1 entry, PASS) | 1s |
| MCP `guard_run` (cross-repo workdir) | full per-guard output incl. complete pytest log | 1s |
| MCP `judge_evaluate` (wait=true) | structured per-criterion verdict, PASS/COMPLETE | ~3 min |

The DF-011 hook-PATH-shadowing fix is confirmed in the released wheel: the
pre-commit hook now pins the **absolute path** of the gitreins binary that ran
`install` (with a comment referencing DF-011). The DF-001 gitleaks-regex fix
also holds — fresh init's allowlist no longer panics gitleaks.

## Frictions hit (in order)

1. **Version drift (P0, DF-015).** `gitreins --version` prints `gitreins
   0.11.0` although the installed wheel is 0.12.0 (dist-info METADATA says
   0.12.0; the wheel's `engine/version.py` statically says 0.11.0; repo HEAD
   uses dynamic `metadata.version`). Consequence: **every** command prints
   "Update available: 0.11.0 → 0.12.0" on the already-current install.
2. **ModuleNotFoundError on a standard layout (P1, DF-017).** Root-package
   layout (`weather.py` at root, `tests/` subdir, no pyproject.toml) fails
   `uv run pytest` collection; guard shows only "1 error in 0.07s". Fix on the
   user side: add `[tool.pytest.ini_options] pythonpath = ["."]` (what a real
   user would do — this is the DF-002 scenario resurfacing on the uv path).
3. **Tests-guard failure output is useless (P2, DF-018).** On failure the
   guard shows only the final pytest summary line — no failing test name, no
   traceback. I had to run `uv run pytest` manually twice to find the failing
   tests. (MCP `guard_run` returns the full pytest log — the CLI should too.)
4. **Secrets scan masks extra findings (P1, DF-016).** A file containing both
   `sk-...` and `ghp_...` reported 1 finding (the sk- key only); the ghp_
   token was silently invisible, though each is caught in isolation.
5. **init claims static analysis that doesn't run (P2, DF-019).** init prints
   "Static analysis: enabled (mypy, pyright)" but writes no
   `static_analysis_tools`; the guard then no-ops with "No static analysis
   tools configured for this language".
6. **uv version juggling (minor).** `uv run pytest` created its own project
   venv and ran pytest under Python 3.14 while the gitreins venv is 3.11 —
   surprising but harmless here.
7. **Hook pins the install-time binary path (minor).** The DF-011 fix pins
   the absolute venv path; if that venv moves, every commit fails with a
   confusing `command not found` from the hook.

## The working example (what a real user ends up with)

A weather CLI (stdlib only) guarded by GitReins:

```bash
mkdir weather-cli && cd weather-cli
git init -b main
python3 -m venv .venv && .venv/bin/pip install gitreins pytest
.venv/bin/gitreins install && .venv/bin/gitreins init   # re-run init after adding sources
# write weather.py + tests/test_weather.py
# pytest layout: add pyproject.toml with [tool.pytest.ini_options] pythonpath=["."]
git add .
.venv/bin/gitreins guard          # expect 4/4 PASS
.venv/bin/gitreins task create DFW-001 "Add --city-file" \
  "weather --city-file cities.txt prints one line per city" \
  "missing file exits 2 with stderr error"
.venv/bin/gitreins task start DFW-001
# ... implement ...
.venv/bin/gitreins task complete DFW-001   # Tier1 + Tier2 judge (~3 min)
.venv/bin/gitreins commit "feat: --city-file"
```

The full working consumer (app + tests, placeholder secrets only) is in the
scratch report of this run; the pattern above is the durable part.

## Time-to-first-success

~4 minutes from `gitreins install` to the first green `guard` (4/4 PASS),
including two genuine code bugs the harness caught and one pytest-layout fix
— i.e. the guard's blocking behavior is *working as intended*; the time is
dominated by the user-side fixes it forced.

## Judge quality (Tier 2)

The evaluator verdicts were evidence-cited per criterion (file:line + test
names) and matched reality: it correctly PASSed all 4 criteria of DFW-001
including the regression criterion, and its summary correctly noted the 9/9
test pass. No hallucinated claims in either CLI or MCP verdict.

## Board tasks filed

DF-015 (P0 version drift), DF-016 (P1 secrets first-finding-only),
DF-017 (P1 uv-path regression), DF-018 (P2 tests-guard failure detail),
DF-019 (P2 static-analysis claim vs no tools).
