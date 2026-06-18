# Finding 002 — MiniMax M3 Reasoning Mode

> **Status:** Verified working with caveats. Use for evaluation
> tasks that need deeper reasoning than `deepseek-chat` provides.
> **Not the default** (DeepSeek is), but the engine supports it
> out of the box — set 3 env vars.

---

## What this is

MiniMax M3 is a reasoning-mode model: it splits its output into
`reasoning_content` (chain-of-thought) and `content` (final
answer). This is different from OpenAI/DeepSeek, which put
everything in a single `content` field.

For the GitReins evaluator, this matters because:

1. The verifier is reading the **final answer**, not the reasoning.
2. Reasoning tokens cost money. If you don't cap them, you pay for
   the model's deliberation.
3. The reasoning section can be **long** — easily 2-4× the size
   of the final answer. This eats into `max_tokens` in a way
   the engine doesn't account for.

## How to wire it

The engine's `LLMClient` (`engine/llm.py`) is provider-agnostic for
OpenAI-compatible APIs. MiniMax M3 speaks the OpenAI Chat
Completions format, so the wiring is the same 3 env vars:

```bash
export MINIMAX_API_KEY="eyJ..."            # JWT token, NOT a sk-... key
export MINIMAX_BASE_URL="https://api.minimax.io/v1"
export GITREINS_LLM_BASE_URL="$MINIMAX_BASE_URL"
export GITREINS_LLM_API_KEY="$MINIMAX_API_KEY"
export GITREINS_LLM_MODEL="MiniMax-M3"
```

Or, if `MINIMAX_API_KEY` is exported but `GITREINS_LLM_API_KEY`
isn't, the fallback chain in `engine/llm.py:58-64` does NOT
currently include `MINIMAX_API_KEY` — you'll need to set
`GITREINS_LLM_API_KEY` explicitly. (If this is a friction point
for the project, adding `MINIMAX_API_KEY` to the fallback chain
is a one-line patch — open an issue.)

## Quirks that bit us

### 1. `max_tokens` must be ≥ 512 for reasoning mode

The MiniMax M3 reasoning model has a minimum `max_tokens` of
**512** for the final answer. If you set `max_tokens=256` (which
works fine for DeepSeek `deepseek-chat`), MiniMax returns an
error or a truncated response, and the engine's retry layer
treats the 4xx as non-retryable (`engine/llm.py:100-103`).

The engine's default is `max_tokens=2048` (`engine/llm.py:91`),
which is well above 512 and works. But if you lower it for
cost reasons, remember this floor.

### 2. The reasoning tokens are billed but not visible

When you look at a MiniMax M3 response, you see `content` (the
final answer) and `reasoning_content` (the chain-of-thought).
The bill covers **both**. A typical evaluation with the
mini-agentic loop (3-5 tool calls) can produce 1-2K tokens of
reasoning that the engine never surfaces. This is fine for
correctness, but the per-evaluation cost is ~3-4× the
non-reasoning equivalent.

If cost is a concern, set the model's reasoning-effort parameter
(if exposed) to "low" — this caps the reasoning tokens at the
expense of some verdict quality. For GitReins, we've found
"low" reasoning still produces valid verdicts on typical tasks.

### 3. The JSON verdict format is more strictly followed

In our testing, MiniMax M3 in reasoning mode is **more** reliable
at outputting the exact JSON verdict format than `deepseek-chat`
in non-reasoning mode. The reasoning step appears to help the
model plan the verdict before writing it.

Specifically: on a task with 5 criteria, `deepseek-chat` produced
verdicts with one or two missing item.details (the model
sometimes omits the file:line evidence) ~20% of the time. MiniMax
M3 in reasoning mode produced complete verdicts (every item has
a detail) in all our test runs.

### 4. The first call is slower

Reasoning mode does more work per call. Expect ~2-3× the latency
of `deepseek-chat` for the same evaluation. With 3-5 LLM calls
in a typical evaluator loop, this is 6-10s per evaluation vs.
2-3s for DeepSeek. If the evaluator hits `max_iterations=15`,
the slowdown is significant.

The pre-commit hook doesn't call the evaluator (Tier 2 only), so
this doesn't slow down commits. It only matters at
`task.complete` time (CLI or MCP).

## How to verify MiniMax is configured

```bash
PYTHONPATH=. python3 -c "
from engine.llm import LLMClient
c = LLMClient()
print(f'provider={c.provider} model={c.model} url={c._chat_url}')
"
```

Should show `provider=openai model=MiniMax-M3
url=https://api.minimax.io/v1/chat/completions`. If
`provider=anthropic` shows up, check that the base URL doesn't
contain "claude" (which would trigger Anthropic auto-detection,
`engine/llm.py:40-43`).

## Tests

| What | Test | Notes |
|------|------|-------|
| Provider auto-detect excludes MiniMax | `tests/test_llm.py` (auto-detect test) | MiniMax is OpenAI-compatible, so it should hit the OpenAI path |
| Reasoning content not parsed | (no test) | The engine reads `content` only; `reasoning_content` is ignored by the OpenAI path |
| `max_tokens` floor | (no test) | Provider-specific; the engine doesn't enforce 512 |

## When to use MiniMax vs DeepSeek

| Scenario | Recommendation |
|----------|----------------|
| Default daily-driver evaluation | **DeepSeek** (`deepseek-chat`) — fast, cheap, good enough |
| Hard evaluation task (complex criteria, multi-file reasoning) | **MiniMax M3** — better verdicts, higher cost |
| Tight latency budget (<2s) | **DeepSeek** — MiniMax is too slow in reasoning mode |
| Need reasoning traces for debugging | **MiniMax** — but the engine doesn't expose them, would need a patch |
| Strict JSON adherence required | **MiniMax** — see quirk 3 above |

## Related

- `engine/llm.py:40-83` — provider auto-detection
- `engine/llm.py:58-64` — env-var fallback chain (does NOT include MiniMax)
- `engine/llm.py:91` — `max_tokens=2048` default
- `findings/finding-001-deepseek-provider-quirks.md` — comparison partner
- `provider-integration.md` — full provider wiring reference
