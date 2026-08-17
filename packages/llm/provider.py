"""LLM provider abstraction — CLAUDE.md §7.

Every model call in the project goes through this Protocol. `StubProvider` is
what tests use: deterministic, offline, and the reason no test can quietly
start depending on a network model.
"""

from __future__ import annotations

import json
import os
from typing import Protocol, TypeVar

import httpx
import structlog
from pydantic import BaseModel

from packages.llm.audit import record

log = structlog.get_logger(__name__)

T = TypeVar("T", bound=BaseModel)


class LLMError(Exception):
    """The provider could not produce a usable answer."""


class LLMProvider(Protocol):
    name: str

    async def complete(self, system: str, user: str, *, max_tokens: int = 1024) -> str: ...

    async def complete_json(self, system: str, user: str, schema: type[T]) -> T: ...


class StubProvider:
    """Deterministic canned output. No network, ever.

    Responses are keyed by substrings so a test can assert on a specific task
    without pattern-matching a real model's prose. Anything unrecognized
    returns a fixed marker rather than something plausible-looking — a stub
    that invents realistic text makes fabrication bugs invisible.
    """

    name = "stub"

    #: Marker returned for prompts with no canned answer. Deliberately obvious.
    UNKNOWN = "[stub: no canned response]"

    def __init__(self, responses: dict[str, str] | None = None) -> None:
        self.responses = responses or {}
        #: Every call, for assertions and for the audit trail (CLAUDE.md §2.8).
        self.calls: list[tuple[str, str]] = []

    async def complete(self, system: str, user: str, *, max_tokens: int = 1024) -> str:
        self.calls.append((system, user))
        record(self.name, system, user)
        for needle, response in self.responses.items():
            if needle in user or needle in system:
                return response
        return self.UNKNOWN

    async def complete_json(self, system: str, user: str, schema: type[T]) -> T:
        self.calls.append((system, user))
        record(self.name, system, user)
        for needle, response in self.responses.items():
            if needle in user or needle in system:
                return schema.model_validate_json(response)
        # No canned answer and no way to invent a valid one — fail loudly.
        raise LLMError(f"StubProvider has no canned JSON response for schema {schema.__name__}")

    def last_prompt(self) -> tuple[str, str] | None:
        return self.calls[-1] if self.calls else None


class OllamaProvider:
    name = "ollama"

    def __init__(self, base_url: str | None = None, model: str = "llama3.1") -> None:
        self.base_url = (
            base_url or os.environ.get("OLLAMA_BASE_URL") or "http://localhost:11434"
        ).rstrip("/")
        self.model = model

    async def complete(self, system: str, user: str, *, max_tokens: int = 1024) -> str:
        record(self.name, system, user)
        async with httpx.AsyncClient() as client:
            try:
                resp = await client.post(
                    f"{self.base_url}/api/chat",
                    json={
                        "model": self.model,
                        "messages": [
                            {"role": "system", "content": system},
                            {"role": "user", "content": user},
                        ],
                        "stream": False,
                        "options": {"num_predict": max_tokens},
                    },
                    timeout=120.0,
                )
                resp.raise_for_status()
                return str(resp.json()["message"]["content"])
            except Exception as exc:
                raise LLMError(f"Ollama call failed: {exc}") from exc

    async def complete_json(self, system: str, user: str, schema: type[T]) -> T:
        record(self.name, system, user)
        system_with_json = f"{system}\n\n{render_json_instruction(schema)}"
        async with httpx.AsyncClient() as client:
            try:
                resp = await client.post(
                    f"{self.base_url}/api/chat",
                    json={
                        "model": self.model,
                        "messages": [
                            {"role": "system", "content": system_with_json},
                            {"role": "user", "content": user},
                        ],
                        "stream": False,
                        "format": "json",
                        "options": {"num_predict": 1024},
                    },
                    timeout=120.0,
                )
                resp.raise_for_status()
                content = resp.json()["message"]["content"]
                return schema.model_validate_json(content)
            except Exception as exc:
                raise LLMError(f"Ollama JSON call failed: {exc}") from exc


class GeminiProvider:
    name = "gemini"

    def __init__(self, model: str = "gemini-2.5-flash") -> None:
        self.api_key = os.environ.get("GEMINI_API_KEY")
        if not self.api_key:
            raise LLMError("GEMINI_API_KEY environment variable is not set")
        self.model = model
        self.base_url = "https://generativelanguage.googleapis.com/v1beta/models"

    async def complete(self, system: str, user: str, *, max_tokens: int = 1024) -> str:
        record(self.name, system, user)
        async with httpx.AsyncClient() as client:
            try:
                resp = await client.post(
                    f"{self.base_url}/{self.model}:generateContent?key={self.api_key}",
                    json={
                        "systemInstruction": {"parts": [{"text": system}]},
                        "contents": [{"parts": [{"text": user}]}],
                        "generationConfig": {"maxOutputTokens": max_tokens},
                    },
                    timeout=60.0,
                )
                resp.raise_for_status()
                return str(resp.json()["candidates"][0]["content"]["parts"][0]["text"])
            except Exception as exc:
                raise LLMError(f"Gemini call failed: {exc}") from exc

    async def complete_json(self, system: str, user: str, schema: type[T]) -> T:
        record(self.name, system, user)
        async with httpx.AsyncClient() as client:
            try:
                resp = await client.post(
                    f"{self.base_url}/{self.model}:generateContent?key={self.api_key}",
                    json={
                        "systemInstruction": {"parts": [{"text": system}]},
                        "contents": [{"parts": [{"text": user}]}],
                        "generationConfig": {
                            "responseMimeType": "application/json",
                            "responseSchema": schema.model_json_schema(),
                        },
                    },
                    timeout=60.0,
                )
                resp.raise_for_status()
                content = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
                return schema.model_validate_json(content)
            except Exception as exc:
                raise LLMError(f"Gemini JSON call failed: {exc}") from exc


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, model: str = "claude-3-5-sonnet-20241022") -> None:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise LLMError("ANTHROPIC_API_KEY environment variable is not set")
        self.api_key: str = api_key
        self.model = model

    async def complete(self, system: str, user: str, *, max_tokens: int = 1024) -> str:
        record(self.name, system, user)
        async with httpx.AsyncClient() as client:
            try:
                resp = await client.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={
                        "x-api-key": self.api_key,
                        "anthropic-version": "2023-06-01",
                        "content-type": "application/json",
                    },
                    json={
                        "model": self.model,
                        "system": system,
                        "messages": [{"role": "user", "content": user}],
                        "max_tokens": max_tokens,
                    },
                    timeout=60.0,
                )
                resp.raise_for_status()
                return str(resp.json()["content"][0]["text"])
            except Exception as exc:
                raise LLMError(f"Anthropic call failed: {exc}") from exc

    async def complete_json(self, system: str, user: str, schema: type[T]) -> T:
        record(self.name, system, user)
        system_with_json = f"{system}\n\n{render_json_instruction(schema)}"
        async with httpx.AsyncClient() as client:
            try:
                resp = await client.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={
                        "x-api-key": self.api_key,
                        "anthropic-version": "2023-06-01",
                        "content-type": "application/json",
                    },
                    json={
                        "model": self.model,
                        "system": system_with_json,
                        "messages": [{"role": "user", "content": user}],
                        "max_tokens": 1024,
                    },
                    timeout=60.0,
                )
                resp.raise_for_status()
                content = resp.json()["content"][0]["text"]
                return schema.model_validate_json(content)
            except Exception as exc:
                raise LLMError(f"Anthropic JSON call failed: {exc}") from exc


def build_provider(name: str | None = None) -> LLMProvider:
    """Select a provider by name. Only the stub exists until it is needed."""
    from packages.core.config import get_settings

    selected = (name or get_settings().llm_provider).lower()
    if not selected:
        raise LLMError("LLM_PROVIDER is unset or empty")

    if selected == "stub":
        return StubProvider()
    elif selected == "ollama":
        return OllamaProvider()
    elif selected == "gemini":
        return GeminiProvider()
    elif selected == "anthropic":
        return AnthropicProvider()
    else:
        raise LLMError(f"LLM provider {selected!r} is unknown or not implemented.")


def render_json_instruction(schema: type[BaseModel]) -> str:
    """Shared system-prompt fragment for JSON-mode calls."""
    return (
        "Respond with a single JSON object and nothing else. "
        f"It must validate against this schema: {json.dumps(schema.model_json_schema())}"
    )
