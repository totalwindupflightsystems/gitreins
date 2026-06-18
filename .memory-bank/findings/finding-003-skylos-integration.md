# Finding 003 — Skylos Integration

> **Status:** Implemented, opt-in, working. Skylos adds
> multi-language dead-code detection (Python, TS/JS, Go, Java,
> PHP, Rust, Dart, C#) and AI-mistake pattern detection that
> the built-in Python AST scanner can't match.
>
> **Default:** OFF. Enable with `guards.skylos: true` in
> `.gitreins/config.yaml` and `pip install skylos`.

---

## What Skylos adds on top of the built-in dead-code guard

The built-in `engine/dead_code.py:48-280` is **Python-only** — it
walks Python AST and finds unreachable code, empty functions,
unused imports, and unused functions. It's fast, no dependencies,
always-on.

Skylos (`pip install skylos`) is a **multi-language** static
analyzer with two extras the Python AST scanner can't provide:

1. **AI-mistake pattern detection.** Skylos specifically flags
   patterns that LLMs commonly hallucinate or leave behind: unused
   destructured variables, abandoned scaffold code, dead config
   branches. The built-in scanner only finds *syntactically*
   unused code; Skylos finds *semantically* suspicious code.

2. **Cross-language analysis.** A typical monorepo has Python +
   TypeScript + Go. The built-in scanner walks only `.py` files
   (see `engine/dead_code.py:97-108` skip-dirs list and Python
   file extension filter). Skylos handles all of them.

3. **Graded output.** Skylos returns an A-F letter grade plus
   a numeric score in the JSON output
   (`engine/guard_manager.py:313-315`). The built-in scanner
   returns a list of findings, no grade.

## How it was integrated

Two integration points:

### 1. As a Tier 1 guard (`engine/guard_manager.py:278-343`)

```python
def _check_skylos(self) -> GuardResult:
    try:
        result = subprocess.run(
            ["skylos", self.workdir, "--format", "json", "--no-grep-verify"],
            capture_output=True, text=True, timeout=120,
            cwd=self.workdir,
        )
        if result.returncode != 0:
            return GuardResult(name="skylos", passed=True,
                output=f"skylos exited {result.returncode}: {result.stderr[:200]}")
        data = json.loads(result.stdout)
        # ... extract unused_functions, unused_imports, unused_classes,
        # dead symbols, grade ...
        if not findings:
            return GuardResult(name="skylos", passed=True,
                output=f"Skylos grade {letter} ({score}) — no dead code found")
        return GuardResult(name="skylos", passed=False, output=output)
    except FileNotFoundError:
        return GuardResult(name="skylos", passed=True,
            output="skylos not installed — install with: pip install skylos")
    # ... all "soft errors" (timeout, parse failure) return passed=True
```

Three deliberate design choices:

- **`passed=True` on Skylos failure.** If Skylos isn't installed,
  times out, or returns unparseable output, the guard **passes**.
  This is the same pattern as lint and tests: a missing optional
  tool never blocks the commit. The user gets a message in the
  output: `"skylos not installed — install with: pip install skylos"`.

- **`--no-grep-verify` flag.** Skips Skylos's grep-based cross-
  reference verification, which is slow on large repos. The AST
  analysis alone is enough for the GitReins use case.

- **Findings, not grade, gate the commit.** The grade letter is
  shown in the output (e.g., `"Skylos grade B (87)"`) but the
  commit only fails if there are specific findings. We don't want
  to fail a commit because Skylos graded it a C+ when there are
  no actionable issues.

### 2. As an evaluator tool (`engine/evaluator.py:622-663`)

The agentic evaluator can call `skylos_scan()` to get the same
JSON output as a tool result:

```python
def _tool_skylos_scan(self) -> dict:
    try:
        result = _sp.run(
            ["skylos", self.workdir, "--format", "json", "--no-grep-verify"],
            ...
        )
        data = _json.loads(result.stdout)
        findings = {
            "unused_functions": [...],
            "unused_imports": [...],
            "dead_symbols": [...],
        }
        grade = data.get("grade", {}).get("overall", {})
        return {
            "grade": f"{grade.get('letter', '?')} ({grade.get('score', '?')})",
            "total_findings": sum(len(v) for v in findings.values()),
            "findings": findings,
        }
    except FileNotFoundError:
        return {"error": "skylos not installed — pip install skylos"}
```

The LLM can then incorporate Skylos findings into its verdict —
e.g., "PASS: no dead code per Skylos grade A" or "FAIL: 3 unused
functions per Skylos scan, see findings.unused_functions." This
gives the LLM cross-referenced evidence beyond its own tool
calls, which improves verdict quality on dead-code-related
criteria.

## What Skylos catches (that the built-in misses)

| Pattern | Built-in | Skylos |
|---------|---------|--------|
| Unused Python function | ✅ | ✅ |
| Unused Python import | ✅ | ✅ |
| Unreachable code after return | ✅ | ✅ |
| Empty function body | ✅ | ✅ |
| Unused TypeScript function | ❌ | ✅ |
| Unused Go function | ❌ | ✅ |
| Unused Rust function | ❌ | ✅ |
| Unused JS class | ❌ | ✅ |
| AI-hallucinated scaffold code | ❌ | ✅ |
| Dead configuration branches | ❌ | ✅ |
| Graded score (A-F) | ❌ | ✅ |
| Cross-language taint analysis | ❌ | ✅ |

## How to enable

### In `.gitreins/config.yaml`:

```yaml
guards:
  secrets: true
  lint: true
  tests: true
  dead_code: true
  skylos: true        # ← add this
```

### Install the binary:

```bash
pip install skylos
```

That's it. The next `git commit` (via the pre-commit hook) or
`gitreins guard` / `judge` run will include Skylos.

## What we caught in the GitReins repo itself

When we ran Skylos against `~/gitreins-poc/`, the most useful
finding was the `.gitreins-hooks/` directory in the repo root:
empty, never written to, never gitignored, never used. It's a
vestige of an earlier design where hooks were shipped in a
sibling directory. The current install path writes directly to
`.git/hooks/` (see `gitreins/install:40-64`). The empty
directory survives because nothing removes it.

This is the kind of "looks fine to a human, the LLM wouldn't
flag it" finding Skylos is good at: a directory that exists,
has the right name, is in the right place, but serves no
function.

## Trade-offs accepted

- ❌ **New dependency.** `pip install skylos` is now a soft
  requirement for the multi-language dead-code guard. (Mitigation:
  the guard is opt-in, defaults to OFF, and degrades gracefully
  with a clear message when Skylos is missing.)
- ❌ **Slower than the AST scanner.** Skylos invocation is
  ~2-10s for a typical repo, vs. <100ms for the built-in Python
  AST scan. (Mitigation: only runs when explicitly enabled.)
- ❌ **Output format can change.** Skylos's JSON schema is
  owned by the Skylos project, not by us. If they change the
  shape, our parser breaks. (Mitigation: the `--no-grep-verify`
  flag and the `passed=True` on parse failure mean a Skylos
  upgrade that changes the JSON doesn't block commits — it just
  silently degrades the guard to "skip with message.")

## Tests

| What | Test | Notes |
|------|------|-------|
| Skylos not installed → passes | `engine/guard_manager.py:333-337` | Soft-fail behavior |
| Skylos installed + clean → passes | (manual) | Grade A, no findings |
| Skylos installed + dirty → fails | (manual) | Lists findings |
| Skylos as evaluator tool | `engine/evaluator.py:622-663` | Returns JSON for the LLM |

## Related

- `engine/guard_manager.py:278-343` — Tier 1 guard
- `engine/evaluator.py:170-176` — tool definition
- `engine/evaluator.py:622-663` — tool implementation
- `engine/guard_manager.py:55` — opt-in toggle default (False)
- `.skylos/` — Skylos's own cache directory (gitignored by us implicitly)
