"""Inbox handler — poll the mailbox, route what arrives.

Idempotent by two mechanisms: a durable IMAP UID checkpoint advances in the
same transaction as routed messages, and `route_message` skips a Message-ID
already recorded against the candidate. A replay therefore loses nothing and
records nothing twice.
"""

from __future__ import annotations

import structlog
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models import Candidate, MailboxCheckpoint
from packages.core.queue import ClaimedTask
from packages.inbox.imap import DEFAULT_BATCH, build_mail_source
from packages.inbox.route import route_message
from packages.llm.provider import LLMError, build_provider

log = structlog.get_logger(__name__)

INBOX_TASK_KIND = "inbox"


async def handle_inbox(session: AsyncSession, claimed: ClaimedTask) -> None:
    """Fetch and route one batch after the mailbox's durable checkpoint."""
    payload = claimed.task.payload_json or {}
    limit = int(payload.get("limit", DEFAULT_BATCH))

    source = build_mail_source()
    if source is None:
        log.info("inbox_not_configured")
        return

    # Create then lock one row before touching the mailbox. Concurrent inbox
    # tasks for the same configured source serialize here, so neither can move
    # the high-water mark behind the other.
    await session.execute(
        pg_insert(MailboxCheckpoint)
        .values(
            source_key=source.source_key,
            uid_validity=0,
            last_uid=0,
            initialized=False,
        )
        .on_conflict_do_nothing(index_elements=["source_key"])
    )
    checkpoint = await session.scalar(
        select(MailboxCheckpoint)
        .where(MailboxCheckpoint.source_key == source.source_key)
        .with_for_update()
    )
    if checkpoint is None:  # pragma: no cover - the insert/select are one transaction
        raise RuntimeError("mailbox checkpoint could not be created")

    batch = await source.fetch_after(
        last_uid=checkpoint.last_uid,
        initialized=checkpoint.initialized,
        uid_validity=checkpoint.uid_validity,
        limit=limit,
    )
    if not batch.messages and not batch.drained:
        raise RuntimeError("mailbox fetch stopped before the next message")

    # Inference needs to know whose applications to search. This is a
    # single-owner tool (§1), so the sole candidate is the answer; with none
    # or several, alias routing still works and inference simply sits out
    # rather than guessing across people.
    candidates = list((await session.scalars(select(Candidate))).all())
    candidate_id = candidates[0].id if len(candidates) == 1 else None

    # The model tier of packages/inbox/classify.py. Ollama by name and never a
    # cloud provider: recruiter mail is the owner's correspondence, and §2.8
    # permits one third-party upload that this is not. If it cannot be built
    # the chain simply stops one tier short — an inbox poll must not fail
    # because a local model is not running.
    provider: object | None = None
    try:
        provider = build_provider("ollama")
    except LLMError:
        log.info("inbox_classifier_model_unavailable")

    routed = 0
    inferred = 0
    for fetched in batch.messages:
        result = await route_message(
            session,
            fetched.message,
            candidate_id=candidate_id,
            provider=provider,
        )
        if result.routed:
            routed += 1
        if result.inferred:
            inferred += 1

    validity_changed = checkpoint.uid_validity != batch.uid_validity
    checkpoint.uid_validity = batch.uid_validity
    if validity_changed:
        checkpoint.last_uid = 0
        checkpoint.initialized = False
    if batch.messages:
        checkpoint.last_uid = batch.messages[-1].uid
    if batch.drained:
        # high_water was captured before the search, so messages arriving
        # during this poll receive larger UIDs and are fetched next time.
        checkpoint.last_uid = max(checkpoint.last_uid, batch.high_water)
        checkpoint.initialized = True

    # This is the durability boundary: routed rows and the exact UID position
    # become visible together. A crash before it replays; a crash after it
    # resumes strictly above the committed high-water mark.
    await session.commit()

    log.info(
        "inbox_batch_processed",
        fetched=len(batch.messages),
        routed=routed,
        inferred=inferred,
        last_uid=checkpoint.last_uid,
        initialized=checkpoint.initialized,
    )
