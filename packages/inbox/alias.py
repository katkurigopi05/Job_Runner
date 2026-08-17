"""Per-application email aliases — CLAUDE.md §3, `owner+app{id}@gmail.com`.

Plus-addressing is what makes routing reliable. Matching replies by company
name or subject line is guesswork; an alias the application itself generated
is an exact key, so a reply lands on the right application even when three
applications went to the same company.

The alias is applied-with, so it appears on the employer's side and comes back
in the `To:` header of their reply. That is the whole mechanism.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass

#: `owner+app0f8c...@gmail.com`. The tag is the application UUID as bare hex —
#: unambiguous, and short enough for the 64-character local-part limit.
_ALIAS_RE = re.compile(r"^(?P<user>[^+@]+)\+app(?P<hex>[0-9a-f]{32})@(?P<domain>.+)$", re.I)

TAG_PREFIX = "app"


class AliasError(ValueError):
    """The base address cannot carry a plus-tag."""


@dataclass(frozen=True)
class ParsedAlias:
    application_id: uuid.UUID
    base_address: str


def alias_for(base_address: str, application_id: uuid.UUID | str) -> str:
    """Build the reply-to alias for one application.

    Raises:
        AliasError: the base address already carries a tag, or is not an
            address at all. Silently producing `a+b+app...@x` would create an
            alias that never routes back.
    """
    if "@" not in base_address:
        raise AliasError(f"{base_address!r} is not an email address")

    local, _, domain = base_address.partition("@")
    if "+" in local:
        raise AliasError(f"{base_address!r} already contains a plus-tag; use the bare address")

    identifier = uuid.UUID(str(application_id))
    return f"{local}+{TAG_PREFIX}{identifier.hex}@{domain}"


def parse_alias(address: str) -> ParsedAlias | None:
    """Recover the application from an alias, or None if it is not one."""
    match = _ALIAS_RE.match(address.strip())
    if match is None:
        return None
    try:
        identifier = uuid.UUID(hex=match.group("hex"))
    except ValueError:  # pragma: no cover - the regex already constrains this
        return None
    return ParsedAlias(
        application_id=identifier,
        base_address=f"{match.group('user')}@{match.group('domain')}",
    )


def find_alias(*header_values: str | None) -> ParsedAlias | None:
    """Scan `To`/`Cc`/`Delivered-To` for an alias we issued.

    Replies routinely land with the alias in a header other than `To:` —
    forwards, list expansions, ATS notification systems — so every candidate
    header is searched rather than just one.
    """
    for value in header_values:
        if not value:
            continue
        for candidate in re.split(r"[,;]", value):
            cleaned = candidate.strip()
            # Strip a display name: `Name <addr>`.
            angled = re.search(r"<([^>]+)>", cleaned)
            if angled:
                cleaned = angled.group(1)
            parsed = parse_alias(cleaned)
            if parsed is not None:
                return parsed
    return None
