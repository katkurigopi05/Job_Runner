"""Route an inbound message to its application and record what it means.

The one rule worth restating: this **never moves `Application.status`**, with a
single exception. Status is the automation's own state machine and ends at
`submitted` (CLAUDE.md §6); an employer's decision is recorded on `outcome`
instead. Trying to express "rejected by employer" as a status transition would
break the terminality the queue relies on to be safely retryable.

The exception is an OTP. A verification code is not the employer's decision —
it is the thing a parked run was waiting for, so it legitimately drives
`needs_otp -> running`, which is an edge the machine already has.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.enums import (
    OUTCOME_FOR_CLASSIFICATION,
    ApplicationStatus,
    Classification,
)
from packages.core.models import Application, InboundMessage
from packages.core.state import transition
from packages.inbox.alias import find_alias
from packages.inbox.classify import ClassificationResult, classify

log = structlog.get_logger(__name__)

_CODE_RE = re.compile(r"\b(\d{4,8})\b")


@dataclass
class InboundEmail:
    """One message, as fetched. Headers kept separate from the body."""

    message_id: str
    from_addr: str
    to_addr: str = ""
    cc_addr: str = ""
    delivered_to: str = ""
    subject: str = ""
    body: str = ""
    received_at: datetime | None = None


@dataclass
class RoutingResult:
    message_id: str
    classification: Classification
    application_id: str | None = None
    outcome_set: str | None = None
    status_changed: bool = False
    #: Why it went nowhere, when it did.
    unrouted_reason: str | None = None

    @property
    def routed(self) -> bool:
        return self.application_id is not None


def extract_code(text: str) -> str | None:
    """The verification code in an OTP message, if there is one."""
    match = _CODE_RE.search(text)
    return match.group(1) if match else None


async def route_message(
    session: AsyncSession,
    email: InboundEmail,
    *,
    result: ClassificationResult | None = None,
) -> RoutingResult:
    """Store, classify, and act on one message. Does not commit.

    Idempotent on `message_id`: IMAP re-delivers, and a rejection recorded
    twice must not look like two rejections.
    """
    verdict = result or classify(email.subject, email.body)
    routing = RoutingResult(message_id=email.message_id, classification=verdict.classification)

    alias = find_alias(email.to_addr, email.cc_addr, email.delivered_to)
    if alias is None:
        routing.unrouted_reason = "no application alias in To/Cc/Delivered-To"
        log.info("inbound_unrouted", message_id=email.message_id, reason=routing.unrouted_reason)
        return routing

    application = await session.get(Application, alias.application_id)
    if application is None:
        routing.unrouted_reason = f"alias names unknown application {alias.application_id}"
        return routing

    routing.application_id = str(application.id)

    already = await session.scalar(
        InboundMessage.__table__.select().where(
            InboundMessage.from_addr == email.from_addr,
            InboundMessage.application_id == application.id,
            InboundMessage.subject == email.subject,
        )
    )
    if already is not None:
        log.debug("inbound_duplicate_skipped", message_id=email.message_id)
        routing.unrouted_reason = "already recorded"
        return routing

    session.add(
        InboundMessage(
            candidate_id=application.candidate_id,
            application_id=application.id,
            from_addr=email.from_addr,
            subject=email.subject,
            body=email.body,
            classification=verdict.classification.value,
            at=email.received_at or datetime.now(UTC),
        )
    )

    # An OTP is the one inbound message that drives the state machine, and
    # only from the state that was waiting for it.
    if verdict.classification is Classification.OTP:
        code = extract_code(f"{email.subject}\n{email.body}")
        if code and application.status == ApplicationStatus.NEEDS_OTP.value:
            application.review_json = {**(application.review_json or {}), "otp": code}
            await transition(
                session,
                application,
                ApplicationStatus.RUNNING,
                payload={"otp_supplied": True, "source": "inbox"},
            )
            routing.status_changed = True
        await session.flush()
        return routing

    # A later acknowledgement must not overwrite a rejection already on record;
    # only ever move to an outcome at least as definitive as the current one.
    outcome = OUTCOME_FOR_CLASSIFICATION.get(verdict.classification)
    if outcome is not None and _outcome_rank(outcome) >= _outcome_rank_of(application.outcome):
        application.outcome = outcome.value
        application.outcome_at = email.received_at or datetime.now(UTC)
        routing.outcome_set = outcome.value

    await session.flush()
    log.info(
        "inbound_routed",
        application_id=routing.application_id,
        classification=verdict.classification.value,
        outcome=routing.outcome_set,
    )
    return routing


#: How definitive each outcome is. A rejection or an offer is the end of the
#: story; an acknowledgement is not, and must never overwrite one.
_OUTCOME_RANK: dict[str, int] = {
    "awaiting": 0,
    "acknowledged": 1,
    "info_requested": 2,
    "interview": 3,
    "rejected": 4,
    "offer": 4,
}


def _outcome_rank(outcome: object) -> int:
    return _OUTCOME_RANK.get(str(getattr(outcome, "value", outcome)), 0)


def _outcome_rank_of(existing: str | None) -> int:
    return _OUTCOME_RANK.get(existing or "awaiting", 0)
