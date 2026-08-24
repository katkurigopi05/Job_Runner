"""LLM provider abstraction — CLAUDE.md §7.

Every model call in the project goes through this Protocol. `StubProvider` is
what tests use: deterministic, offline, and the reason no test can quietly
start depending on a network model.

## Temperature

Every call carries one, and it is a per-task decision made in `router.py`
rather than a global setting, for the same reason the provider is: the tasks
want opposite things.

Classifying an email is picking one word from a fixed set, where variance is
pure downside. Tailoring a bullet is bounded by the fabrication guard, which
throws away anything inventive — a creative model there does not produce
better résumés, it produces a higher rejection rate and a fallback to the
original text, which looks like the tailorer doing nothing. A cover letter is
the one place variance buys something.

Left to the vendors these would all run at whatever each defaults to, which is
around 1.0 for most of them and far too high for the first two.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Protocol, TypeVar

import httpx
import structlog
from pydantic import BaseModel

from packages.llm.audit import record
from packages.llm.pacing import MAX_RETRIES, pacer_for, retry_after_seconds

log = structlog.get_logger(__name__)

T = TypeVar("T", bound=BaseModel)

#: Used when a caller does not say. Low, because most calls in this project
#: are extraction or classification, and the ones that are not ask explicitly.
DEFAULT_TEMPERATURE = 0.2

#: Structured output is never a creative task. `complete_json` pins this
#: regardless of what the caller asked for: the answer has to parse against a
#: schema, and sampling widely only produces more ways to fail that.
JSON_TEMPERATURE = 0.0


class LLMError(Exception):
    """The provider could not produce a usable answer."""


class LLMProvider(Protocol):
    name: str

    async def complete(
        self,
        system: str,
        user: str,
        *,
        max_tokens: int = 1024,
        temperature: float = DEFAULT_TEMPERATURE,
    ) -> str: ...

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
        #: What each call asked for. A stub cannot vary its output by
        #: temperature, but a test can still assert the router passed the
        #: right one — which is the part that would break silently.
        self.temperatures: list[float] = []

    async def complete(
        self,
        system: str,
        user: str,
        *,
        max_tokens: int = 1024,
        temperature: float = DEFAULT_TEMPERATURE,
    ) -> str:
        self.calls.append((system, user))
        self.temperatures.append(temperature)
        # `model=` is load-bearing, not decoration: audit.is_local() reads it
        # to tell `llama3.1` from `kimi-k2.6:cloud`. Dropping it records a
        # cloud call as local, which is the §2.8 trail asserting the opposite
        # of what happened.
        record(self.name, system, user, model=getattr(self, "model", None))
        for needle, response in self.responses.items():
            if needle in user or needle in system:
                return response
        return self.UNKNOWN

    async def complete_json(self, system: str, user: str, schema: type[T]) -> T:
        self.calls.append((system, user))
        record(self.name, system, user, model=getattr(self, "model", None))
        for needle, response in self.responses.items():
            if needle in user or needle in system:
                return schema.model_validate_json(response)
        # No canned answer and no way to invent a valid one — fail loudly.
        raise LLMError(f"StubProvider has no canned JSON response for schema {schema.__name__}")

    def last_prompt(self) -> tuple[str, str] | None:
        return self.calls[-1] if self.calls else None


class OllamaProvider:
    name = "ollama"

    def __init__(self, base_url: str | None = None, model: str | None = None) -> None:
        from packages.core.config import get_settings

        settings = get_settings()
        self.base_url = (
            base_url or os.environ.get("OLLAMA_BASE_URL") or settings.ollama_base_url
        ).rstrip("/")
        self.model = model or os.environ.get("OLLAMA_MODEL") or settings.ollama_model

    async def complete(
        self,
        system: str,
        user: str,
        *,
        max_tokens: int = 1024,
        temperature: float = DEFAULT_TEMPERATURE,
    ) -> str:
        record(self.name, system, user, model=getattr(self, "model", None))
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
                        "options": {"num_predict": max_tokens, "temperature": temperature},
                    },
                    timeout=120.0,
                )
                resp.raise_for_status()
                return str(resp.json()["message"]["content"])
            except Exception as exc:
                raise LLMError(f"Ollama call failed: {_scrubbed(exc)}") from exc

    async def complete_json(self, system: str, user: str, schema: type[T]) -> T:
        record(self.name, system, user, model=getattr(self, "model", None))
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
                        "options": {"num_predict": 1024, "temperature": JSON_TEMPERATURE},
                    },
                    timeout=120.0,
                )
                resp.raise_for_status()
                content = resp.json()["message"]["content"]
                return schema.model_validate_json(content)
            except Exception as exc:
                raise LLMError(f"Ollama JSON call failed: {_scrubbed(exc)}") from exc


#: Gemini authenticates with the key as a *query parameter*, so the key is part
#: of every request URL — and httpx puts the URL into the text of any transport
#: exception it raises. Re-raising that verbatim writes the key into the error,
#: the log, and whatever the owner pastes into an issue. §2.7 says secrets never
#: appear in logs and has no exception for error paths.
#:
#: Found by a real 404: the message carried the full key.
_KEY_QUERY_RE = re.compile(r"([?&](?:key|api_key|access_token)=)[^&\s\'\"]+")

#: Header-style credentials, for providers that authenticate that way.
_KEY_HEADER_RE = re.compile(r"((?i:x-api-key|authorization|bearer)[\"\' :=]+)[A-Za-z0-9._\-]{8,}")


def _scrubbed(exc: Exception) -> str:
    """An exception's text with any `key=` query value removed."""
    scrubbed = _KEY_QUERY_RE.sub(r"\1***", str(exc))
    return _KEY_HEADER_RE.sub(r"\1***", scrubbed)


class GeminiProvider:
    name = "gemini"

    #: Pinned, not an alias. `gemini-flash-latest` never 404s but changes the
    #: model under you, and the audit trail would record the alias rather than
    #: what actually ran — §2.8 wants proof of what left the machine, and
    #: "whatever Google was serving that day" is not proof. The cost of pinning
    #: is that models retire: `gemini-2.5-flash` did, and every call returned
    #: 404 with a message this code then swallowed into a generic failure.
    DEFAULT_MODEL = "gemini-3.6-flash"

    def __init__(self, model: str | None = None) -> None:
        from packages.core.config import get_settings

        self.api_key = os.environ.get("GEMINI_API_KEY") or get_settings().gemini_api_key
        if not self.api_key:
            raise LLMError("GEMINI_API_KEY environment variable is not set")
        self.model = model or get_settings().gemini_model or self.DEFAULT_MODEL
        self.base_url = "https://generativelanguage.googleapis.com/v1beta/models"

    async def complete(
        self,
        system: str,
        user: str,
        *,
        max_tokens: int = 1024,
        temperature: float = DEFAULT_TEMPERATURE,
    ) -> str:
        record(self.name, system, user, model=getattr(self, "model", None))
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"parts": [{"text": user}]}],
            "generationConfig": {
                "maxOutputTokens": max_tokens,
                "temperature": temperature,
            },
        }
        payload = await self._post(body)
        try:
            return str(payload["candidates"][0]["content"]["parts"][0]["text"])
        except (KeyError, IndexError) as exc:
            # A 200 with no candidate is usually a safety block. Saying so
            # beats an IndexError from four frames down.
            raise LLMError(f"Gemini returned no text: {_scrubbed(exc)}") from exc

    async def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        """One request, paced and retried on 429.

        The tailorer makes a call per bullet, so a résumé is five in a row and
        a batch is hundreds. Gemini's free tier limits per minute as well as
        per day, and a tight loop trips the per-minute one — 39 of 60 calls in
        the first real run. Pacing keeps us under it; `Retry-After` is obeyed
        when we still go over.
        """
        pacer = pacer_for(self.name)
        url = f"{self.base_url}/{self.model}:generateContent?key={self.api_key}"

        for attempt in range(MAX_RETRIES + 1):
            await pacer.wait_turn()
            async with httpx.AsyncClient() as client:
                try:
                    resp = await client.post(url, json=body, timeout=60.0)
                except Exception as exc:
                    raise LLMError(f"Gemini call failed: {_scrubbed(exc)}") from exc

                if resp.status_code == 429 and attempt < MAX_RETRIES:
                    await pacer.back_off(attempt, retry_after_seconds(resp.headers))
                    continue

                try:
                    resp.raise_for_status()
                except Exception as exc:
                    raise LLMError(f"Gemini call failed: {_scrubbed(exc)}") from exc
                return dict(resp.json())

        raise LLMError(
            f"Gemini refused {MAX_RETRIES + 1} times with 429 — the account's rate limit is "
            "lower than this workload. Raise LLM_CALL_INTERVAL_S, or run a smaller batch."
        )

    async def complete_json(self, system: str, user: str, schema: type[T]) -> T:
        record(self.name, system, user, model=getattr(self, "model", None))
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
                            "temperature": JSON_TEMPERATURE,
                        },
                    },
                    timeout=60.0,
                )
                resp.raise_for_status()
                content = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
                return schema.model_validate_json(content)
            except Exception as exc:
                raise LLMError(f"Gemini JSON call failed: {_scrubbed(exc)}") from exc


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, model: str = "claude-3-5-sonnet-20241022") -> None:
        from packages.core.config import get_settings

        api_key = os.environ.get("ANTHROPIC_API_KEY") or get_settings().anthropic_api_key
        if not api_key:
            raise LLMError("ANTHROPIC_API_KEY environment variable is not set")
        self.api_key: str = api_key
        self.model = model

    async def complete(
        self,
        system: str,
        user: str,
        *,
        max_tokens: int = 1024,
        temperature: float = DEFAULT_TEMPERATURE,
    ) -> str:
        record(self.name, system, user, model=getattr(self, "model", None))
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
                        "temperature": temperature,
                    },
                    timeout=60.0,
                )
                resp.raise_for_status()
                return str(resp.json()["content"][0]["text"])
            except Exception as exc:
                raise LLMError(f"Anthropic call failed: {_scrubbed(exc)}") from exc

    async def complete_json(self, system: str, user: str, schema: type[T]) -> T:
        record(self.name, system, user, model=getattr(self, "model", None))
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
                        "temperature": JSON_TEMPERATURE,
                    },
                    timeout=60.0,
                )
                resp.raise_for_status()
                content = resp.json()["content"][0]["text"]
                return schema.model_validate_json(content)
            except Exception as exc:
                raise LLMError(f"Anthropic JSON call failed: {_scrubbed(exc)}") from exc


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
