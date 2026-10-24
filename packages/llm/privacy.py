"""Mask contact identifiers at the shared cloud-provider boundary.

Production callers use ``build_provider``; raw transports are for isolated
transport tests. Mappings live only for one call and never enter a prompt,
log or database. This is contact minimization, not general anonymization:
unknown people's names and identifying employment history are not detected.
"""

from __future__ import annotations

import json
import re
import secrets
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, TypeVar, cast
from urllib.parse import unquote, urlsplit

from pydantic import BaseModel
from sqlalchemy import select

from packages.llm.audit import is_local

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from packages.llm.provider import LLMProvider

T = TypeVar("T", bound=BaseModel)
_EMAIL = re.compile(r"[\w.!#$%&'*+/=?^`{|}~-]+@[\w-]+(?:\.[\w-]+)+", re.UNICODE)
# Horizontal whitespace only: dates and numbers on adjacent lines are not a
# phone. A country prefix or ten digits is required; years/metrics stay facts.
_PHONE = re.compile(
    r"(?<!\w)(?:\+\d(?:[ \t().-]*\d){6,14}|"
    r"(?:\(\d{2,4}\)|\d{2,4})[ \t.-]*\d{3,4}[ \t.-]*\d{3,4})"
    r"(?:[ \t]*(?:ext\.?|x)[ \t]*\d{1,6})?(?![\w\d])",
    re.IGNORECASE,
)
_URL = re.compile(r"(?:https?://|mailto:)[^\s<>\"]+", re.IGNORECASE)


@dataclass(frozen=True, repr=False)
class Identifiers:
    """Locally stored contacts; repr must never reveal their values."""

    names: tuple[str, ...] = field(default=(), repr=False)
    emails: tuple[str, ...] = field(default=(), repr=False)
    phones: tuple[str, ...] = field(default=(), repr=False)


async def read_identifiers(session: AsyncSession) -> Identifiers:
    """Read contact columns only, including unselected résumé versions.

    This single-owner application can send any profile/version to a cloud
    task. Read all their contacts, not just the dashboard's selected profile.
    Neither résumé bodies nor profile employment/authorization answers are
    needed to build this dictionary.
    """
    from packages.core.models import Candidate, Profile, Resume, User

    names: set[str] = set()
    emails: set[str] = set()
    phones: set[str] = set()

    def add(target: set[str], value: Any) -> None:
        if isinstance(value, str) and value.strip():
            target.add(value.strip())

    for name, email, alias in await session.execute(
        select(Candidate.name, Candidate.email, Candidate.managed_alias)
    ):
        add(names, name)
        add(emails, email)
        add(emails, alias)
    for email in await session.scalars(select(User.email)):
        add(emails, email)
    name_keys = ("name", "full_name", "first_name", "last_name", "preferred_name")
    email_keys = ("email", "email_address")
    phone_keys = ("phone", "phone_number", "mobile", "mobile_number")
    keys = (*name_keys, *email_keys, *phone_keys)
    for row in await session.execute(
        select(Profile.phone, *(Profile.answers_kv_json[key] for key in keys))
    ):
        add(phones, row[0])
        for key, value in zip(keys, row[1:], strict=True):
            add(names if key in name_keys else emails if key in email_keys else phones, value)
    for contact in await session.scalars(select(Resume.parsed_json["contact"])):
        if isinstance(contact, dict):
            add(names, contact.get("name"))
            add(emails, contact.get("email"))
            add(phones, contact.get("phone"))
    return Identifiers(tuple(sorted(names)), tuple(sorted(emails)), tuple(sorted(phones)))


async def load_identifiers() -> Identifiers:
    from packages.core.db import get_sessionmaker

    async with get_sessionmaker()() as session:
        return await read_identifiers(session)


class Redactor:
    """Reversible contact substitutions with an unguessable per-call prefix."""

    def __init__(self, identifiers: Identifiers) -> None:
        self.prefix = f"[JR_PRIVATE_{secrets.token_hex(8)}_"
        self._originals: dict[str, str] = {}
        self._tokens: dict[str, str] = {}
        words = set(identifiers.names)
        for name in identifiers.names:
            parts = name.split()
            words.update(part for part in parts if len(part) > 1)
            words.add("-".join(parts))
            words.add("_".join(parts))
            words.add("".join(parts))
        # Also cover explicitly known contacts in nonstandard formats.
        words.update(identifiers.emails)
        words.update(identifiers.phones)
        self._json_values = {json.dumps(word, ensure_ascii=True)[1:-1]: word for word in words}
        words.update(self._json_values)
        self._known = (
            re.compile(
                r"(?<!\w)(?:"
                + "|".join(re.escape(w) for w in sorted(words, key=len, reverse=True))
                + r")(?!\w)",
                re.IGNORECASE,
            )
            if words
            else None
        )

    def _token(self, value: str) -> str:
        if value not in self._tokens:
            token = f"{self.prefix}{len(self._tokens)}]"
            self._tokens[value] = token
            self._originals[token] = value
        return self._tokens[value]

    def _mask(self, text: str) -> str:
        text = _EMAIL.sub(lambda match: self._token(match[0]), text)

        def phone(match: re.Match[str]) -> str:
            value = match[0]
            digits = re.sub(r"\D", "", value.split("ext")[0])
            return self._token(value) if value.startswith("+") or len(digits) >= 10 else value

        text = _PHONE.sub(phone, text)
        return self._known.sub(lambda match: self._token(match[0]), text) if self._known else text

    def mask(self, text: str) -> str:
        def url(match: re.Match[str]) -> str:
            original = match[0]
            decoded = original
            for _ in range(3):
                decoded = unquote(decoded)
            # Keep the exact encoded URL for local restoration. Never normalize
            # an unrelated URL, or transmit identifying path/query fragments.
            return self._token(original) if self._mask(decoded) != decoded else original

        return self._mask(_URL.sub(url, text))

    def restore(self, text: str, *, json_value: bool = False) -> str:
        # One substitution pass: inserted originals cannot themselves expand
        # another marker. Only this call's exact markers have any authority.
        from packages.llm.provider import LLMError

        pattern = re.compile("|".join(re.escape(token) for token in self._originals) or r"(?!)")
        if "JR_PRIVATE_" in pattern.sub("", text):
            raise LLMError("Cloud answer contains a damaged privacy placeholder.")

        def original(match: re.Match[str]) -> str:
            value = self._originals[match[0]]
            return self._json_values.get(value, value) if json_value else value

        return pattern.sub(original, text)

    async def restore_stream(self, pieces: AsyncGenerator[str, None]) -> AsyncGenerator[str, None]:
        """Hold incomplete markers even when a transport splits every byte."""
        pending = ""
        starts = ("[JR_PRIVATE_", "JR_PRIVATE_")
        try:
            async for piece in pieces:
                pending += piece
                while pending:
                    at = min((i for start in starts if (i := pending.find(start)) >= 0), default=-1)
                    if at < 0:
                        keep = next(
                            (
                                n
                                for n in range(min(len(pending), max(map(len, starts))), 0, -1)
                                if any(
                                    pending.endswith(start[:n])
                                    for start in starts
                                    if len(start) >= n
                                )
                            ),
                            0,
                        )
                        ready = pending[:-keep] if keep else pending
                        pending = pending[-keep:] if keep else ""
                        if ready:
                            yield ready
                        break
                    if at:
                        yield pending[:at]
                        pending = pending[at:]
                    end = pending.find("]")
                    if end < 0:
                        if len(pending) > len(self.prefix) + 20:
                            self.restore(pending)  # refuse rather than buffering without a bound
                        break
                    yield self.restore(pending[: end + 1])
                    pending = pending[end + 1 :]
            if pending:
                yield self.restore(pending)
        finally:
            await pieces.aclose()


def _walk(value: Any, transform: Callable[[str], str]) -> Any:
    if isinstance(value, str):
        return transform(value)
    if isinstance(value, list):
        return [_walk(item, transform) for item in value]
    if isinstance(value, dict):
        return {transform(key): _walk(item, transform) for key, item in value.items()}
    return value


def _guarded_schema[T: BaseModel](schema: type[T], redactor: Redactor) -> type[T]:
    """Adapt the transport's schema without changing vendor JSON handling.

    Defaults/enums/aliases are outbound data too. Restore JSON strings before
    local EmailStr/enum validators, and keep validation errors free of inputs.
    A per-call subclass avoids mutable global state across concurrent requests
    and also works for new transports using the standard schema interface.
    """

    def mask_schema(value: Any) -> Any:
        if isinstance(value, dict):
            result = {}
            for key, item in value.items():
                if key in ("type", "format"):
                    # A profile name such as "String" must not change the
                    # protocol's type/format vocabulary into placeholders.
                    result[key] = item
                elif key in ("default", "const", "enum", "examples"):
                    result[key] = _walk(item, redactor.mask)
                elif key in (
                    "properties",
                    "$defs",
                    "definitions",
                    "patternProperties",
                    "dependentSchemas",
                    "dependentRequired",
                    "mapping",
                ) and isinstance(item, dict):
                    result[key] = {redactor.mask(k): mask_schema(v) for k, v in item.items()}
                else:
                    # JSON Schema keywords are protocol, not profile names.
                    result[key] = mask_schema(item)
            return result
        if isinstance(value, list):
            return [mask_schema(item) for item in value]
        return redactor.mask(value) if isinstance(value, str) else value

    def json_schema(cls: type[Any], /, **kwargs: Any) -> dict[str, Any]:
        return dict(mask_schema(schema.model_json_schema(**kwargs)))

    def validate(cls: type[Any], /, value: Any, **kwargs: Any) -> T:
        from packages.llm.provider import LLMError

        try:
            return schema.model_validate(
                _walk(value, lambda text: redactor.restore(text, json_value=True)), **kwargs
            )
        except ValueError:
            # Pydantic validation errors otherwise include the restored input.
            raise LLMError("Cloud JSON answer failed local privacy guard validation.") from None

    def validate_json(cls: type[Any], /, value: str | bytes | bytearray, **kwargs: Any) -> T:
        from packages.llm.provider import LLMError

        try:
            decoded = json.loads(value)
        except ValueError:
            raise LLMError("Cloud JSON answer failed local privacy guard validation.") from None
        return validate(cls, decoded, **kwargs)

    return cast(
        type[T],
        type(
            schema.__name__,
            (schema,),
            {
                "model_json_schema": classmethod(json_schema),
                "model_validate": classmethod(validate),
                "model_validate_json": classmethod(validate_json),
            },
        ),
    )


class PrivacyProvider:
    """Wrap a cloud transport once; routing, auditing and budgets stay intact."""

    def __init__(self, inner: LLMProvider) -> None:
        self.inner = inner
        self.name = inner.name

    def __getattr__(self, key: str) -> Any:
        return getattr(self.inner, key)

    async def _prepare(self, system: str, user: str) -> tuple[Redactor, str, str]:
        from packages.llm.provider import LLMError

        try:
            redactor = Redactor(await load_identifiers())
            return redactor, redactor.mask(system), redactor.mask(user)
        except Exception:
            raise LLMError(
                "Cloud call refused: privacy guard could not read or mask contacts."
            ) from None

    async def complete(
        self,
        system: str,
        user: str,
        *,
        max_tokens: int = 1024,
        temperature: float = 0.2,
    ) -> str:
        redactor, system, user = await self._prepare(system, user)
        answer = await self.inner.complete(
            system, user, max_tokens=max_tokens, temperature=temperature
        )
        return redactor.restore(answer)

    async def complete_json(self, system: str, user: str, schema: type[T]) -> T:
        redactor, system, user = await self._prepare(system, user)
        return await self.inner.complete_json(system, user, _guarded_schema(schema, redactor))

    async def stream(
        self,
        system: str,
        user: str,
        *,
        max_tokens: int = 1024,
        temperature: float = 0.2,
    ) -> AsyncGenerator[str, None]:
        from packages.llm.provider import stream_text

        redactor, system, user = await self._prepare(system, user)
        pieces = redactor.restore_stream(
            stream_text(self.inner, system, user, max_tokens=max_tokens, temperature=temperature)
        )
        try:
            async for piece in pieces:
                yield piece
        finally:
            await pieces.aclose()


def protect_provider(provider: LLMProvider) -> LLMProvider:
    """Unknown providers are remote by default, including hosted notebooks."""
    local = is_local(provider.name, getattr(provider, "model", None))
    if local and provider.name == "ollama":
        # A non-cloud model on somebody else's Ollama host still leaves this
        # machine. Only a loopback daemon qualifies for the local exemption.
        local = urlsplit(getattr(provider, "base_url", "")).hostname in {
            "localhost",
            "127.0.0.1",
            "::1",
        }
    if isinstance(provider, PrivacyProvider) or local:
        return provider
    return PrivacyProvider(provider)
