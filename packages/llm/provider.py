"""LLM provider abstraction — CLAUDE.md §7.

Every model call in the project goes through this Protocol. `StubProvider` is
what tests use: deterministic, offline, and the reason no test can quietly
start depending on a network model.
"""

from __future__ import annotations

import json
from typing import Protocol, TypeVar

from pydantic import BaseModel

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
        for needle, response in self.responses.items():
            if needle in user or needle in system:
                return response
        return self.UNKNOWN

    async def complete_json(self, system: str, user: str, schema: type[T]) -> T:
        self.calls.append((system, user))
        for needle, response in self.responses.items():
            if needle in user or needle in system:
                return schema.model_validate_json(response)
        # No canned answer and no way to invent a valid one — fail loudly.
        raise LLMError(f"StubProvider has no canned JSON response for schema {schema.__name__}")

    def last_prompt(self) -> tuple[str, str] | None:
        return self.calls[-1] if self.calls else None


def build_provider(name: str | None = None) -> LLMProvider:
    """Select a provider by name. Only the stub exists until it is needed."""
    from packages.core.config import get_settings

    selected = (name or get_settings().llm_provider).lower()
    if selected == "stub":
        return StubProvider()
    raise LLMError(f"LLM provider {selected!r} is not implemented yet; set LLM_PROVIDER=stub")


def render_json_instruction(schema: type[BaseModel]) -> str:
    """Shared system-prompt fragment for JSON-mode calls."""
    return (
        "Respond with a single JSON object and nothing else. "
        f"It must validate against this schema: {json.dumps(schema.model_json_schema())}"
    )
