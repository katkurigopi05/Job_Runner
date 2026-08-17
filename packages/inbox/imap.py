"""Fetch mail over IMAP.

The connection is injectable so the ingest path is testable without a mailbox
— the same reason the browser and the GitHub client are. `imaplib` is
synchronous, so calls run in a thread rather than blocking the event loop.

Messages are read, never deleted. The mailbox is the owner's, and an agent
that prunes someone's inbox on their behalf is doing more than it was asked
to. Fetched messages are marked seen; that is the whole footprint.
"""

from __future__ import annotations

import asyncio
import contextlib
import email
import email.policy
import email.utils
import imaplib
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any, Protocol

import structlog

from packages.inbox.route import InboundEmail

log = structlog.get_logger(__name__)

#: Cap one poll, so a long-neglected mailbox cannot stall a worker for hours.
DEFAULT_BATCH = 50


class MailSource(Protocol):
    """Anything that can hand over unread messages."""

    async def fetch_unread(self, limit: int = DEFAULT_BATCH) -> list[InboundEmail]: ...


def _decode(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def parse_message(raw: bytes) -> InboundEmail:
    """Turn RFC-822 bytes into the shape the router wants."""
    parsed = email.message_from_bytes(raw, policy=email.policy.default)

    body = ""
    if parsed.is_multipart():
        # Prefer text/plain; HTML-only mail falls back to the HTML part with
        # tags stripped rather than being dropped.
        for part in parsed.walk():
            if part.get_content_type() == "text/plain":
                body = _decode(part.get_payload(decode=True))
                break
        else:
            for part in parsed.walk():
                if part.get_content_type() == "text/html":
                    from packages.crawler.extract import strip_html

                    body = strip_html(_decode(part.get_payload(decode=True))) or ""
                    break
    else:
        body = _decode(parsed.get_payload(decode=True))

    received_at = None
    date_header = parsed.get("Date")
    if date_header:
        try:
            received_at = email.utils.parsedate_to_datetime(str(date_header))
        except (TypeError, ValueError):
            received_at = None

    return InboundEmail(
        message_id=_decode(parsed.get("Message-ID")) or f"no-id-{id(raw)}",
        from_addr=_decode(parsed.get("From")),
        to_addr=_decode(parsed.get("To")),
        cc_addr=_decode(parsed.get("Cc")),
        delivered_to=_decode(parsed.get("Delivered-To")),
        subject=_decode(parsed.get("Subject")),
        body=body,
        received_at=received_at or datetime.now(UTC),
    )


class ImapMailSource:
    """Reads unread mail from one IMAP mailbox."""

    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        *,
        port: int = 993,
        mailbox: str = "INBOX",
        connector: Any = None,
    ) -> None:
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.mailbox = mailbox
        #: Injected in tests. Defaults to a real TLS connection.
        self._connector = connector or (lambda: imaplib.IMAP4_SSL(host, port))

    def _fetch_sync(self, limit: int) -> list[InboundEmail]:
        connection = self._connector()
        try:
            connection.login(self.username, self.password)
            connection.select(self.mailbox)

            status, data = connection.search(None, "UNSEEN")
            if status != "OK":
                log.warning("imap_search_failed", status=status)
                return []

            identifiers: Iterable[bytes] = (data[0] or b"").split()
            messages: list[InboundEmail] = []

            for identifier in list(identifiers)[:limit]:
                # imaplib returns ids as bytes but wants str back.
                status, payload = connection.fetch(identifier.decode(), "(RFC822)")
                if status != "OK" or not payload:
                    continue
                for part in payload:
                    if isinstance(part, tuple) and len(part) > 1:
                        messages.append(parse_message(part[1]))
                        break

            return messages
        finally:
            # A failed logout is not interesting; the messages are already read.
            with contextlib.suppress(Exception):
                connection.logout()

    async def fetch_unread(self, limit: int = DEFAULT_BATCH) -> list[InboundEmail]:
        """Unread messages, oldest first. imaplib is sync, so this threads."""
        return await asyncio.to_thread(self._fetch_sync, limit)


class StaticMailSource:
    """A fixed list of messages. What tests use in place of a mailbox."""

    def __init__(self, messages: list[InboundEmail]) -> None:
        self.messages = messages

    async def fetch_unread(self, limit: int = DEFAULT_BATCH) -> list[InboundEmail]:
        batch, self.messages = self.messages[:limit], self.messages[limit:]
        return batch


def build_mail_source() -> MailSource | None:
    """Construct the configured source, or None when mail is not set up."""
    from packages.core.config import get_settings

    settings = get_settings()
    if not (settings.imap_host and settings.imap_username and settings.imap_password):
        return None

    return ImapMailSource(
        host=settings.imap_host,
        username=settings.imap_username,
        password=settings.imap_password,
        port=settings.imap_port,
    )
