# Finding 001 — DeepSeek Provider Quirks

> **Status:** Lessons learned. All workarounds are now baked into
> `engine/llm.py` and `.gitreins/config.yaml`.
> **Default provider:** DeepSeek (`deepseek-chat` on
> `https://api.deepseek.com/v1`).

---

## What this is

DeepSeek is the default LLM provider for GitReins in this repo. The
`.env` file ships with:

```
GITREINS_LLM_API_KEY=sk-...
GITREINS_LLM_BASE_URL=https://api.deepseek.com/v1
GITREINS_LLM_MODEL=deepseek-chat
```

(API key redacted; the real one is in `.env` which is gitignored.)
The fallback env-var chain in `engine/llm.py:58-64` will also pick
up `DEEPSEEK_API_KEY` if `GITREINS_LLM_API_KEY` is empty.

## Quirks that bit us

### 1. The `/v1` suffix matters

DeepSeek's documentation sometimes shows the base URL as
`https://api.deepseek.com` (no `/v1`). The client appends
`/chat/completions` to whatever you pass, so:

- `base_url="https://api.deepseek.com"` → requests
  `https://api.deepseek.com/chat/completions` → **404**.
- `base_url="https://api.deepseek.com/v1"` → requests
  `https://api.deepseek.com/v1/chat/completions` → **200**.

The client trims trailing slashes (`base_url.rstrip("/")`,
`engine/llm.py:57`), but does NOT add the `/v1`. If you copy a
URL from the docs, double-check it ends in `/v1`.

`test_llm.py:334` is the regression test for this — it
explicitly sets `base_url="https://api.deepseek.com/v1"` and
asserts the chat URL is correct.

### 2. `deepseek-chat` is the only model name to use

`deepseek-reasoner` exists but does not support the function-calling
shape the evaluator depends on (verified against the DeepSeek docs as
of 2026-05). If you need reasoning-style evaluation, the recommendation
is to switch to MiniMax M3 (see `finding-002-minimax-reasoning-mode.md`)
which has a dedicated reasoning mode that DOES work with our
function-calling format via `max_tokens` tuning.

The engine's `model` field is just a string passed through; the
engine does no model validation. Setting `GITREINS_LLM_MODEL=wrong-name`
will fail at the provider with a 4xx, which the retry layer will
**not** retry (4xx non-429 is non-retryable per
`engine/llm.py:100-103`).

### 3. Rate limiting is generous but the retry matters

DeepSeek's rate limits are higher than most providers in practice
(hundreds of requests per minute on the standard tier). But network
blips and 5xx are real. The retry loop in `engine/llm.py:95-114`
catches `requests.RequestException` (network errors) and HTTP
errors with `status=429` (rate limited) and retries up to
`max_retries=3` (default) with `2 ** attempt` second backoff:
1s, 2s, 4s. For a typical flake the second attempt succeeds.

### 4. Temperature 0.1 is the sweet spot

The engine hardcodes `temperature=0.1` in
`engine/llm.py:90` for the chat call. This is deliberate: lower than
0.1 and the model becomes repetitive on the JSON verdict (the same
phrasing every time, which masks when the LLM is actually reading
vs. guessing). Higher than 0.2 and the verdict format starts
breaking — the model invents tool calls or wraps the JSON in
markdown fences. 0.1 is the value we converged on after testing
the `engine/evaluator.py:212` loop on a real task.

### 5. Long tool-call results are fine, but the chat history isn't

The DeepSeek context window is large enough that a typical
evaluator session (3-8 tool calls × 2-4KB each) fits
comfortably. But the LLM's output tokens are not free — if you
let `max_iterations` climb to 20 and the LLM does 15+ tool calls,
the conversation length matters. The engine caps
`max_iterations=20` in `.gitreins/config.yaml:53` and the loop
forces a final verdict when the cap is hit
(`engine/evaluator.py:312-323`).

## How to verify DeepSeek is configured

```bash
# Should return 200 and a list of models
curl -s -H "Authorization: Bearer $GITREINS_LLM_API_KEY" \
  "$GITREINS_LLM_BASE_URL/models" | head
```

Or in the engine:

```bash
PYTHONPATH=. python3 -c "
from engine.llm import LLMClient
c = LLMClient()
print(f'provider={c.provider} model={c.model} url={c._chat_url}')
"
```

## Tests

| What | Test |
|------|------|
| Env-var fallback to `DEEPSEEK_API_KEY` | `tests/test_llm.py:117-133` |
| Correct base URL construction | `tests/test_llm.py:334` |
| Retry on network errors | `tests/test_llm.py` (mocked HTTP) |
| Anthropic exclusion (DeepSeek should NOT trigger Anthropic path) | `tests/test_llm.py` (auto-detect tests) |

## Switching away from DeepSeek

`provider-integration.md` is the canonical reference for wiring any
other OpenAI-compatible provider. The short version: set the 3
env vars, no code change.

## Related

- `engine/llm.py:40-83` — provider auto-detection
- `engine/llm.py:95-114` — retry logic
- `engine/llm.py:58-64` — env-var fallback chain
- `.gitreins/config.yaml:53` — `max_iterations: 16` (default in repo)
- `.env` — current default provider config
