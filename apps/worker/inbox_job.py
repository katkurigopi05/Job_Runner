"""Inbox handler — poll the mailbox, route what arrives.

Idempotent by two mechanisms: a durable IMAP UID checkpoint advances in the
same transaction as routed messages, and `route_message` skips a Message-ID
already recorded against the candidate. A replay therefore loses nothing and
records nothing twice.
"""

from __future__ import annotations

import uuid

import structlog
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.config import get_settings
from packages.core.enums import QueueTaskStatus
from packages.core.models import Candidate, MailboxCheckpoint, QueueTask
from packages.core.queue import ClaimedTask, db_now_plus, enqueue
from packages.inbox.imap import DEFAULT_BATCH, build_mail_source
from packages.inbox.route import route_message
from packages.llm.provider import LLMError, build_provider

log = structlog.get_logger(__name__)

INBOX_TASK_KIND = "inbox"

_UNFINISHED = (QueueTaskStatus.PENDING.value, QueueTaskStatus.RUNNING.value)

#: Distinct from the crawler and test-suite advisory locks. It serializes the
#: count-then-enqueue invariant across processes and is released at transaction
#: end, including when a worker loses its database connection.
_QUEUE_GUARD_LOCK_KEY = 4_917_206


async def ensure_inbox_poll(
    session: AsyncSession,
    *,
    exclude_task_id: uuid.UUID | None = None,
    delay_seconds: int = 0,
    source_configured: bool | None = None,
) -> QueueTask | None:
    """Ensure one durable mailbox poll exists, returning it when newly queued.

    The worker supervisor calls this at startup and periodically to repair a
    schedule whose task exhausted its retries. A successful handler calls it
    before committing the mailbox checkpoint, which makes the next wakeup
    durable at the same boundary as the mail it just routed.

    Does not commit. The caller owns the transaction.
    """
    if source_configured is None:
        source_configured = build_mail_source() is not None
    if not source_configured:
        return None

    await session.execute(select(func.pg_advisory_xact_lock(_QUEUE_GUARD_LOCK_KEY)))
    # A handler ignores other running polls: they are current work, not the
    # future successor it must leave behind. The supervisor counts both states
    # because it must not seed a second current poll.
    guarded_statuses = (
        (QueueTaskStatus.PENDING.value,) if exclude_task_id is not None else _UNFINISHED
    )
    conditions = [
        QueueTask.kind == INBOX_TASK_KIND,
        QueueTask.status.in_(guarded_statuses),
    ]
    if exclude_task_id is not None:
        conditions.append(QueueTask.id != exclude_task_id)
    waiting = await session.scalar(select(func.count()).select_from(QueueTask).where(*conditions))
    if waiting:
        return None

    task = await enqueue(session, INBOX_TASK_KIND, {})
    if delay_seconds:
        # Claims compare against the database clock. Write this deadline from
        # that same clock so host drift cannot make a poll early or late.
        await session.execute(
            update(QueueTask)
            .where(QueueTask.id == task.id)
            .values(run_after=db_now_plus(delay_seconds))
        )
    return task


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

    await ensure_inbox_poll(
        session,
        exclude_task_id=claimed.task.id,
        delay_seconds=get_settings().inbox_poll_seconds,
        source_configured=True,
    )

    # This is the durability boundary: routed rows and the exact UID position
    # and the next poll become visible together. A crash before it replays; a
    # crash after it resumes strictly above the committed high-water mark and
    # the queue still knows when to poll again.
    await session.commit()

    log.info(
        "inbox_batch_processed",
        fetched=len(batch.messages),
        routed=routed,
        inferred=inferred,
        last_uid=checkpoint.last_uid,
        initialized=checkpoint.initialized,
    )
