---
name: gitreins-judge-tuning
description: >-
  How to tune what you PASS to, and what you REQUEST from, the GitReins
  Tier-2 LLM judge: acceptance criteria a judge can actually verify, the
  context it sees and how to bound it, the evaluator caps (iterations, time,
  input/output tokens, per-call tokens, tool-call weight, compaction, code
  context budget, file scope, fast-track), model selection by context need,
  the judge pre-screen (resolution gate) and its token ceiling, measured cost
  and wall-clock, and the diagnostic order when a verdict FAILs, times out or
  returns "Cap exceeded". Load it before creating a task whose criteria will
  be judged, before raising caps to "make the judge pass", or when a judge run
  is too slow or too expensive. Complements gitreins-usage (lifecycle/flags),
  it does not repeat it.
version: 1.0.0
category: software-development
---

# GitReins Judge Tuning — Criteria, Context, Caps, Cost

The Tier-2 judge is an **agentic loop**, not one LLM call: it reasons, calls
tools, reads results, and stops when it issues no tool call. Everything you
tune is either (a) the input you hand it — criteria and pre-loaded context — or
(b) a budget on that loop. Get (a) right and you rarely touch (b).

Verified at HEAD `abc7f7c` (0.15.0). Anything not checked against code or a
command run on this box is marked *(unverified)*.

## The levers, their defaults, and where they are read

All defaults live in `GitReinsDefaults` (`engine/config.py:45-61`) and are
overridden per-repo under `evaluator:` in `.gitreins/config.yaml`
(`engine/config.py:146-164`).

| Lever | Default | What it actually meters | Where |
|---|---|---|---|
| `max_iterations` | `100.0` (`-1` = unlimited) | LLM reasoning turns; each tool call also charges `tool_call_weight` | `engine/config.py:46` |
| `max_time` | `-1.0` = **unlimited** | wall-clock for the whole evaluation | `engine/config.py:47` |
| `max_input_tokens` | `10_000_000` (10M) | **cumulative** prompt tokens per context window; `200k`/`1.5M` forms accepted | `engine/config.py:48` |
| `max_output_tokens` | `131_072` (128K) | **session** output budget (cumulative), *not* the per-request value | `engine/config.py:49` |
| `max_tokens_per_call` | `16384` | the per-request `max_tokens` actually sent to the provider — **this one is provider-clamped** | `engine/config.py:50`, `engine/evaluator.py:1222-1225` |
| `tool_call_weight` | `0.1` | iterations charged per tool call (a 100-iter cap ≈ ~1000 tool calls) | `engine/config.py:51` |
| `compaction_threshold` | `0.90` | share of `max_input_tokens` of *cumulative* input tokens at which the conversation is rebuilt | `engine/config.py:53-54` |
| `code_context_budget` | `0.70` | share of `max_input_tokens` allowed for the pre-loaded code block | `engine/config.py:55`, enforced `engine/evaluator.py:1135-1147` |
| `file_scope` | `"changed"` | which files the judge may `read_file`/`search_pattern` | `engine/config.py:56`, `engine/evaluator.py:1123-1127` |
| `fast_track` | `"auto"` | `on`/`off`/`auto`; `auto` turns it **on** at ≥20 source packages | `engine/config.py:57`, `engine/evaluator.py:498-550` |
| `max_file_bytes` | `131_072` | cap on one `read_file` result | `engine/config.py:58-60`, `engine/evaluator.py:496` |

Three override paths, in priority order:

1. **Env** (highest) — `GITREINS_MAX_ITERATIONS`, `GITREINS_MAX_TIME`,
   `GITREINS_MAX_INPUT_TOKENS`, `GITREINS_MAX_OUTPUT_TOKENS`
   (`engine/eval_cap.py:484-498`). Only these four have env overrides; there is
   **no** env for `max_tokens_per_call`, `file_scope`, `compaction_threshold`,
   `code_context_budget` or `fast_track` — those are config-only.
2. **Per call** — MCP `judge.evaluate` takes individual `max_iterations` /
   `max_time` / `max_input_tokens` / `max_output_tokens` params, or the legacy
   combined string `eval_cap: "100/30m/200k/50k"`
   (`docs/mcp-api.md:233`; parser `engine/eval_cap.py:325-388`).
3. **`.gitreins/config.yaml`** under `evaluator:`.

Caps are checked **before** each call, so a run can end slightly above its cap
(the final call is let through) — `docs/evaluator-loop.md:18-19`.

### The provider clamp on `max_tokens_per_call` — and why it does not save you

`_PROVIDER_MAX_OUTPUT_TOKENS` (`engine/llm.py:279-284`): `deepseek` **393 216**,
`openai` / `anthropic` / `openrouter` 1 000 000. `_clamp_max_tokens`
(`engine/llm.py:286-303`) downgrades an over-limit value with a warning log.

Verified live (pure function, no network):

```
LLMClient._clamp_max_tokens(1_000_000, "deepseek") -> 393216   # + warning logged
LLMClient._clamp_max_tokens(1_000_000, "openai")   -> 1000000
LLMClient._clamp_max_tokens(16384,     "deepseek") -> 16384
```

**The clamp is keyed on the client's provider *string*, and in production that
string is never `deepseek`.** The hint passed at the call site is
`self.provider` (`engine/llm.py:312`), which is set either from an explicit
`provider=` argument or, when that is absent, from endpoint auto-detection —
and auto-detection only ever yields `"anthropic"` or `"openai"`
(`engine/llm.py:146-152`). Every production constructor passes no provider at
all: `engine/pipeline.py:800,802,967`, `gitreins/cli.py:1408,2293,2394,2571,2884`,
`gitreins_mcp/server.py:111,449`, `engine/worktree_manager.py:1252`. A judge
pointed at `api.deepseek.com` therefore gets the hint `"openai"`, so the clamp
returns 1 000 000 untouched and the raised value goes out on the wire. The
`393_216` entry is effectively dead code.

`GITREINS_LLM_PROVIDER` looks like the escape hatch and is not:
`engine/llm.py:12` documents it as "Force provider", the MCP `configure` tool
writes it into the environment (`gitreins_mcp/server.py:445`, advertised at
`:170`), but `LLMClient.__init__` never reads it — it reads
`GITREINS_LLM_REASONING` (`engine/llm.py:143-144`) and nothing else of that
family. Setting it changes nothing.

**Correction to older guidance:** "setting `max_output_tokens: 1M` causes HTTP
400 on every judge call" is **wrong twice over**. (1) `max_output_tokens` is the
session budget; it never reaches the provider — `max_tokens_per_call` does
(`engine/evaluator.py:1222-1225`). (2) But the replacement claim — "even
`max_tokens_per_call: 1M` is clamped to 393 216 on DeepSeek instead of 400-ing"
— is **also wrong**: the clamp does not fire for a DeepSeek endpoint, because
the hint is `"openai"`. Racing a deepseek `/chat/completions` call with
`max_tokens` above 393 216 is a genuine HTTP 400 waiting to happen. Do not
raise `max_tokens_per_call` "to fix truncation"; the per-call default 16384 is
already generous for a verdict payload, and raising it only risks longer,
more-expensive turns.

## What the judge is given, and what bounds it

`evaluate()` builds a system prompt + one user turn, then loops
(`engine/evaluator.py:1111-1169`):

- **Criteria**, numbered, injected into the task prompt; the judge is told to
  call `get_task_item()` first (`docs/evaluator-loop.md:401-402`) and to record
  each finding under `sandbox_write("verified_<index>", …)`
  (`engine/evaluator.py:119-126`).
- **Pre-loaded code context** — `git diff HEAD` hunks capped at 500 lines
  (`engine/evaluator.py:639-646`), or, in `test_mode: full`, up to 20 changed
  files × 200 lines (`engine/evaluator.py:661-667`). Data-file hunks are
  stripped (`_is_data_file_path`, `engine/evaluator.py:103`).
  On a clean tree with no diff it falls back to the last-commit anchor
  instead of claiming "no changes detected" (`engine/evaluator.py:614-623`).
- **Tier-1 LSP diagnostics**, when Tier 1 produced any
  (`engine/evaluator.py:1102-1113`).
- **The pre-screen block**, when the resolution gate answered
  (`engine/evaluator.py:1117-1118`).

The `code_context_budget` cap is a **character-count approximation**, not a
tokenizer: `ctx_est = len(code_context) // 3` and the block is truncated
head-first to `int(max_input_tokens * code_context_budget)` estimated tokens,
with a disclosure line (`engine/evaluator.py:1135-1147`). Consequently the cap
is only meaningful when `max_input_tokens` is finite — with the unlimited
sentinel `-1` no truncation happens at all (`engine/evaluator.py:1136-1140`).

**Tool-result bounds** (what actually accumulates and costs you):

| Source | Bound |
|---|---|
| `run_command` | 30 s timeout, output capped at 4 KB (`docs/evaluator-loop.md:161-165`) |
| `read_file` | first 400 lines when the file >12 KB and no range requested (`docs/evaluator-loop.md:143-145`); hard `max_file_bytes` 128 KB (`engine/evaluator.py:496`) |
| `search_pattern` | 200-match cap; files >500 KB skipped (`docs/evaluator-loop.md:181-184`) |
| Stored step evidence in the verdict | `MAX_EVIDENCE_CHARS = 4000`, head+tail with FAILED/ERROR lines hoisted (`engine/evidence_bounds.py:26-38`) |

The judge's **system prompt already tells it the pre-loaded block is
authoritative** ("you do NOT need to call read_file() on files shown below",
`engine/evaluator.py:141`), and dedups `read_file`/`run_command`/
`search_pattern` repeats with a `_dedup_warning` (`docs/evaluator-loop.md:295-320`).
Budgeting therefore starts with *not making the judge re-read what it was
handed*.

## Writing criteria a judge can actually verify

The judge treats criteria **literally** and explores until it can cite
evidence. Vague scoping is the #1 cause of both false FAILs and blown budgets.

Rules that hold up in practice:

1. **Name the exact path, extension and content pattern.** "Assembled files
   have trace headers" makes the judge grep the wrong extension and conclude
   the symbol is missing; `.speclang/assembled/*.code.ts files start with
   // spec:trace` gets a correct verdict.
2. **One criterion → at most one file read, or a count.** Prefer countable,
   command-verifiable statements: "count of `*_handler.go` equals count of
   `RegisterRoutes` calls in `serve.go` — use `ls | wc -l` and `grep -c`".
   Every criterion that reads a whole file adds its bytes to the cumulative
   input meter.
3. **Any criterion mentioning tests / build / lint / type-checking / "it
   works" MUST be checkable by running a command** — this is a hard rule in
   the prompt, not a suggestion: the judge must quote `exit_code` and the
   decisive line, and a detail of just "tests pass" is a FAIL
   (`engine/evaluator.py:148-177`, `docs/evaluator-loop.md:60-81`). Phrase the
   criterion so the command is unambiguous ("`go test -count=1 ./...` exits 0",
   not "tests pass").
4. **State the criterion in the form the judge will re-emit it.** The verdict
   item's `criterion` must carry the exact criterion text
   (`engine/evaluator.py:180-190`); long, multi-clause criteria make partial
   PASS/FAIL ambiguity more likely.
5. **An unjudgeable criterion looks like**: a project-wide sweep ("all N
   handlers are registered"), a superlative with no test ("the API is
   performant"), a criterion about a system the judge cannot reach (a live
   service it cannot start), or one whose only evidence is *outside* the file
   scope it is allowed to read (`file_scope: changed` rejects reads outside the
   changed set with `File not in scope: …`, `engine/evaluator.py:1566`). The
   criterion is not wrong — it is unscoped. Split it or restate it as a
   countable check.

**How many.** Keep tasks small: the sources converge on **~4 criteria per
task** as the sizing rule for anything beyond a toy repo, and each criterion
should be checkable in 1-2 reads. Criteria that each demand a full file read
scale the input meter linearly; splitting a 7-criterion task into 4+3 covers
the same ground with two cheaper, more reliable verdicts *(measured per-
criterion iteration burn: ~8-12 iterations/criterion non-trivial — prior
reports, **unverified** at HEAD)*.

## Judging a big repo cheaply

The single most effective lever is `file_scope`. It is already the default
(`changed`, `engine/config.py:56`): the allowed set is computed from
`git diff --cached` + `git diff` plus mapped test files and config files
(`_compute_allowed_files`, `engine/evaluator.py:767-851`), and both `read_file`
and `search_pattern` are gated to it (`engine/evaluator.py:1566, 1713, 1752,
1838`). `run_command` stays unrestricted because tests must run anywhere.

Cheap-judging recipe, in order:

1. **Grade the diff, not the repo.** `file_scope: changed` (default) + `diff`
   test mode + a criterion set that talks about the changed files' names. The
   pre-loaded block and the allowed set then agree with the criteria.
2. **Use `file_scope: full` only when the criteria genuinely span the tree**
   (small repos, cross-cutting invariants) — on a 100+ package monorepo it is
   how the loop drowns before producing output.
3. **Grade without persisting when it is a review, not a record** — `gitreins
   judge --ephemeral --title … --criterion …` evaluates inline criteria and
   persists **nothing**: no task entry, no verdict history, no branch, no
   stash (`gitreins judge --help`; `gitreins/cli.py:3510-3535`). Add
   `--persist-verdict` only when a judge-gated `worktree merge` must find
   `.gitreins/verdicts/verdict.json`.
4. **Bound the loop explicitly for a quick check**: `GITREINS_MAX_ITERATIONS=12
   GITREINS_MAX_TIME=8m` (env beats config), or per call via MCP `eval_cap:
   "20/5m/200k/50k"`.
5. **Turn fast-track on** for a mechanical diff: `fast_track: on` narrows the
   judge to changed lines and immediate callers instead of a deep call-graph
   walk (`engine/evaluator.py:498-550`; the instruction text is injected at
   `engine/evaluator.py:973-978`).

## The pre-screen (resolution gate) — what it costs, what it buys

`docs/jev-resolution-gate.md` §3.4 / JEVRES-004: before the expensive loop, the
judge can ask Jev one batched question over the criteria — *"is the repo's own
code sufficient evidence that each criterion is satisfied?"* — and the answer
rides into the prompt as **input only** (`engine/prescreen.py:9-21, 1117-1118`).
It never skips Tier 2 and never overrides the judge's authority
(`engine/prescreen.py:15-17`).

- Enabled by `evaluator.prescreen` which **defaults True**
  (`engine/evaluator.py:1077`), but the surface it needs —
  `resolution.enabled.judge_prescreen` — **defaults false**
  (`engine/config.py:122`, `engine/resolution.py:215-217`). So on a stock
  install every judge run attempts a pre-screen, gets a fast
  `surface-disabled` ABSTAIN (~0.1 s), logs one line
  (`WARN_TEMPLATE`, `engine/prescreen.py:68`) and runs the judge unchanged.
  **This is the correct default** — an uncalibrated band must not drive a
  pre-screen, and the repo's own config keeps it `false` for exactly that
  reason (`.gitreins/config.yaml:69-71`).
- Budget: the bundle is capped at **`MAX_BUNDLE_TOKENS = 28 000`**
  (`engine/resolution.py:102`), 2 k under the measured server wall (~33 k
  input tokens rejects with HTTP 400 `max_tokens_exceeded`,
  `engine/resolution.py:98-101`). The token estimate uses a calibrated
  3.5 chars/token (`engine/resolution.py:104-116`) — *not* the 3 used in the
  evaluator's own `code_context_budget` maths.
- Criteria are clipped to `MAX_CRITERION_CHARS = 600` each in the question
  (`engine/prescreen.py:73`, `build_prescreen_question`), so one pathological
  criterion cannot eat the bundle.
- Per-criterion attribution is deterministic (`_MISSING_DELTA`, max −0.25 for
  `missing_kind: implementation`), not a second model opinion
  (`engine/prescreen.py:85-101`).

Use it when you want "which criterion is weakly evidenced?" answered cheaply;
leave it off until JEVRES-005 calibration lands.

## Measured cost and wall clock

**Tokens** — `.gitreins/usage.jsonl` is one JSON line per evaluation step
(`step: "tier2"`), counters cumulative for the current context window, so sum
**deltas** and expect a **drop** at each compaction
(`docs/evaluator-loop.md:322-345`). Real rows from this repo (read at HEAD):

```
{"tokens_in": 102576,  "tokens_out": 1599,  ... "step": "tier2"}
{"tokens_in": 130635,  "tokens_out": 3081,  ... "step": "tier2"}
{"tokens_in": 811509,  "tokens_out": 6502,  ... "step": "tier2"}
{"tokens_in": 4533986, "tokens_out": 13613, ... "step": "tier2"}
```

So a verdict on this repo spans **~100 k to ~4.5 M input tokens** and
**~1.6 k to ~14 k output tokens** — input is where the money is, and the
spread is 40x. Cost = those tokens × your model's price; the file carries no
model or cost field, only tokens, so join it to
`.gitreins/history/<date>/<hash>/verdict.json` by timestamp when you need
per-task attribution. Judge spend **never** shows up in the invoking agent's
telemetry — GitReins calls its own LLM client
(`docs/evaluator-loop.md:343-345`).

**Wall clock** — the judge pays for real `run_command` work, so time
correlates with the repo's test suite, not with repo size:

- small repo, Tier 2 ≈ **3.5 min**; a 6-test task via MCP ≈ **7 min**
  (deepseek-v4-flash; prior dogfood measurements — **unverified** here, they
  need a live model call).
- this repo hit its `max_time` at **45 m 9 s** re-running a 259 s suite per
  iteration, which is why the local config raised it to `60m`
  (`.gitreins/config.yaml:47-48`).
- reported failure mode: a judge with no verdict for **50+ minutes** while a
  sibling test process contended for the machine (field report, **unverified**).
- `max_time: -1` (the engine default, `engine/config.py:47`) is **unlimited** —
  fine for a batch host, dangerous unattended. Always set a wall clock in
  config or env when a human is not watching.

**Concurrency is a real constraint.** Three concurrent judges plus DB
contention produced INCOMPLETEs that solo async re-judges cleared (field
report). Serialize judge runs on one host, or give each its own worktree and a
generous `max_time`.

## When the judge FAILs, times out, or says "Cap exceeded"

Fix the cheapest, most-likely cause first — and note that "Cap exceeded: …"
and "Evaluator returned empty response" are **different failures with different
fixes** (`docs/evaluator-loop.md:31-44`; empty response has no budget message
and is usually auth/context-window, not a cap).

Order to try levers:

1. **Input cap** — `Cap exceeded: Input token budget (X) exceeded (Y used)`.
   Fix by *reducing what you asked for* first (`file_scope: changed`,
   `test_mode: diff`, narrower criteria, fewer criteria), then by raising
   `max_input_tokens` above the observed `Y` with ≥20 % headroom.
2. **Output** — a verdict cut off mid-object, or "Output token budget (X)
   exceeded". Raise `max_output_tokens` (session). Only if a *single* response
   is truncated, and only after checking you are not chasing the session
   budget, consider `max_tokens_per_call` — remembering the 393 216 clamp.
3. **Time** — no verdict, wall clock at `max_time`. Raise `max_time`; then ask
   whether the judge is re-running a huge suite each iteration (see the
   45 m 9 s case) and bound it instead.
4. **Iteration** — `Iteration cap (N) reached (N.x used)`. Double it, or split
   the task; remember each tool call charges `tool_call_weight` (0.1).
5. **Compaction** — still hitting the input cap after (1)? Check
   `compaction_threshold` is below 1.0 and that the pre-loaded block is not
   larger than `code_context_budget × max_input_tokens`. Compaction rebuilds
   the conversation at most `MAX_COMPACTIONS = 3` times
   (`engine/evaluator.py:1169`) and **cannot shrink the initial pre-loaded
   block** — a too-large first message is forever. Compaction resets the
   per-turn counters but **not** the iteration/time caps
   (`docs/evaluator-loop.md:46-58`).

What a capped run actually returns (`docs/evaluator-loop.md:33-44`): if the
judge stored `verified_<index>` scratch entries, it emits a partial verdict —
any non-PASS becomes FAIL and un-checked criteria become FAIL with *"Not
verified — evaluation terminated before this criterion was checked"* — and the
whole run is COMPLETE only if **every** criterion PASSed. A capped run can
therefore never pass a task it did not finish verifying. If nothing was
recorded, you get INCOMPLETE with `summary` = `Cap exceeded: <reason>`.

Before re-running, **read the verdict's failing item**. A FAIL with concrete
`file:line` evidence is a code bug, not a cap problem; a FAIL with
"Not verified" is a budget problem.

## Test judge behaviour hermetically, for free

`engine/llm.py:186-201`: if `GITREINS_MOCK_LLM_RESPONSE` is set, `chat()`
returns a canned `LLMResponse` **without making a network call**.

```
GITREINS_MOCK_LLM_RESPONSE='{"content":"{\"verdict\":\"COMPLETE\",\"items\":[]}"}'
```

Verified live on this box with a **dead** base URL (`http://127.0.0.1:9/v1`) —
the mock returned the canned content and zero tool calls.

Rules for using it well:

- The **same** response is returned for *every* `chat()` call. A content-only
  verdict with no `tool_calls` makes the loop terminate on turn 1 — that is
  the pattern to copy. A mock carrying `tool_calls` loops until a cap is hit,
  so only do that to exercise cap/compaction behaviour deliberately.
- Set a **placeholder** credential alongside it; the client requires a key to
  be constructed. The test suite uses a non-secret placeholder
  (`tests/test_cli.py:125-131`) — copy that convention; never put a real key in
  an env dict in a test.
- `tool_calls` accepts `[{"id":…, "name":…, "arguments":{…}}]` — enough to
  drive the tool dispatch path hermetically.
- Use `judge --ephemeral` for a hermetic end-to-end CLI check: it persists
  nothing, so a mock run leaves no verdict history, branch or stash
  (`gitreins/cli.py:3510-3535`).
- The existing suite's hermetic judge tests live in `tests/test_cli.py` and
  `tests/test_evidence_contract.py` (`GITREINS_MOCK_LLM_RESPONSE` at
  `tests/test_cli.py:702, 848`, `tests/test_evidence_contract.py:745-749`) —
  copy their shapes rather than inventing one.

## Quick reference

```yaml
# .gitreins/config.yaml — a well-bounded judge for a mid-size repo
evaluator:
  max_iterations: 100        # -1 = unlimited; tool calls charge 0.1 each
  max_time: 15m              # engine default is -1 (UNLIMITED) — always set one
  max_input_tokens: 10M      # cumulative, per context window
  max_output_tokens: 131072  # session output budget (not per request)
  max_tokens_per_call: 16384 # per request; clamped to 393216 on DeepSeek
  tool_call_weight: 0.1
  compaction_threshold: 0.90 # 1.0 disables proactive compaction
  code_context_budget: 0.70  # share of the input cap for pre-loaded code
  file_scope: changed        # the single biggest cost lever — keep it
  fast_track: auto           # on at >=20 source packages
```

```bash
# Bound one run without editing config (env wins over config)
GITREINS_MAX_ITERATIONS=12 GITREINS_MAX_TIME=8m gitreins judge <id>

# Grade a diff and persist nothing
gitreins judge --ephemeral --title "spot check" --criterion "<exact, scoped criterion>"

# Where the spend is
tail -1 .gitreins/usage.jsonl     # tokens for the last tier2 step
```

## Corrections to older sources (do not trust these)

- **"Set `max_output_tokens: 1M` to stop truncation"** — wrong. It is the
  session budget; the per-request value is `max_tokens_per_call`, and an
  over-limit per-call value is clamped to 393 216 on DeepSeek rather than
  rejected (`engine/llm.py:279-303`, `engine/evaluator.py:1222-1225`).
- **"The evaluator reads ALL source files every iteration, so size
  `max_input_tokens` off repo size"** — obsolete since `file_scope: changed`
  became the default (`engine/config.py:56`). Cost tracks the changed set and
  the criteria, not the package count. Cap-sizing tables keyed on "packages"
  predate that and over-provision badly.
- **"`max_time` unset is a safe default"** — the engine default is `-1`
  (**unlimited**, `engine/config.py:47`). Set it explicitly for anything
  unattended.
- **"Compaction will save a blown first message"** — it cannot; it rebuilds
  turn-by-turn accumulation only. Bound the pre-loaded block with
  `code_context_budget` and narrower criteria.
- The judge's Tier 1 is not the whole guard (it has been secrets/lint/tests in
  flux) — **gate merges on `gitreins guard`/CI, never on the judge exit code
  alone**; see the `gitreins-usage` skill for the current lane coverage.
