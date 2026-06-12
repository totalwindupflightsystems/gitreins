"""
LLM Interface — multi-provider with retry and robust error handling.

Supports:
    - OpenAI-compatible (/v1/chat/completions) — OpenAI, OpenRouter, local
    - Anthropic native (/v1/messages) — auto-detected from base_url

Configure via environment variables:
    GITREINS_LLM_BASE_URL   — API base URL
    GITREINS_LLM_API_KEY    — API key
    GITREINS_LLM_MODEL      — Model name (default: gpt-4o-mini)
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


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class LLMResponse:
    content: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)


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
    ):
        base_url = (base_url or os.getenv("GITREINS_LLM_BASE_URL", "https://api.openai.com/v1")).rstrip("/")
        self.api_key = api_key or os.getenv("GITREINS_LLM_API_KEY", "")
        if not self.api_key:
            # Fallback: try common provider keys
            for env_key in ("NEURALWATT_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "DEEPSEEK_API_KEY"):
                self.api_key = os.getenv(env_key, "")
                if self.api_key:
                    break
        self.model = model or os.getenv("GITREINS_LLM_MODEL", "gpt-4o-mini")
        self.max_retries = max_retries

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

        logger.debug("LLM client: provider=%s model=%s url=%s", self.provider, self.model, self._chat_url)

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.1,
        max_tokens: int = 2048,
    ) -> LLMResponse:
        """Send a chat completion request with retry logic."""
        last_error = None
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
                logger.warning("Network error (attempt %d/%d): %s", attempt + 1, self.max_retries, e)

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

    # ── OpenAI path ──────────────────────────────────────────────

    def _chat_openai(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        temperature: float,
        max_tokens: int,
    ) -> LLMResponse:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": self._convert_messages_for_openai(messages),
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if tools:
            payload["tools"] = self._convert_tools_for_openai(tools)
            payload["tool_choice"] = "auto"

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

        return LLMResponse(content=message.get("content"), tool_calls=tool_calls)

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

        headers = {
            "Content-Type": "application/json",
            "x-api-key": self.api_key,
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

        return LLMResponse(content=content, tool_calls=tool_calls)

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
                        "input": json.loads(tc["function"]["arguments"]) if isinstance(tc["function"]["arguments"], str) else tc["function"]["arguments"],
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
