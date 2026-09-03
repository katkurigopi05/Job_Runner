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

from packages.llm.audit import CLOUD_MODEL_MARKERS, record
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

    def _headers(self) -> dict[str, str]:
        """Nothing to send. `OllamaCloudProvider` overrides this.

        A hook rather than a conditional so the two request bodies below stay
        single-sourced: they are identical for a local and a hosted model,
        which is the whole reason the two are easy to confuse.
        """
        return {}

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
                    headers=self._headers(),
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
                    headers=self._headers(),
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
    """An exception's text with any `key=` query value removed.

    Falls back to the class name when the text is empty. Several httpx
    timeouts carry no message at all, and the caller interpolates this into
    "<service> call failed: {…}" — which then ends in a colon and tells the
    owner nothing about what went wrong. Found on a slow free route, where the
    real answer was a read timeout and the log said only "TokenRouter call
    failed:".
    """
    scrubbed = _KEY_QUERY_RE.sub(r"\1***", str(exc))
    scrubbed = _KEY_HEADER_RE.sub(r"\1***", scrubbed)
    return scrubbed.strip() or type(exc).__name__


class OllamaCloudProvider(OllamaProvider):
    """A model Ollama hosts on its own servers, reached over the same API.

    Ollama serves cloud-hosted models through the identical `/api/chat`
    endpoint as local ones — `glm-5.3-flash:cloud` and `llama3.1` differ by the
    model tag and nothing else, and the URL is `localhost:11434` either way.
    That is exactly the confusion CLAUDE.md §14 refuses on the assistant, and
    the reason this is a separate provider rather than a model setting.

    ## Why a second class and not just a `:cloud` tag on `OllamaProvider`

    `audit.is_local` reads the provider first and the model second. Under the
    name `ollama` a call is presumed local and only the "cloud" substring in
    the tag rescues the label; under this name it is presumed remote, which is
    the correct default for something whose whole purpose is running elsewhere.
    Two names make the audit trail right by construction instead of by a string
    match that a rename could break.

    It also keeps the §14 refusal intact. Setting `OLLAMA_MODEL` to a `:cloud`
    tag still fails, because asking for the local model and silently getting a
    third party is not a decision. Asking for `ollama_cloud` is.

    ## Deliberately absent from `router.QUALITY_ORDER`

    The same reasoning as `OpenRouterProvider`, and for one more reason
    besides. `_configured("ollama_cloud")` is true as soon as
    `OLLAMA_CLOUD_MODEL` names a model, and Ollama needs no API key when the
    local daemon is signed in — so were this in the quality order, a single
    line in `.env` would silently redirect every "auto" task off the machine.
    §2.8 permits that upload; it does not permit it happening unnoticed. This
    provider answers when named and not otherwise.

    Unlike an OpenRouter `stealth/*` route the recipient here is nameable —
    Ollama's servers, running the tagged model — so the trail can record where
    the résumé went rather than only that it left.
    """

    name = "ollama_cloud"

    #: What the owner asked for. Pinned rather than tracking "latest" for the
    #: reason the other providers pin: the trail should name what actually ran,
    #: and a cloud tag that silently moves under you makes an audited upload
    #: unauditable after the fact.
    DEFAULT_MODEL = "glm-5.3-flash:cloud"

    def __init__(self, base_url: str | None = None, model: str | None = None) -> None:
        from packages.core.config import get_settings

        settings = get_settings()
        self.base_url = (
            base_url or os.environ.get("OLLAMA_BASE_URL") or settings.ollama_base_url
        ).rstrip("/")
        self.model = (
            model or os.environ.get("OLLAMA_CLOUD_MODEL") or settings.ollama_cloud_model
        ) or self.DEFAULT_MODEL
        # The mirror of §14's refusal, and the reason both exist: the label has
        # to match the reality in *both* directions. `ollama` refusing a
        # `:cloud` tag stops a remote call being recorded as local. This stops
        # a local call being recorded as remote — which sounds harmless, and is
        # not: an audit trail that cries wolf about a résumé leaving teaches
        # the owner to stop reading it, and §2.8's whole value is that it gets
        # read.
        if not any(marker in self.model.lower() for marker in CLOUD_MODEL_MARKERS):
            raise LLMError(
                f"OLLAMA_CLOUD_MODEL is set to {self.model!r}, which is not one of the "
                "models Ollama hosts remotely — those carry a 'cloud' tag, such as "
                "'glm-5.3-flash:cloud'. Use OLLAMA_MODEL and the 'ollama' provider for "
                "a model on this machine."
            )
        # Optional. The local daemon proxies cloud models once it is signed in,
        # so there is nothing to send in the common case; a key is only needed
        # when OLLAMA_BASE_URL points at ollama.com directly.
        self.api_key = os.environ.get("OLLAMA_API_KEY") or settings.ollama_api_key

    def _headers(self) -> dict[str, str]:
        if not self.api_key:
            return {}
        return {"Authorization": f"Bearer {self.api_key}"}


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
                            # Pydantic emits `required` at every object level,
                            # which Gemini needs — without it every property is
                            # optional and a partial object comes back with no
                            # error. What it also emits is `$defs`/`$ref` for a
                            # *nested* model, and `responseSchema` has not
                            # historically resolved those. No caller passes a
                            # nested schema today, so this is a landmine rather
                            # than a bug: the first one that does should check
                            # the response before trusting it, and inline the
                            # definitions if Gemini rejects the reference.
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


#: Keep the reasoning trace out of the response.
#:
#: Not a preference — a budget fix, measured. `tailor_bullets` caps a rewrite at
#: `max_tokens=300`, and a reasoning model spends that allowance thinking before
#: it writes anything: one bullet through `stealth/ox-alpha` returned
#: `completion_tokens=299` with the trace included, right against the cap, so
#: the answer came back empty often enough to look like the tailorer doing
#: nothing. Excluding the trace took the same call to 176.
#:
#: Excluding is the only lever this endpoint offers. Both `{"enabled": False}`
#: and `{"max_tokens": 0}` are refused with *"Reasoning is mandatory for this
#: endpoint and cannot be disabled"*, so the choice is whether the trace comes
#: back, not whether it happens.
#:
#: Nothing is lost by dropping it: only the final text is ever written into a
#: résumé, and §10 would forbid logging the trace anyway — it quotes the
#: bullets it is reasoning about, which are résumé contents.
_REASONING_EXCLUDED = {"reasoning": {"exclude": True}}

#: Extra completion budget for reasoning this endpoint will not let us switch off.
#:
#: `max_tokens` here bounds *reasoning plus answer*, while every caller in this
#: repo means it as an answer budget — `tailor_bullets` passes 300 to keep a
#: rewritten bullet roughly bullet-sized. Passing that through unchanged
#: misapplies it, and the failure is silent in the worst way: measured on
#: `stealth/ox-alpha`, a 300-token call comes back `finish_reason="length"`,
#: `completion_tokens=300`, and **empty content** — the model spent the whole
#: allowance thinking and was cut off before writing anything. The tailorer
#: catches the error and keeps the original line, so the visible symptom is a
#: tailorer that appears to do nothing.
#:
#: The same call at 1200 returns `finish_reason="stop"` and a 53-character
#: bullet for 296-299 completion tokens. So reasoning costs ~250 and the answer
#: ~50, and 300 sits exactly on the boundary — which is why this failed
#: intermittently rather than always.
#:
#: Headroom rather than a larger fixed budget, so the caller's number still
#: means what it says. Nothing stops a model using the slack for a longer
#: answer, but `vet()` already rejects a rewrite disproportionately longer than
#: its source, so length stays bounded by the guard rather than by this.
REASONING_HEADROOM_TOKENS = 1024


class _OpenAICompatibleProvider:
    """Shared machinery for an OpenAI-compatible `/chat/completions` gateway.

    Not selectable itself — `name` is empty and `build_provider` never returns
    one. A subclass supplies the endpoint, the key it reads, and the service
    name that appears in errors.

    It exists because the second such gateway would otherwise have been a
    hundred and fifty copied lines: the pacing, the 429 retry, and the
    empty-content handling below were each written for a defect we hit in
    production, and two copies means fixing the next one twice. What is
    deliberately *not* shared is identity — every gateway is its own named
    provider, because §2.8 asks the audit trail to say where a résumé went and
    a base class with a settable base URL would make that "wherever `.env` last
    pointed".

    What every subclass inherits, and must keep true of itself:

    - **Absent from `router.QUALITY_ORDER`.** These forward to an upstream they
      do not name. Setting a key must not silently redirect every "auto" task
      to a model whose vendor is unknown; the provider answers when asked for
      by name, via `LLM_PROVIDER` or one of §7's per-task settings.
    - **Free routes are the ones to read the data policy for.** They commonly
      log prompts and share them with the upstream creator, which for résumé
      PII is a different bargain from a paid tier.
    - **No leaderboard headers.** `HTTP-Referer` and `X-Title` list an app on a
      public ranking, and §1 is a private single-user tool.
    """

    #: Set by each subclass. Empty here so the base is never selectable.
    name = ""
    #: Human-readable service name, used only in error messages.
    SERVICE = ""
    #: The `/chat/completions` prefix.
    BASE_URL = ""
    #: Environment variable holding the key, and the `Settings` fields to
    #: fall back to. Named rather than derived so a typo is a NameError
    #: here instead of a provider that silently reads nothing.
    KEY_ENV = ""
    KEY_SETTING = ""
    MODEL_ENV = ""
    MODEL_SETTING = ""
    #: Seconds to wait for one completion. A class attribute rather than a
    #: literal because these gateways are not comparable: a paid route answers
    #: a bullet in seconds, and a free one queues behind everyone else using
    #: it. Too low is the worse failure — the call is abandoned after the
    #: upstream has already done the work, so the owner pays the latency and
    #: gets nothing.
    REQUEST_TIMEOUT_S = 120.0
    #: Tokens added to the caller's `max_tokens` to pay for reasoning.
    #:
    #: Per provider because the routes are not comparable, and getting it wrong
    #: is silent: the call returns 200 with empty content and the tailorer keeps
    #: the original line, so the symptom is a tailorer that appears to do
    #: nothing. Measured per route on a *real* tailoring prompt — a toy prompt
    #: reasons far less and will happily suggest a number that fails in
    #: production.
    REASONING_HEADROOM = REASONING_HEADROOM_TOKENS

    #: Pinned for the same reason Gemini's is — the trail should name what ran.
    #: `stealth/*` routes are pre-release and get withdrawn without notice, at
    #: which point every call 404s; §7's `LLM_FALLBACK_LOCAL` is what keeps that
    #: from stopping tailoring, and the fallback is recorded so a résumé written
    #: by the local model after a withdrawal is not mistaken for this one's work.
    DEFAULT_MODEL = "stealth/ox-alpha"

    def __init__(self, model: str | None = None) -> None:
        from packages.core.config import get_settings

        settings = get_settings()
        self.api_key = os.environ.get(self.KEY_ENV) or getattr(settings, self.KEY_SETTING, None)
        if not self.api_key:
            raise LLMError(f"{self.KEY_ENV} environment variable is not set")
        self.model = (
            model or os.environ.get(self.MODEL_ENV) or getattr(settings, self.MODEL_SETTING, None)
        ) or self.DEFAULT_MODEL
        self.base_url = self.BASE_URL

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def _missing_route_message(self) -> str:
        """What a 404 means on this gateway, and the one line that fixes it.

        Overridden per subclass because the remedy differs: OpenRouter
        withdraws pre-release routes, TokenRouter has a paid model whose id
        differs from the free one by a suffix. A generic "404 not found" reads
        as a bug in this code or a bad key, and the tailorer turns that into a
        résumé that silently went untailored.
        """
        return (
            f"{self.SERVICE} has no model named {self.model!r} (404). Set "
            f"{self.MODEL_ENV} in .env to one it serves."
        )

    async def complete(
        self,
        system: str,
        user: str,
        *,
        max_tokens: int = 1024,
        temperature: float = DEFAULT_TEMPERATURE,
    ) -> str:
        record(self.name, system, user, model=getattr(self, "model", None))
        payload = await self._post(
            {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "max_tokens": max_tokens + self.REASONING_HEADROOM,
                "temperature": temperature,
                **_REASONING_EXCLUDED,
            }
        )
        return self._content(payload)

    def _content(self, payload: dict[str, Any]) -> str:
        """The assistant text, or a failure that says what actually happened.

        Reasoning models on this endpoint spend `max_tokens` on thinking before
        they write anything, and a budget that runs out mid-thought returns a
        200 with an empty `content` and no error. Left to the generic path that
        surfaces as an unhelpful KeyError or, worse, an empty rewrite that the
        tailorer treats as a real answer. `reasoning_details` in the response is
        ignored on purpose: only the final text belongs in a résumé.
        """
        try:
            message = payload["choices"][0]["message"]
        except (KeyError, IndexError) as exc:
            raise LLMError(f"{self.SERVICE} returned no choices: {_scrubbed(exc)}") from exc

        content = (message.get("content") or "").strip()
        if not content:
            finish = payload.get("choices", [{}])[0].get("finish_reason")
            detail = (
                " The response was cut off at the token limit "
                f"(finish_reason={finish!r}), so raise REASONING_HEADROOM_TOKENS."
                if finish == "length"
                else f" (finish_reason={finish!r})"
            )
            raise LLMError(
                f"{self.model} returned no text.{detail} Reasoning models spend max_tokens "
                "on thinking before they write, and this endpoint refuses to let reasoning "
                "be disabled."
            )
        return str(content)

    async def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        """One request, paced and retried on 429 — as Gemini's, and for the same
        reason: the tailorer makes a call per bullet, so a résumé is several in
        a row and a batch is hundreds. Free routes rate-limit hard.
        """
        pacer = pacer_for(self.name)
        url = f"{self.base_url}/chat/completions"

        for attempt in range(MAX_RETRIES + 1):
            await pacer.wait_turn()
            async with httpx.AsyncClient() as client:
                try:
                    resp = await client.post(
                        url, json=body, headers=self._headers(), timeout=self.REQUEST_TIMEOUT_S
                    )
                except Exception as exc:
                    raise LLMError(f"{self.SERVICE} call failed: {_scrubbed(exc)}") from exc

                if resp.status_code == 429 and attempt < MAX_RETRIES:
                    await pacer.back_off(attempt, retry_after_seconds(resp.headers))
                    continue

                if resp.status_code == 404:
                    # The failure `.env.example` predicts, said out loud.
                    #
                    # A pre-release route is withdrawn without notice and every
                    # call 404s from then on. Reported as a bare "404 Not Found"
                    # it reads like a bug in this code or a bad key, and the
                    # tailorer's own error handling turns it into a résumé that
                    # silently went untailored. The remedy is one line in
                    # `.env`, so the error should say which line.
                    raise LLMError(self._missing_route_message())

                try:
                    resp.raise_for_status()
                except Exception as exc:
                    raise LLMError(f"{self.SERVICE} call failed: {_scrubbed(exc)}") from exc
                return dict(resp.json())

        raise LLMError(
            f"{self.SERVICE} refused {MAX_RETRIES + 1} times with 429 — this route's rate limit is "
            "lower than this workload. Raise LLM_CALL_INTERVAL_S, or run a smaller batch."
        )

    async def complete_json(self, system: str, user: str, schema: type[T]) -> T:
        record(self.name, system, user, model=getattr(self, "model", None))
        payload = await self._post(
            {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": f"{system}\n\n{render_json_instruction(schema)}"},
                    {"role": "user", "content": user},
                ],
                # The schema goes in the prompt as well as here. Not every model
                # behind this endpoint honours `response_format`, and one that
                # ignores it silently returns prose that fails validation with
                # no hint as to why.
                "response_format": {"type": "json_object"},
                "max_tokens": 1024 + self.REASONING_HEADROOM,
                "temperature": JSON_TEMPERATURE,
                **_REASONING_EXCLUDED,
            }
        )
        try:
            return schema.model_validate_json(self._content(payload))
        except LLMError:
            raise
        except Exception as exc:
            raise LLMError(f"{self.SERVICE} JSON call failed: {_scrubbed(exc)}") from exc


class OpenRouterProvider(_OpenAICompatibleProvider):
    """openrouter.ai — one key, many upstream models.

    The audit trail records `openrouter` and the model id, and that is
    genuinely where its knowledge stops: OpenRouter forwards to an upstream
    provider, and for a cloaked `stealth/*` route the identity of that provider
    is undisclosed by design. The trail can name the hop but not the
    destination.
    """

    name = "openrouter"
    SERVICE = "OpenRouter"
    BASE_URL = "https://openrouter.ai/api/v1"
    KEY_ENV = "OPENROUTER_API_KEY"
    KEY_SETTING = "openrouter_api_key"
    MODEL_ENV = "OPENROUTER_MODEL"
    MODEL_SETTING = "openrouter_model"
    DEFAULT_MODEL = "stealth/ox-alpha"

    def _missing_route_message(self) -> str:
        return (
            f"OpenRouter has no route named {self.model!r} (404). Pre-release "
            "and stealth routes are withdrawn without notice, and every call "
            "404s once that happens. Set OPENROUTER_MODEL in .env to a route "
            "that still exists — https://openrouter.ai/models lists them."
        )


class TokenRouterProvider(_OpenAICompatibleProvider):
    """api.tokenrouter.com — a second gateway, same bargain as the first.

    Added because its `z-ai/glm-5.3-free` route is genuinely free, which the
    GLM routes on OpenRouter are not: of eighteen `z-ai/*` ids there, exactly
    one prices at zero. Both gateways being reachable is also the point of
    §7's comparison panel — two free routes are two columns.

    **Reasoning is on and is billed against `max_tokens`,** the same trap
    `REASONING_HEADROOM_TOKENS` was written for on OpenRouter, and measured
    again here before this class existed. A trivial "reply OK" call spent 48 of
    50 completion tokens thinking. At the tailorer's 300 the model spent all
    300 on reasoning and returned `finish_reason="length"` with **empty
    content** — a tailorer that appears to do nothing. The same call at
    300 + 1024 headroom returns `finish_reason="stop"`, 398 reasoning tokens
    and a real bullet, so the existing constant is sufficient and is not
    re-tuned here.

    It is slow: ~26s for a two-token answer, and a bullet rewrite is longer.
    A résumé is one call per bullet, so a batch wants patience or a smaller one.
    """

    name = "tokenrouter"
    SERVICE = "TokenRouter"
    BASE_URL = "https://api.tokenrouter.com/v1"
    KEY_ENV = "TOKENROUTER_API_KEY"
    KEY_SETTING = "tokenrouter_api_key"
    MODEL_ENV = "TOKENROUTER_MODEL"
    MODEL_SETTING = "tokenrouter_model"
    #: The free GLM route. Named rather than inherited from a shorter alias
    #: because the paid `z-ai/glm-5.3` differs from it by one suffix, and the
    #: cost of confusing them lands on the owner's card.
    DEFAULT_MODEL = "z-ai/glm-5.3-free"
    #: Measured, not guessed. A two-token answer took 26s; a bullet rewrite
    #: with the reasoning headroom exceeded the inherited 120s and failed as a
    #: read timeout *after* the upstream had generated the answer. Free routes
    #: queue, so the wait is the price of the route rather than a fault.
    REQUEST_TIMEOUT_S = 300.0
    #: Three times the inherited allowance, and measured on the real prompt
    #: rather than a toy one — which is the mistake that made this necessary.
    #:
    #: A 89-character probe reasoned for 398 tokens, so 1024 looked ample. The
    #: actual tailoring prompt is ~6k characters (the posting, plus the
    #: supported and off-limits term lists), and on that this route spends
    #: **2,982 reasoning tokens to write a 48-token bullet**. At the inherited
    #: allowance every real call returned `finish_reason="length"` with empty
    #: content, and all three test bullets fell back to the original line —
    #: a tailorer that appears to do nothing, exactly as the constant above
    #: warns.
    REASONING_HEADROOM = 4096

    def _missing_route_message(self) -> str:
        return (
            f"TokenRouter has no model named {self.model!r} (404). Set "
            "TOKENROUTER_MODEL in .env to one it serves — GET /v1/models lists "
            "them. Note the free GLM route is 'z-ai/glm-5.3-free'; dropping the "
            "'-free' suffix selects the paid model of the same name."
        )


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
    elif selected == "openrouter":
        return OpenRouterProvider()
    elif selected == "tokenrouter":
        return TokenRouterProvider()
    elif selected == "ollama_cloud":
        return OllamaCloudProvider()
    else:
        raise LLMError(f"LLM provider {selected!r} is unknown or not implemented.")


def render_json_instruction(schema: type[BaseModel]) -> str:
    """Shared system-prompt fragment for JSON-mode calls."""
    return (
        "Respond with a single JSON object and nothing else. "
        f"It must validate against this schema: {json.dumps(schema.model_json_schema())}"
    )
