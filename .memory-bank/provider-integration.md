# Provider Integration — How to Wire Any OpenAI-Compatible LLM

> **TL;DR:** Set 3 environment variables. That's it for 90% of providers.
> The engine's `LLMClient` (`engine/llm.py`) speaks the OpenAI Chat
> Completions format natively. Anthropic is auto-detected and routed
> to its native API.

---

## The 3 env vars

Every OpenAI-compatible provider is configured with the same
3 environment variables. No code change, no new dependency.

| Variable | Required | Example | Notes |
|----------|----------|---------|-------|
| `GITREINS_LLM_BASE_URL` | yes | `https://api.deepseek.com/v1` | Must end in `/v1` for OpenAI-compat providers. The engine appends `/chat/completions` to this. |
| `GITREINS_LLM_API_KEY` | yes | `sk-...` or `eyJ...` (JWT) | Sent as `Authorization: Bearer <key>`. Anthropic uses `x-api-key` header and is auto-detected. |
| `GITREINS_LLM_MODEL` | yes | `deepseek-chat` | Provider-specific model name. The engine passes it through verbatim. |

### Fallback chain

If `GITREINS_LLM_API_KEY` is empty, the client tries in order
(`engine/llm.py:58-64`):

1. `NEURALWATT_API_KEY`
2. `OPENAI_API_KEY`
3. `ANTHROPIC_API_KEY`
4. `DEEPSEEK_API_KEY`

First non-empty wins. This means: if you have `OPENAI_API_KEY`
set in your shell for any other reason, the engine will pick it
up without you needing to set `GITREINS_LLM_API_KEY` separately.

**Not in the fallback chain (yet):** `MINIMAX_API_KEY`. To use
MiniMax M3, set `GITREINS_LLM_API_KEY=$MINIMAX_API_KEY` explicitly.
See `findings/finding-002-minimax-reasoning-mode.md`.

---

## Base URL table

| Provider | `GITREINS_LLM_BASE_URL` | `GITREINS_LLM_MODEL` | Notes |
|----------|------------------------|----------------------|-------|
| **DeepSeek** (default) | `https://api.deepseek.com/v1` | `deepseek-chat` | Fast, cheap, good default. See `findings/finding-001-...`. |
| **OpenAI** | `https://api.openai.com/v1` | `gpt-4o-mini` (cheap) / `gpt-4o` (smart) | Native OpenAI. |
| **OpenRouter** | `https://openrouter.ai/api/v1` | `anthropic/claude-3.5-sonnet` etc. | Multi-provider router. Model names use `provider/model` format. |
| **Groq** | `https://api.groq.com/openai/v1` | `llama-3.1-70b-versatile` etc. | Very fast inference, rate-limited. |
| **Together** | `https://api.together.xyz/v1` | `meta-llama/Llama-3-70b-chat-hf` | Open-source model aggregator. |
| **Fireworks** | `https://api.fireworks.ai/inference/v1` | `accounts/fireworks/models/llama-v3p1-70b-instruct` | Fast open-source inference. |
| **Ollama (local)** | `http://localhost:11434/v1` | `llama3.1` etc. | Local. No API key needed; use any string. |
| **LM Studio (local)** | `http://localhost:1234/v1` | any loaded model | Local. Same shape as Ollama. |
| **vLLM (local)** | `http://localhost:8000/v1` | any served model | Self-hosted. |
| **Anthropic** | `https://api.anthropic.com/v1` | `claude-3-5-sonnet-latest` etc. | **NOT** OpenAI-compat — auto-detected, uses `/v1/messages` and `x-api-key`. |
| **MiniMax M3** | `https://api.minimax.io/v1` | `MiniMax-M3` | Reasoning mode. See `findings/finding-002-...`. |
| **NeuralWatt** | (provider-specific) | (per model) | Falls back via `NEURALWATT_API_KEY`. |

> **Common mistake:** dropping the `/v1` suffix. If the docs
> show `https://api.example.com` and the engine returns 404, the
> fix is `https://api.example.com/v1`. The engine does NOT add
> the `/v1` for you.

---

## Provider-specific quirks

### OpenAI

- Works out of the box. `gpt-4o-mini` is the cheapest and
  fast enough for most evaluations.
- Supports function calling in the same shape as the engine
  expects.
- Set `temperature=0.1` (the engine's default) for reproducible
  verdicts.

### DeepSeek

- `deepseek-chat` is the only model to use. `deepseek-reasoner`
  does not support function calling in the format the engine
  expects.
- See `findings/finding-001-deepseek-provider-quirks.md` for
  the full quirk list.

### Anthropic

- **Auto-detected** by `_is_anthropic(base_url)` in
  `engine/llm.py:40-43`. The check is for "anthropic.com" or
  "claude" in the URL.
- The engine handles the message conversion internally
  (`_convert_messages_for_anthropic`, `engine/llm.py:240-286`).
  Your code does not need to know.
- Use `claude-3-5-sonnet-latest` or `claude-haiku-4-5-20251001`
  for the evaluator. Haiku is faster and cheaper; Sonnet is
  more accurate on hard verdicts.

### OpenRouter

- Model names use the `provider/model` format, e.g.
  `anthropic/claude-3.5-sonnet`. Pass it through `GITREINS_LLM_MODEL`
  unchanged.
- OpenRouter handles the auth conversion to the upstream
  provider, so this is just an OpenAI-compat client.
- Some OpenRouter-served models don't support function calling;
  check the model card before relying on a specific provider.

### Ollama (local)

- `ollama serve` runs on `localhost:11434` by default. The
  OpenAI-compat endpoint is at `/v1`.
- No real API key needed — Ollama ignores the `Authorization`
  header. Use any string.
- Not all Ollama models support function calling. Models that
  do: `llama3.1`, `qwen2.5`, `mistral-nemo`, `command-r-plus`.
  Models that don't: most older models. Check
  `https://ollama.com/search?c=tools` for the current list.

```bash
# Example: use Ollama with qwen2.5
export GITREINS_LLM_BASE_URL=http://localhost:11434/v1
export GITREINS_LLM_API_KEY=ollama
export GITREINS_LLM_MODEL=qwen2.5:7b
```

### LM Studio (local)

- LM Studio exposes an OpenAI-compat server on
  `http://localhost:1234/v1` (default port).
- Same model-name passthrough as Ollama.

### Groq

- Very fast inference (often <500ms for short completions).
- The `llama-3.1-70b-versatile` model supports function calling.
- Rate limits are aggressive on the free tier; if you see
  frequent 429s, increase the retry budget in the engine
  (would need a small patch to `LLMClient.__init__`).

### MiniMax M3

- Reasoning-mode model. See `findings/finding-002-minimax-reasoning-mode.md`
  for full details (max_tokens ≥ 512 floor, slower than
  DeepSeek, better JSON adherence, higher cost).
- Base URL: `https://api.minimax.io/v1`. Model: `MiniMax-M3`.
- Use JWT token (looks like `eyJ...`), not a `sk-` key.

---

## Troubleshooting

### "LLM request failed after 3 attempts"

This is the error from `engine/llm.py:114` when all retries
exhausted. The chain is `2 ** attempt` seconds: 1s, 2s, 4s.
9 seconds total wait.

Common causes:
- Wrong base URL (most common — check the `/v1` suffix).
- Wrong API key (4xx non-429, no retry — see the log for
  status code).
- Provider is down (5xx, retried 3×, gave up).
- Network unreachable (no `requests.RequestException` detail
  in the log — check `gitreins.llm` logger level).

### "404 Not Found" on a base URL that worked yesterday

The provider changed their routing. Check:
- The provider's status page.
- Whether the model name is still current (some providers
  deprecate old model names without notice).
- Whether you need a different base URL per product (some
  providers split chat completions from embeddings from fine-tuning).

### "Tool calls are not being made" (LLM is just returning text)

Some providers don't pass through the `tools` field correctly,
or the model doesn't support function calling. The engine sends
the tools in the OpenAI format
(`engine/llm.py:144-146`); if the provider expects a different
shape, tool calls won't fire.

Quick check:
```bash
curl -s -H "Authorization: Bearer $GITREINS_LLM_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"'$GITREINS_LLM_MODEL'","messages":[{"role":"user","content":"hi"}],"tools":[{"type":"function","function":{"name":"get_weather","description":"get weather","parameters":{"type":"object","properties":{"city":{"type":"string"}}}}}]}' \
  "$GITREINS_LLM_BASE_URL/chat/completions" | jq '.choices[0].message'
```

If `tool_calls` is empty or absent in the response, the model
doesn't support function calling with the current settings.

### "Verdict is INCOMPLETE for a clearly complete task"

Three possibilities:
1. The LLM is running out of iterations. Check the
   `max_iterations` in `.gitreins/config.yaml:53` (default 16).
   Bump to 20 if needed.
2. The LLM is making the same tool call repeatedly. The dedup
   logic should prevent this, but if the dedup key is different
   each time (e.g., reading different lines of the same file),
   the loop can spin. Add a `sandbox_write("checked", ...)`
   call early so the LLM tracks its own progress.
3. The model has low function-calling reliability. Switch
   providers or upgrade to a stronger model.

### "Cost is higher than expected"

- Each evaluation is N+1 LLM calls where N is the number of
  tool iterations. With `max_iterations=16` and a model that
  uses 6 iterations, that's 7 calls per evaluation.
- Reasoning-mode models (MiniMax M3) cost 3-4× more per call
  due to the `reasoning_content` tokens.
- For high-volume use, set `max_iterations=10` in
  `.gitreins/config.yaml` and use a cheaper model
  (`gpt-4o-mini`, `llama-3.1-8b` via Groq, etc.).

---

## Verifying the configuration

```bash
# 1. Check the engine sees the right config
PYTHONPATH=. python3 -c "
from engine.llm import LLMClient
c = LLMClient()
print(f'provider: {c.provider}')
print(f'model:    {c.model}')
print(f'url:      {c._chat_url}')
"

# 2. Make a real chat call
PYTHONPATH=. python3 -c "
from engine.llm import LLMClient
c = LLMClient()
r = c.chat([{'role': 'user', 'content': 'Reply with the word OK and nothing else.'}])
print(repr(r.content))
"
```

The first should print your provider/model/URL. The second
should print `'OK'` (or close to it — `temperature=0.1` keeps
it close to deterministic but not exactly).

---

## Related

- `engine/llm.py` — full client implementation
- `adr/adr-002-openai-compatible-llm.md` — why this design
- `findings/finding-001-deepseek-provider-quirks.md` — DeepSeek-specific
- `findings/finding-002-minimax-reasoning-mode.md` — MiniMax-specific
- `docs/technology-choices.md:24-26` — why no SDK, just `requests`
- `.env` — current default provider config (DeepSeek, redacted key)
