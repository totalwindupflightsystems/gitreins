# ADR-003 — Tiered Guard Pipeline (secrets → lint → tests → dead_code → skylos)

> **Decision:** Run a fixed Tier 1 guard sequence in a specific order
> (secrets, lint, tests, dead_code, optional skylos) and only invoke
> the Tier 2 LLM evaluator if Tier 1 succeeds. The order and the
> Tier 1 → Tier 2 split are the point of the architecture.
>
> **Status:** Accepted (2026-05-27). Implemented in
> `engine/guard_manager.py` and `engine/judge.py`.

---

## Context

The original design question was: *where do the quality gates live?*
Three options were considered:

- **A.** One LLM call that judges everything from "did you commit a
  secret" to "does the feature work."
- **B.** Static-analysis-only (no LLM, ever).
- **C.** Tiered: static checks first (fast, free, deterministic),
  LLM only if static passes. **← what we shipped.**

The Tier 1 / Tier 2 naming was deliberately chosen to match the
design intent: Tier 1 is the **hard gate** that runs in <100ms and
catches the embarrassing stuff; Tier 2 is the **judgment layer**
that needs an LLM to read code semantically.

## Decision

### Tier 1 — Static, in this order

`engine/guard_manager.py:58-78` runs guards in the order they're
appended to `results`:

```
1. _check_secrets()       — gitleaks or built-in pattern scanner
2. _check_lint()          — ruff or flake8 on staged Python files
3. _check_tests()         — pytest via the test_command
4. _check_dead_code()     — AST-based Python dead-code scan
5. _check_skylos()        — multi-language dead code via Skylos (opt-in)
```

`Tier1Result.passed = all(r.passed for r in results)` (line 77).

### Tier 2 — Agentic LLM evaluator

Triggered **only** if Tier 1 passes. Lives in
`engine/evaluator.py:AgenticEvaluator` and is invoked from
`engine/judge.py:_run_legacy` (line 82-94) or
`engine/pipeline.py:_run_ai_eval` (line 273-317).

```
Tier 1 PASS → run AgenticEvaluator → Verdict (COMPLETE / INCOMPLETE)
            → judge.passed = (verdict == "COMPLETE")
```

### Why this order?

The order is **cheapest-AND-most-severe first**:

1. **Secrets first.** A leaked API key is the worst-case failure
   (rotating a key costs money and time). It is also the cheapest
   check — the built-in scanner reads only the staged files and
   applies a regex, <50ms.

2. **Lint next.** Style issues are cheap to detect (single tool
   invocation) and indicate probable code-quality issues. If lint
   fails, the agent probably didn't run it locally — a signal
   worth surfacing before we spend a full LLM evaluator call.

3. **Tests third.** Tests are slower (start Python interpreter,
   run pytest) but deterministic. We need to know tests pass
   before we trust the LLM's verdict.

4. **Dead code fourth.** An AST scan — fast and gives the LLM
   additional evidence ("yes, I checked for unused functions and
   found none") in its `detect_dead_code` tool result.

5. **Skylos last, opt-in.** Multi-language, slower, requires
   `pip install skylos`. Off by default to keep the no-install
   path fast and dependency-free.

If any guard fails, the rest still run (in the parallel pipeline
default: `on_fail: continue`). This is deliberate — the
evaluator's job is to evaluate, and it benefits from seeing all
the failure context. A guarded commit that fails Tier 1 never
reaches Tier 2 (because Tier 1 is `block` for `on_fail` when used
as a commit gate via `.git/hooks/pre-commit`).

### Why Tier 1 first, period?

- **Cost.** Tier 1 is free and <100ms. Tier 2 is one or more LLM
  API calls — typically $0.001–$0.01 per evaluation with DeepSeek
  or Haiku. Running Tier 2 only when Tier 1 passes is the single
  biggest cost-saver in the system.

- **Determinism.** Tier 1 has a binary outcome. There is no LLM
  to second-guess the secrets scanner. The agent can't argue
  with `gitleaks`.

- **Speed.** Pre-commit runs on every commit. <100ms is invisible
  to the developer. A 2–30s LLM call is a tax that adds up.

- **Auditability.** Tier 1 results are structured
  (`GuardResult(name, passed, output, error)`). They're easy to
  log, summarize, and feed into the LLM prompt as context.

### Why `on_fail: continue` (not `block`) on individual script steps?

Looking at `.gitreins/config.yaml:33-46` and the default
`engine/pipeline.py:393-401`, individual `script` steps in the
`tier1` parallel stage use `on_fail: continue`. The **stage** as
a whole still gates the next stage (Tier 2 only runs if
`stage.tier1.passed`).

The reason: when LLM evaluation is about to run, you want it to
see **all** the failures, not just the first one. The LLM
evaluator can write a better verdict ("2 lint warnings + 1
failing test + 3 unused functions") than re-running the pipeline
three times. The pre-commit hook, by contrast, is a hard gate
and uses `block` semantics.

## Trade-offs accepted

- ❌ Two layers mean two places to debug when a commit is
  blocked. (Mitigation: the `guard.run` MCP tool and
  `gitreins guard` CLI both show all Tier 1 results with a
  summary line per guard.)
- ❌ Tier 1 false positives block the agent. (Mitigation: the
  secrets scanner has a 7-line whitelist; see
  `findings/finding-004-precommit-guard-false-positives.md`.)
- ❌ Lint and test guards require their respective tools
  installed. (Mitigation: both are gated on `FileNotFoundError`
  and degrade to `passed=True, output="tool not found — skipped"`,
  see `guard_manager.py:222-225` and `:230-232`.)

## What lives where

| Concern | File | Lines |
|---------|------|-------|
| Guard order | `engine/guard_manager.py:58-78` | `run_all` |
| Secrets scanner | `engine/guard_manager.py:80-192` | `_check_secrets` + `_builtin_secrets_scan` |
| Lint | `engine/guard_manager.py:194-225` | `_check_lint` |
| Tests | `engine/guard_manager.py:227-250` | `_check_tests` |
| Dead code | `engine/guard_manager.py:252-276` | `_check_dead_code` (delegates to `engine/dead_code.py`) |
| Skylos | `engine/guard_manager.py:278-343` | `_check_skylos` (opt-in) |
| Tier 1 → Tier 2 glue | `engine/judge.py:69-94` | `_run_legacy` |
| Default pipeline | `engine/pipeline.py:381-428` | `load_pipeline_config` |
| Live pipeline | `.gitreins/config.yaml` | 63 lines |

## How to extend

- **Add a new Tier 1 guard:** add a `_check_*` method, add an
  entry to `self._enabled`, and append the call in `run_all`.
  Update `.gitreins/config.yaml` if it's opt-in.
- **Re-order the existing guards:** edit `run_all` in
  `engine/guard_manager.py:62-75`. The order in `results` is
  the order of evaluation.
- **Skip Tier 2 even when Tier 1 passes:** remove the
  `evaluator.evaluate(task_dict)` call from
  `engine/judge.py:_run_legacy` (line 91) — but then the
  per-criterion evidence goes away, which defeats the point
  of having criteria in the first place.
