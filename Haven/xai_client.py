#!/usr/bin/env python3
"""Minimal xAI (Grok) OpenAI-compatible chat client.

xAI exposes an OpenAI-compatible REST API. This module provides a thin
``requests``-based wrapper shaped like the small subset of the Groq / OpenAI
SDK the dashboard actually uses:

    client = XAIClient(api_key=..., base_url=..., default_model=...)
    response = client.chat.completions.create(
        model="grok-4-fast-reasoning",
        messages=[...],
        tools=[...],
        tool_choice="auto",
        max_tokens=4096,
    )
    response.choices[0].message.content
    response.choices[0].message.tool_calls  # list or None

The wrapper also exposes ``APIStatusError`` so call sites that catch rate
limits or oversized requests continue to work without depending on the Groq
SDK.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Iterable, Optional

import requests


# Environment variables consulted for the xAI API key, in priority order.
# ``Grok_XAI_API_KEY`` matches the Vercel project env var; ``XAI_API_KEY`` is
# the canonical xAI name and is supported for local-dev convenience; the
# legacy ``GROQ_API_KEY`` is accepted only as a last-resort fallback for older
# local setups but is no longer the documented provider.
AI_API_KEY_ENV_VARS: tuple[str, ...] = ("Grok_XAI_API_KEY", "XAI_API_KEY", "GROQ_API_KEY")

DEFAULT_BASE_URL = "https://api.x.ai/v1"
DEFAULT_MODEL = "grok-4-fast-reasoning"
DEFAULT_TIMEOUT_SECONDS = 120


def get_ai_api_key() -> Optional[str]:
    """Return the first non-empty API key from the supported env vars."""
    for name in AI_API_KEY_ENV_VARS:
        value = os.environ.get(name)
        if value and value.strip():
            return value.strip()
    return None


def get_xai_base_url() -> str:
    return (os.environ.get("XAI_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")


def get_xai_model() -> str:
    return os.environ.get("XAI_MODEL") or DEFAULT_MODEL


def get_active_provider_info() -> dict[str, Any]:
    """Return a JSON-safe summary of the active AI provider configuration.

    Reports *presence* only — never the actual key. Safe to expose from a
    diagnostic endpoint.
    """
    active_env = None
    for name in AI_API_KEY_ENV_VARS:
        value = os.environ.get(name)
        if value and value.strip():
            active_env = name
            break
    return {
        "provider": "xai",
        "base_url": get_xai_base_url(),
        "model": get_xai_model(),
        "key_env_var_in_use": active_env,
        "key_present": active_env is not None,
        "supported_env_vars": list(AI_API_KEY_ENV_VARS),
    }


class APIStatusError(Exception):
    """Raised when the upstream API returns a non-2xx response.

    Shaped to mimic ``groq.APIStatusError`` so call sites that branch on
    ``status_code`` (e.g. 429 rate limit, 413 too large) keep working.
    """

    def __init__(self, status_code: int, message: str, response_text: str = ""):
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.response_text = response_text


@dataclass
class _ToolCallFunction:
    name: str
    arguments: str


@dataclass
class _ToolCall:
    id: str
    type: str
    function: _ToolCallFunction


@dataclass
class _Message:
    role: str
    content: Optional[str]
    tool_calls: Optional[list[_ToolCall]] = None


@dataclass
class _Choice:
    index: int
    message: _Message
    finish_reason: Optional[str] = None


@dataclass
class _ChatCompletion:
    id: str
    model: str
    choices: list[_Choice]
    raw: dict[str, Any]


def _parse_response(payload: dict[str, Any]) -> _ChatCompletion:
    raw_choices: Iterable[dict[str, Any]] = payload.get("choices") or []
    choices: list[_Choice] = []
    for idx, ch in enumerate(raw_choices):
        msg = ch.get("message") or {}
        tool_calls_raw = msg.get("tool_calls") or None
        tool_calls: Optional[list[_ToolCall]] = None
        if tool_calls_raw:
            tool_calls = []
            for tc in tool_calls_raw:
                fn = tc.get("function") or {}
                tool_calls.append(
                    _ToolCall(
                        id=tc.get("id", ""),
                        type=tc.get("type", "function"),
                        function=_ToolCallFunction(
                            name=fn.get("name", ""),
                            arguments=fn.get("arguments", "") or "",
                        ),
                    )
                )
        choices.append(
            _Choice(
                index=ch.get("index", idx),
                message=_Message(
                    role=msg.get("role", "assistant"),
                    content=msg.get("content"),
                    tool_calls=tool_calls,
                ),
                finish_reason=ch.get("finish_reason"),
            )
        )
    return _ChatCompletion(
        id=payload.get("id", ""),
        model=payload.get("model", ""),
        choices=choices,
        raw=payload,
    )


class _Completions:
    def __init__(self, client: "XAIClient"):
        self._client = client

    def create(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        tools: Optional[list[dict[str, Any]]] = None,
        tool_choice: Optional[str] = None,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        **extra: Any,
    ) -> _ChatCompletion:
        body: dict[str, Any] = {"model": model, "messages": messages}
        if tools:
            body["tools"] = tools
        if tool_choice is not None:
            body["tool_choice"] = tool_choice
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        if temperature is not None:
            body["temperature"] = temperature
        for k, v in extra.items():
            if v is not None:
                body[k] = v

        url = f"{self._client.base_url}/chat/completions"
        try:
            resp = requests.post(
                url,
                headers={
                    "Authorization": f"Bearer {self._client.api_key}",
                    "Content-Type": "application/json",
                },
                data=json.dumps(body),
                timeout=self._client.timeout,
            )
        except requests.RequestException as e:
            raise APIStatusError(0, f"network error contacting xAI: {e}") from e

        if resp.status_code >= 400:
            text = resp.text or ""
            snippet = text[:500]
            raise APIStatusError(
                resp.status_code,
                f"xAI API error {resp.status_code}: {snippet}",
                response_text=text,
            )

        try:
            payload = resp.json()
        except ValueError as e:
            raise APIStatusError(
                resp.status_code,
                f"xAI returned non-JSON response: {e}",
                response_text=resp.text or "",
            ) from e

        return _parse_response(payload)


class _Chat:
    def __init__(self, client: "XAIClient"):
        self.completions = _Completions(client)


class XAIClient:
    """Thin OpenAI-compatible client for xAI (Grok)."""

    def __init__(
        self,
        api_key: str,
        base_url: Optional[str] = None,
        timeout: Optional[int] = None,
    ):
        if not api_key:
            raise ValueError("xAI API key is required")
        self.api_key = api_key
        self.base_url = (base_url or get_xai_base_url()).rstrip("/")
        self.timeout = timeout or DEFAULT_TIMEOUT_SECONDS
        self.chat = _Chat(self)


def build_client() -> XAIClient:
    """Build an :class:`XAIClient` from environment variables.

    Raises ``RuntimeError`` if no API key is set.
    """
    api_key = get_ai_api_key()
    if not api_key:
        raise RuntimeError(
            "No xAI API key configured. Set one of: "
            + ", ".join(AI_API_KEY_ENV_VARS)
        )
    return XAIClient(api_key=api_key)
