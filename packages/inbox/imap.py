"""Fetch mail over IMAP.

The connection is injectable so the ingest path is testable without a mailbox
— the same reason the browser and the GitHub client are. `imaplib` is
synchronous, so calls run in a thread rather than blocking the event loop.

Messages are read with BODY.PEEK and never deleted or marked seen. A durable
UID checkpoint, committed with the routed database rows, owns progress without
changing the owner's mailbox state.
"""

from __future__ import annotations

import asyncio
import contextlib
import email
import email.policy
import email.utils
import hashlib
import imaplib
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

import structlog

from packages.inbox.route import InboundEmail

log = structlog.get_logger(__name__)

#: Cap one poll, so a long-neglected mailbox cannot stall a worker for hours.
DEFAULT_BATCH = 50


class MailSource(Protocol):
    """Anything that can hand over messages after a durable position."""

    source_key: str

    async def fetch_after(
        self,
        *,
        last_uid: int,
        initialized: bool,
        uid_validity: int,
        limit: int = DEFAULT_BATCH,
    ) -> MailBatch: ...


@dataclass(frozen=True)
class FetchedEmail:
    uid: int
    message: InboundEmail


@dataclass(frozen=True)
class MailBatch:
    """One ordered, gap-free slice and the mailbox identity it came from."""

    uid_validity: int
    high_water: int
    messages: tuple[FetchedEmail, ...]
    #: True only when every identifier in the search snapshot was fetched.
    drained: bool


def _synthetic_id(raw: bytes) -> str:
    """A stable stand-in for a message with no `Message-ID` header.

    Almost every message has one — RFC 5322 says SHOULD and every mainstream
    MTA obliges — but "almost" is not "always", and `route_message` now
    de-duplicates on this value.

    It used to be `f"no-id-{id(raw)}"`, a *memory address*: unstable between
    runs, so a re-delivered message got a fresh id every poll and would be
    recorded again; and reusable after collection, so a later, different
    message could inherit a freed address and be dropped as a duplicate. Both
    directions were harmless only while nothing keyed on it.

    A digest of the bytes is the identity the header would have carried:
    the same message hashes the same forever, and two different ones do not.
    """
    return f"sha256-{hashlib.sha256(raw).hexdigest()}"


def _decode(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _header(value: object) -> str:
    """A header's text on one line, which is what a header is.

    RFC 2047 lets a sender encode any byte into one, a line break among them
    (`=?utf-8?q?Interview=0AAPPLICATIONS_BY_STATUS=3A…?=`), and it decodes to
    a subject of several lines. A subject is shown in the tracker and handed
    to the assistant as a line of its context, where a second line reads as
    the app's own. `tests/test_red_team.py`.
    """
    return " ".join(_decode(value).split())


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
        message_id=_decode(parsed.get("Message-ID")) or _synthetic_id(raw),
        from_addr=_header(parsed.get("From")),
        to_addr=_header(parsed.get("To")),
        cc_addr=_header(parsed.get("Cc")),
        delivered_to=_header(parsed.get("Delivered-To")),
        subject=_header(parsed.get("Subject")),
        body=body,
        received_at=received_at or datetime.now(UTC),
    )


class ImapMailSource:
    """Reads one checkpointed batch from an IMAP mailbox."""

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
        self.source_key = hashlib.sha256(
            f"{host}\0{port}\0{username}\0{mailbox}".encode()
        ).hexdigest()
        #: Injected in tests. Defaults to a real TLS connection.
        self._connector = connector or (lambda: imaplib.IMAP4_SSL(host, port))

    @staticmethod
    def _identifiers(data: list[bytes] | tuple[bytes, ...] | None) -> list[int]:
        raw = data[0] if data else b""
        return [int(value) for value in (raw or b"").split()]

    @staticmethod
    def _response_int(connection: Any, name: str) -> int:
        _, data = connection.response(name)
        if not data or not data[0]:
            raise RuntimeError(f"IMAP server did not report {name}")
        return int(data[0])

    def _fetch_sync(
        self,
        *,
        last_uid: int,
        initialized: bool,
        uid_validity: int,
        limit: int,
    ) -> MailBatch:
        connection: Any = self._connector()
        try:
            connection.login(self.username, self.password)
            status, _ = connection.select(self.mailbox, readonly=True)
            if status != "OK":
                raise RuntimeError("IMAP mailbox could not be selected")

            current_validity = self._response_int(connection, "UIDVALIDITY")
            same_mailbox = uid_validity == current_validity
            resume_uid = last_uid if same_mailbox else 0
            bootstrap = not initialized or not same_mailbox

            # SELECT supplies UIDNEXT without scanning every historical UID.
            # Capture it before SEARCH: mail arriving later gets a larger UID
            # and is deliberately left for the next poll.
            high_water = max(0, self._response_int(connection, "UIDNEXT") - 1)

            if high_water <= resume_uid:
                return MailBatch(
                    uid_validity=current_validity,
                    high_water=high_water,
                    messages=(),
                    drained=True,
                )

            criterion = "UNSEEN" if bootstrap else f"UID {resume_uid + 1}:{high_water}"
            status, data = connection.uid("search", None, criterion)
            if status != "OK":
                log.warning("imap_search_failed", status=status)
                raise RuntimeError("IMAP message search failed")

            identifiers: Iterable[int] = (
                uid for uid in self._identifiers(data) if resume_uid < uid <= high_water
            )
            pending = list(identifiers)
            selected = pending[:limit]
            messages: list[FetchedEmail] = []
            drained = len(pending) <= limit

            for uid in selected:
                status, payload = connection.uid("fetch", str(uid), "(BODY.PEEK[])")
                if status != "OK" or not payload:
                    drained = False
                    break
                for part in payload:
                    if isinstance(part, tuple) and len(part) > 1:
                        messages.append(FetchedEmail(uid=uid, message=parse_message(part[1])))
                        break
                else:
                    drained = False
                    break

            return MailBatch(
                uid_validity=current_validity,
                high_water=high_water,
                messages=tuple(messages),
                drained=drained,
            )
        finally:
            # A failed logout is not interesting; the checkpoint owns progress.
            with contextlib.suppress(Exception):
                connection.logout()

    async def fetch_after(
        self,
        *,
        last_uid: int,
        initialized: bool,
        uid_validity: int,
        limit: int = DEFAULT_BATCH,
    ) -> MailBatch:
        """Messages after a checkpoint, oldest first, without changing flags."""
        return await asyncio.to_thread(
            self._fetch_sync,
            last_uid=last_uid,
            initialized=initialized,
            uid_validity=uid_validity,
            limit=limit,
        )


class StaticMailSource:
    """A fixed list of messages. What tests use in place of a mailbox."""

    def __init__(
        self,
        messages: list[InboundEmail],
        *,
        source_key: str = "static",
        uid_validity: int = 1,
    ) -> None:
        self.messages = messages
        self.source_key = source_key
        self.uid_validity = uid_validity
        self._legacy_offset = 0

    async def fetch_after(
        self,
        *,
        last_uid: int,
        initialized: bool,
        uid_validity: int,
        limit: int = DEFAULT_BATCH,
    ) -> MailBatch:
        resume_uid = last_uid if uid_validity == self.uid_validity else 0
        pending = [
            FetchedEmail(uid=index, message=message)
            for index, message in enumerate(self.messages, start=1)
            if index > resume_uid
        ]
        return MailBatch(
            uid_validity=self.uid_validity,
            high_water=len(self.messages),
            messages=tuple(pending[:limit]),
            drained=len(pending) <= limit,
        )

    async def fetch_unread(self, limit: int = DEFAULT_BATCH) -> list[InboundEmail]:
        """Compatibility helper for parser/source unit tests."""
        batch = self.messages[self._legacy_offset : self._legacy_offset + limit]
        self._legacy_offset += len(batch)
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
