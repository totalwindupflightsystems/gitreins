"""
LLM Interface — multi-provider with retry and robust error handling.

Supports:
    - OpenAI-compatible (/v1/chat/completions) — OpenAI, OpenRouter, local
    - Anthropic native (/v1/messages) — auto-detected from base_url

Configure via environment variables:
    GITREINS_LLM_BASE_URL   — API base URL
    GITREINS_LLM_API_KEY    — API key
    GITREINS_LLM_MODEL      — Model name (default: from engine.config)
    GITREINS_LLM_PROVIDER   — Force provider: "openai" or "anthropic" (auto-detect if unset)
"""

import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any

import requests

logger = logging.getLogger("gitreins.llm")


def _default_model() -> str:
    """Return the default model from GitReinsDefaults (lazy import)."""
    from engine.config import GitReinsDefaults
    return GitReinsDefaults().model


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class LLMUsage:
    """Token usage returned by the LLM API.

    Distinguishes regular input from cached input. Cache hits are
    cheaper — most providers charge 10-50% of the regular price.
    """
    prompt_tokens: int = 0         # Regular (uncached) input tokens
    cache_read_tokens: int = 0     # Cache hit — tokens served from cache
    cache_write_tokens: int = 0    # New tokens written to cache
    completion_tokens: int = 0     # Output tokens generated
    total_tokens: int = 0          # Sum of all (for display)

    @property
    def all_input_tokens(self) -> int:
        """Total input tokens including cache (for budget tracking)."""
        return self.prompt_tokens + self.cache_read_tokens + self.cache_write_tokens


@dataclass
class LLMResponse:
    content: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: LLMUsage | None = None


def _is_anthropic(url: str) -> bool:
    """Detect Anthropic from base URL."""
    url_lower = url.lower()
    return "anthropic.com" in url_lower or "claude" in url_lower


class LLMClient:
    """Multi-provider chat completions client with retry logic."""

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        provider: str | None = None,
        max_retries: int = 3,
        llm_reasoning: str | None = None,
    ):
        base_url = (base_url or os.getenv("GITREINS_LLM_BASE_URL") or "https://api.openai.com/v1").rstrip("/")
        self.api_key = api_key or os.getenv("GITREINS_LLM_API_KEY", "")
        if not self.api_key:
            # Fallback: try common provider keys
            for env_key in (
                "NEURALWATT_API_KEY", "OPENAI_API_KEY",
                "ANTHROPIC_API_KEY", "DEEPSEEK_API_KEY",
                "KIMI_API_KEY", "GROQ_API_KEY", "OPENROUTER_API_KEY",
            ):
                self.api_key = os.getenv(env_key, "")
                if self.api_key:
                    break
        self.model = model or os.getenv("GITREINS_LLM_MODEL") or _default_model()
        self.max_retries = max_retries

        # Reasoning mode (DeepSeek thinking control)
        if llm_reasoning is not None:
            self.llm_reasoning = llm_reasoning
        else:
            self.llm_reasoning = os.getenv("GITREINS_LLM_REASONING", "disabled")

        # Auto-detect provider if not forced
        if provider:
            self.provider = provider
        elif _is_anthropic(base_url):
            self.provider = "anthropic"
        else:
            self.provider = "openai"

        # Build full endpoint URLs
        if self.provider == "anthropic":
            # Anthropic: base should be https://api.anthropic.com/v1
            self._chat_url = f"{base_url}/messages"
            self._api_version = os.getenv("GITREINS_ANTHROPIC_VERSION", "2023-06-01")
        else:
            self._chat_url = f"{base_url}/chat/completions"

        logger.debug(
            "LLM client: provider=%s model=%s url=%s",
            self.provider, self.model, self._chat_url
        )

    def chat(
        self,
        messages: list[dict[str, Any]],
        max_tokens: int = 131072,
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.1,
    ) -> LLMResponse:
        """Send a chat completion request with retry logic.

        Set GITREINS_MOCK_LLM_RESPONSE to a JSON string to bypass
        the real API and return the mock response directly. Useful
        for subprocess-based tests where unittest.mock.patch() can't
        reach the child process.
        """
        # Log warning when default is used — config should be driving this
        if max_tokens == 131072:
            logger.debug("LLM chat using default max_tokens=131072 — "
                         "consider setting evaluator.max_output_tokens in config")
        mock_resp_json = os.getenv("GITREINS_MOCK_LLM_RESPONSE")
        if mock_resp_json:
            data = json.loads(mock_resp_json)
            content = data.get("content", "")
            tool_calls_raw = data.get("tool_calls")
            tool_calls = []
            if tool_calls_raw is not None:
                tool_calls = [ToolCall(
                    id=tc.get("id", ""),
                    name=tc.get("name", ""),
                    arguments=tc.get("arguments", {}),
                ) for tc in tool_calls_raw]
            return LLMResponse(content=content, tool_calls=tool_calls, usage=LLMUsage())

        last_error: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                return self._chat_attempt(messages, tools, temperature, max_tokens)
            except requests.HTTPError as e:
                status = e.response.status_code if hasattr(e, "response") and e.response else None
                # Don't retry on 4xx (except 429 rate limit)
                if status and status != 429 and 400 <= status < 500:
                    raise
                last_error = e
                logger.warning("HTTP error (attempt %d/%d): %s", attempt + 1, self.max_retries, e)
            except requests.RequestException as e:
                last_error = e
                logger.warning(
                    "Network error (attempt %d/%d): %s",
                    attempt + 1, self.max_retries, e
                )

            if attempt < self.max_retries - 1:
                wait = 2 ** attempt
                logger.debug("Retrying in %ds...", wait)
                time.sleep(wait)

        raise RuntimeError(f"LLM request failed after {self.max_retries} attempts") from last_error

    def _chat_attempt(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        temperature: float,
        max_tokens: int,
    ) -> LLMResponse:
        """Single attempt at a chat completion."""
        if self.provider == "anthropic":
            return self._chat_anthropic(messages, tools, temperature, max_tokens)
        else:
            return self._chat_openai(messages, tools, temperature, max_tokens)

    def _is_deepseek(self) -> bool:
        """Detect DeepSeek from model name or base URL."""
        return "deepseek" in self.model.lower() or "deepseek" in self.provider.lower()

    # ── OpenAI path ──────────────────────────────────────────────

    # Provider output token caps (max_tokens in chat/completions).
    # Values sourced from provider docs as of 2026-06.
    _PROVIDER_MAX_OUTPUT_TOKENS: dict[str, int] = {
        "deepseek": 393_216,     # DeepSeek V4 API: max_tokens range [1, 393216]
        "openai": 1_000_000,     # OpenAI varies by model; safe ceiling
        "anthropic": 1_000_000,  # Anthropic varies by model; safe ceiling
        "openrouter": 1_000_000,  # OpenRouter — pass-through; no known hard cap
    }

    @staticmethod
    def _clamp_max_tokens(max_tokens: int, provider_hint: str = "") -> int:
        """Clamp max_tokens to the provider's documented API limit.

        Returns the original value if the provider is unknown or has no cap.
        """
        if max_tokens <= 0:
            return max_tokens  # unlimited / unset
        limit = LLMClient._PROVIDER_MAX_OUTPUT_TOKENS.get(provider_hint.lower())
        if limit is not None and max_tokens > limit:
            logger.warning(
                "clamping max_tokens %d → %d (provider %s API limit)",
                max_tokens, limit, provider_hint,
            )
            return limit
        return max_tokens

    def _chat_openai(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        temperature: float,
        max_tokens: int,
    ) -> LLMResponse:
        max_tokens = self._clamp_max_tokens(max_tokens, provider_hint=self.provider)
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": self._convert_messages_for_openai(messages),
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if tools:
            payload["tools"] = self._convert_tools_for_openai(tools)
            payload["tool_choice"] = "auto"

        # DeepSeek thinking mode control (GR-068)
        if self._is_deepseek():
            if self.llm_reasoning == "enabled":
                payload["thinking"] = {"type": "enabled"}
            else:
                payload["thinking"] = {"type": "disabled"}

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }

        resp = requests.post(self._chat_url, headers=headers, json=payload, timeout=120)
        resp.raise_for_status()
        data = resp.json()

        choice = data["choices"][0]
        message = choice["message"]

        tool_calls = []
        if message.get("tool_calls"):
            for tc in message["tool_calls"]:
                try:
                    args = json.loads(tc["function"]["arguments"])
                except (json.JSONDecodeError, KeyError):
                    args = {}
                tool_calls.append(
                    ToolCall(id=tc["id"], name=tc["function"]["name"], arguments=args)
                )

        # Extract token usage (with cache detection for DeepSeek)
        usage = None
        if "usage" in data:
            u = data["usage"]
            usage = LLMUsage(
                prompt_tokens=u.get("prompt_tokens", 0),
                cache_read_tokens=u.get("prompt_cache_hit_tokens", 0),
                cache_write_tokens=u.get("prompt_cache_miss_tokens", 0),
                completion_tokens=u.get("completion_tokens", 0),
                total_tokens=u.get("total_tokens", 0),
            )

        return LLMResponse(content=message.get("content"), tool_calls=tool_calls, usage=usage)

    def _convert_messages_for_openai(self, messages: list[dict]) -> list[dict]:
        """Ensure messages use OpenAI format (identity for OpenAI-native)."""
        return messages

    def _convert_tools_for_openai(self, tools: list[dict]) -> list[dict]:
        """Convert tools to OpenAI format."""
        # Our internal format is already OpenAI-compatible
        return tools

    # ── Anthropic path ───────────────────────────────────────────

    def _chat_anthropic(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        temperature: float,
        max_tokens: int,
    ) -> LLMResponse:
        # Extract system message
        system = None
        converted = []
        for msg in messages:
            if msg["role"] == "system":
                system = msg["content"]
            else:
                converted.append(msg)

        # Convert messages to Anthropic format (alternating user/assistant)
        anthropic_messages = self._convert_messages_for_anthropic(converted)
        anthropic_tools = self._convert_tools_for_anthropic(tools) if tools else None

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": anthropic_messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if system:
            payload["system"] = system
        if anthropic_tools:
            payload["tools"] = anthropic_tools

        api_key = self.api_key or ""
        headers: dict[str, str] = {
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": self._api_version,
        }

        resp = requests.post(self._chat_url, headers=headers, json=payload, timeout=120)
        resp.raise_for_status()
        data = resp.json()

        # Parse Anthropic response
        content = None
        tool_calls = []
        for block in data.get("content", []):
            if block["type"] == "text":
                content = block.get("text", "")
            elif block["type"] == "tool_use":
                tool_calls.append(ToolCall(
                    id=block["id"],
                    name=block["name"],
                    arguments=block.get("input", {}),
                ))

        # Extract token usage (Anthropic distinguishes cache reads/writes)
        usage = None
        if "usage" in data:
            u = data["usage"]
            input_tok = u.get("input_tokens", 0)
            output_tok = u.get("output_tokens", 0)
            cache_read = u.get("cache_read_input_tokens", 0)
            cache_write = u.get("cache_creation_input_tokens", 0)
            regular_input = max(0, input_tok - cache_read - cache_write)
            usage = LLMUsage(
                prompt_tokens=regular_input,
                cache_read_tokens=cache_read,
                cache_write_tokens=cache_write,
                completion_tokens=output_tok,
                total_tokens=input_tok + output_tok,
            )

        return LLMResponse(content=content, tool_calls=tool_calls, usage=usage)

    def _convert_messages_for_anthropic(self, messages: list[dict]) -> list[dict]:
        """
        Convert to Anthropic-native format.

        Anthropic requires:
        - Alternating user/assistant roles
        - Tool results merged into a user message with tool_result blocks
        - No 'tool' role — tool results go inside a user message
        """
        result = []
        for msg in messages:
            role = msg["role"]

            if role == "system":
                # System messages are handled separately — skip here
                continue

            if role == "tool":
                # Anthropic: tool results go in a user message as tool_result content blocks
                result.append({
                    "role": "user",
                    "content": [{
                        "type": "tool_result",
                        "tool_use_id": msg.get("tool_call_id", ""),
                        "content": msg.get("content", ""),
                    }],
                })
                continue

            # For user/assistant roles
            if role == "assistant" and "tool_calls" in msg:
                # Assistant with tool calls: convert to Anthropic format
                content_blocks = []
                if msg.get("content"):
                    content_blocks.append({"type": "text", "text": msg["content"]})
                for tc in msg["tool_calls"]:
                    content_blocks.append({
                        "type": "tool_use",
                        "id": tc["id"],
                        "name": tc["function"]["name"],
                        "input": (
                            json.loads(tc["function"]["arguments"])
                            if isinstance(
                                tc["function"]["arguments"], str
                            )
                            else tc["function"]["arguments"]
                        ),
                    })
                result.append({"role": "assistant", "content": content_blocks})
            else:
                result.append({"role": role, "content": msg.get("content", "")})

        return result

    def _convert_tools_for_anthropic(self, tools: list[dict]) -> list[dict]:
        """Convert OpenAI tool format to Anthropic format."""
        result = []
        for tool in tools:
            func = tool.get("function", tool)
            result.append({
                "name": func["name"],
                "description": func.get("description", ""),
                "input_schema": func.get("parameters", {"type": "object", "properties": {}}),
            })
        return result
