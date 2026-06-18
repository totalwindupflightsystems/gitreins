# ADR-004 — Evaluator is an LLM Agent with Tool Access, Not Static Rules

> **Decision:** The Tier 2 evaluator is an LLM agent that iterates
> with up to 9 tools (`read_file`, `run_command`, `search_pattern`,
> `read_diff`, `get_task_item`, `sandbox_write/read`,
> `detect_dead_code`, `skylos_scan`) until it has enough evidence
> to deliver a structured JSON verdict. It is **not** a regex or
> rule set.
>
> **Status:** Accepted (2026-05-27). Implemented in
> `engine/evaluator.py` (663 lines, the longest file in the engine).

---

## Context

The job: given a list of natural-language criteria, judge whether
the code in the working tree actually meets them. The criteria
look like:

- "Accepts email+password as JSON body"
- "Returns 401 on invalid credentials"
- "Has tests for happy path and error cases"

These are not parseable. They require **reading code**, **running
tests**, and **applying judgment** about whether the test output
constitutes evidence of the criterion being met.

Three options:

- **A. Static rules.** A linter that pattern-matches the code for
  each criterion. Brittle: every new criterion is a new rule.
  Can't evaluate "is the error handling reasonable?"
- **B. Single LLM call.** Pass the criteria + the code to the LLM,
  ask for a verdict. No iteration, no tool access. Limited by the
  context window — for any non-trivial codebase, the relevant
  files won't all fit in the prompt.
- **C. Agentic loop.** The LLM iterates with tools. It decides
  which files to read, which commands to run, when it has enough
  evidence. **← what we shipped.**

## Decision

The evaluator is an LLM with a fixed tool set. The loop is in
`engine/evaluator.py:212-323`:

```python
def evaluate(self, task: dict) -> Verdict:
    self._sandbox.clear()
    messages = [
        {"role": "system", "content": EVALUATOR_SYSTEM_PROMPT},
        {"role": "user", "content": task_prompt},
    ]

    for iteration in range(self.max_iterations):  # default 15, max 20
        response = self.llm.chat(messages, tools=EVALUATOR_TOOLS)

        if not response.tool_calls:
            # LLM stopped calling tools — it has its verdict
            return self._parse_verdict(response.content)

        for tc in response.tool_calls:
            result, was_dup = self._execute_tool_with_dedup(tc)
            messages.append({role: tool, ...})

    # exhausted — force final verdict
    messages.append({"role": "user",
        "content": "You've reached the maximum number of tool calls. "
                   "Deliver your final verdict NOW. Output ONLY the JSON."})
    final = self.llm.chat(messages, tools=EVALUATOR_TOOLS)
    return self._parse_verdict(final.content)
```

The LLM is given a structured system prompt
(`EVALUATOR_SYSTEM_PROMPT`, lines 31-62) that:

- Lists the tools.
- States efficiency rules ("do not re-read the same file").
- Specifies the **exact** JSON verdict format.
- Sets the rule that PASS requires concrete evidence
  (file:line or test output) and FAIL requires explaining
  what's missing.

### 9 tools, in two groups

**Repo inspection (5):**
`read_file(path, offset?, limit?)`, `run_command(cmd)`,
`search_pattern(regex, file_glob?)`, `read_diff()`,
`get_task_item(id)`.

**Sandbox + analysis (4):**
`sandbox_write(key, content)`, `sandbox_read(key)`,
`detect_dead_code()` (Python AST), `skylos_scan()` (multi-language).

Each tool returns a JSON dict the LLM can read. The tools are
**not** given write access to the codebase — the evaluator is a
**judge**, not a fixer. A wrong file edit by the evaluator would
be very bad.

### Verdict contract

The LLM must output, **and nothing else**:

```json
{
  "verdict": "COMPLETE",
  "items": [
    {"criterion": "<exact text>", "status": "PASS",
     "detail": "file.py:42-68 implements POST /login"},
    {"criterion": "<exact text>", "status": "FAIL",
     "detail": "tests/test_auth.py:12 only tests happy path"}
  ],
  "summary": "2 of 3 criteria pass"
}
```

Parsing is 3-strategy: strip markdown fences → find `{...}`
boundaries → keyword fallback. Defaults on parse failure:
`verdict=INCOMPLETE`, `status=FAIL`. The defaults are
deliberately conservative — better to fail the build than to
silently pass with a malformed verdict.

## Why an agent loop and not static rules

### 1. Natural-language criteria are not parseable

The criteria come from humans (or other LLMs) and are written
in English: "Returns 401 on invalid credentials." A rule would
have to either: (a) require the user to write criteria in a
formal language, defeating the point; or (b) be written by us
in advance, which means it can't handle novel criteria.

An LLM can read the criterion and decide what evidence to look
for. That's the whole point.

### 2. Code doesn't fit in a single context window

For a real project, the relevant code for a single criterion
might be 1 file or 20 files. We don't know in advance. A
single-shot LLM call has to either send everything (expensive,
often impossible) or send nothing and guess (useless).

The agentic loop lets the LLM **discover** what to look at.
`search_pattern` finds the entry points; `read_file` reads
the relevant ranges; `run_command` runs the tests. The LLM
chooses the order.

### 3. The LLM is the right tool for "reasonable code review"

Tier 1 (static guards) catches the easy stuff: secrets, lint,
test failures, dead code. But "does this error handler actually
handle the case the criterion describes?" is a code review
question, and code review is what LLMs are good at.

The evaluator IS the code reviewer. Tier 1 is the checklist
that runs before the code reviewer is even invoked.

### 4. Evidence-based verdicts are auditable

The verdict format requires `detail: "file.py:42-68 ..."` for
each PASS. This means a developer who wants to dispute a
verdict can read the evidence and check it. A regex rule
that fails silently is much harder to debug than "the LLM
looked at the wrong file."

### 5. The cost is bounded

Max iterations: 15 (default), 20 (max). A typical DeepSeek-V3
or Haiku evaluation: 3–8 LLM calls, ~2–8 seconds, ~$0.001.
This is cheap enough to run on every task completion.

If the LLM goes in circles, the dedup logic
(`_execute_tool_with_dedup`, lines 325-349) injects
`_dedup_warning` to push it out. The system prompt reinforces
"do not re-read the same file." The max-iterations cap is
the hard stop.

## Trade-offs accepted

- ❌ **Non-determinism.** Two evaluations of the same code can
  produce different verdicts. (Mitigation: temperature is 0.1
  in `engine/llm.py:90`. For a fixed seed where the provider
  supports it, results are reproducible.)
- ❌ **Cost.** A real LLM call per evaluation. (Mitigation: cheap
  models — `deepseek-chat`, `gpt-4o-mini`, `claude-haiku-4-5` —
  run at <2s / <$0.01 per evaluation per
  `docs/technology-choices.md:27-28`.)
- ❌ **Hallucination risk.** The LLM can claim a file says
  something it doesn't. (Mitigation: Tier 1 dead-code scan
  and the `detect_dead_code` / `skylos_scan` tools give the
  LLM cross-references. The system prompt requires file:line
  evidence in every PASS detail.)
- ❌ **15-iter cap means incomplete verdicts on hard tasks.**
  (Mitigation: when the cap is hit, the evaluator forces a
  final verdict with a user prompt — see line 312-322. If the
  forced verdict is also empty, an `INCOMPLETE` default is
  returned, which fails the build rather than silently
  passing.)

## What lives where

| Concern | File | Lines |
|---------|------|-------|
| Loop body | `engine/evaluator.py` | 212-323 |
| System prompt | `engine/evaluator.py` | 31-62 |
| 9 tool definitions | `engine/evaluator.py` | 64-177 |
| Dedup tracking | `engine/evaluator.py` | 325-349 |
| Tool implementations | `engine/evaluator.py` | 380-663 |
| Verdict parser | `engine/evaluator.py` | 530-589 |
| Verdict dataclasses | `engine/evaluator.py` | 180-192 |

## What the evaluator is NOT

- It is **not** a code generator. No write/edit tools.
- It is **not** a debugger. It reads evidence and judges; it
  doesn't fix.
- It is **not** autonomous. It runs to a single verdict and
  returns. No persistent state across evaluations (sandbox
  is cleared on `evaluate()`).
- It is **not** deterministic. Two runs on identical inputs
  can produce different verdicts. This is a feature, not a
  bug — the LLM's variance is the same variance a human
  reviewer has.
