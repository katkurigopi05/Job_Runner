"""Inbox handler — poll the mailbox, route what arrives.

Idempotent by two mechanisms: IMAP hands over unread mail only, and
`route_message` skips a message already recorded against its application. A
replayed task therefore records nothing twice, which matters because a
rejection counted twice would look like two rejections.
"""

from __future__ import annotations

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.queue import ClaimedTask
from packages.inbox.imap import DEFAULT_BATCH, build_mail_source
from packages.inbox.route import route_message

log = structlog.get_logger(__name__)

INBOX_TASK_KIND = "inbox"


async def handle_inbox(session: AsyncSession, claimed: ClaimedTask) -> None:
    """Fetch and route one batch of unread mail."""
    payload = claimed.task.payload_json or {}
    limit = int(payload.get("limit", DEFAULT_BATCH))

    source = build_mail_source()
    if source is None:
        log.info("inbox_not_configured")
        return

    messages = await source.fetch_unread(limit)
    if not messages:
        return

    routed = 0
    for message in messages:
        result = await route_message(session, message)
        if result.routed:
            routed += 1

    log.info("inbox_batch_processed", fetched=len(messages), routed=routed)
