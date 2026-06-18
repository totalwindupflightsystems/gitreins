# ADR-002 — OpenAI-Compatible HTTP Layer for All LLM Providers

> **Decision:** All LLM providers (OpenAI, DeepSeek, OpenRouter,
> Groq, Ollama, LM Studio, Anthropic) are accessed through one HTTP
> client. The internal tool/function-call format is OpenAI's; a
> converter adapts to Anthropic's `/v1/messages` shape only when
> needed.
>
> **Status:** Accepted (2026-05-27). Implemented in
> `engine/llm.py`. No provider-specific SDK is a dependency.

---

## Context

Most major LLM providers (DeepSeek, OpenRouter, Groq, Together,
Fireworks, LM Studio, Ollama, etc.) speak the **OpenAI Chat
Completions** API — `POST /v1/chat/completions`, `Authorization:
Bearer <key>`, JSON in, JSON out, function-calling in the same
shape. Anthropic is the only major exception (`/v1/messages` with
`x-api-key` header and a different content-block structure).

We could have:

- **A.** Shipped a different client per provider (openai-sdk,
  anthropic-sdk, etc.) — 5+ dependencies, version churn,
  inconsistent tool/function-call semantics across SDKs.
- **B.** Used a router library like LiteLLM — one dep, but
  introduces a translation layer we can't debug and pins us to
  the library's release cadence.
- **C.** Written `requests` against the raw HTTP API with a
  ~200-line adapter for the Anthropic diff. **← what we did.**

## Decision

Use the `requests` library directly. Auto-detect the provider from
the `base_url`:

```python
# engine/llm.py:40-83
def _is_anthropic(url: str) -> bool:
    return "anthropic.com" in url.lower() or "claude" in url.lower()

class LLMClient:
    def __init__(self, base_url=None, ...):
        base_url = (base_url or os.getenv("GITREINS_LLM_BASE_URL",
                     "https://api.openai.com/v1")).rstrip("/")
        # ...
        if provider:
            self.provider = provider
        elif _is_anthropic(base_url):
            self.provider = "anthropic"
        else:
            self.provider = "openai"
        # ...
        if self.provider == "anthropic":
            self._chat_url = f"{base_url}/messages"
        else:
            self._chat_url = f"{base_url}/chat/completions"
```

Two paths share the same dataclass return type:

```python
@dataclass
class LLMResponse:
    content: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)
```

The Anthropic path converts at the boundary
(`_convert_messages_for_anthropic`, lines 240-286;
`_convert_tools_for_anthropic`, lines 288-298). The rest of the
engine — `AgenticEvaluator`, `Judge`, `Pipeline` — only knows about
`LLMResponse`. They never see an Anthropic or OpenAI message.

## Why this is the right call

### 1. Zero provider-specific dependencies

`requirements.txt` is exactly:

```
mcp>=1.0.0
pyyaml>=6.0
requests>=2.28
```

`requests` is already in the Python standard toolchain for LLM work.
No `openai` package, no `anthropic` package, no LiteLLM. The
`openai` Python package has had version-churn issues (1.0 → 2.0
breaking change, etc.) that would have made `engine/llm.py` brittle
to the upstream library's release cadence.

### 2. Coverage without lock-in

Every OpenAI-compatible provider works out of the box by setting
3 env vars:

```
GITREINS_LLM_BASE_URL=https://api.deepseek.com/v1
GITREINS_LLM_API_KEY=sk-...
GITREINS_LLM_MODEL=deepseek-chat
```

See `provider-integration.md` for the full table (DeepSeek, MiniMax,
OpenRouter, Groq, Ollama, LM Studio, OpenAI, etc.). The fallback
env-var chain (engine/llm.py:58-64) means an empty
`GITREINS_LLM_API_KEY` falls back to `NEURALWATT_API_KEY`,
`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `DEEPSEEK_API_KEY` — so
a user with any of those set "just works."

### 3. Anthropic is the one we DO special-case

Anthropic's API differs in three ways that aren't worth papering over
with a generic adapter:

- Endpoint: `/v1/messages` (not `/v1/chat/completions`).
- Auth header: `x-api-key` (not `Authorization: Bearer`).
- Tool results: returned in a `user` message with `tool_result`
  content blocks (not as a `role: tool` message).

The conversion lives in `engine/llm.py:184-298` (~115 lines) and is
the only place in the engine that knows about Anthropic's wire
format. This is a one-time cost, not a recurring tax.

### 4. Retry is one place

Exponential backoff lives in `LLMClient.chat()` (lines 95-114). 4xx
errors (except 429) are not retried — they're client errors.
Anything that changes per provider is wrapped in a try/except in
the per-provider path; the retry layer is provider-agnostic.

## Trade-offs accepted

- ❌ If OpenAI changes the function-calling wire format, we patch
  the code ourselves. (Mitigated by the format being stable for
  3+ years across OpenAI-compatible providers.)
- ❌ No streaming support. The evaluator doesn't need it (it parses
  the full response for tool calls), but if a future feature wants
  streaming, we'd add a streaming path.
- ❌ We maintain the Anthropic converter. ~115 lines, well-tested
  by `test_llm.py` (45 tests including 8+ message-conversion cases).

## Evidence in the code

| What | Where |
|------|-------|
| Provider auto-detection | `engine/llm.py:40-43` |
| Endpoint construction | `engine/llm.py:77-83` |
| Env-var fallback chain | `engine/llm.py:58-64` |
| OpenAI path (default) | `engine/llm.py:131-180` |
| Anthropic path | `engine/llm.py:184-238` |
| Message conversion | `engine/llm.py:240-298` |
| Retry / backoff | `engine/llm.py:95-114` |
| Tests | `tests/test_llm.py` (45 tests) |

## Adding a new provider

If the provider is OpenAI-compatible: just set the 3 env vars.
**No code change needed.** `provider-integration.md` is the
checklist for new providers.

If the provider is *not* OpenAI-compatible (e.g., a future
Google Gemini v1beta with a different shape): add a new path
alongside `_chat_openai` / `_chat_anthropic`, set `self.provider`
in `__init__`, and write tests in `test_llm.py`. The `LLMResponse`
return type keeps the rest of the engine unchanged.
