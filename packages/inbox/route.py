"""Route an inbound message to its application and record what it means.

The one rule worth restating: this **never moves `Application.status`**, with a
single exception. Status is the automation's own state machine and ends at
`submitted` (CLAUDE.md §6); an employer's decision is recorded on `outcome`
instead. Trying to express "rejected by employer" as a status transition would
break the terminality the queue relies on to be safely retryable.

The exception is an OTP. A verification code is not the employer's decision —
it is the thing a parked run was waiting for, so it legitimately drives
`needs_otp -> running`, which is an edge the machine already has.

## Two ways a message finds its application

An **alias** is an exact key — the `+app{id}` tag we applied with, echoed back
in a header. Certain, and the only link allowed to conclude anything.

An **inferred** link (`match.py`) is for mail carrying no tag of ours: replies
to applications the owner finished by hand after a §2.5 manual completion, to
aggregator leads we could not resolve to a form, or to anything applied for
outside Jobrunner. Those replies would otherwise never reach the pipeline
board at all.

An inferred link attaches the message and stops. No outcome, no transition,
not even an OTP. The asymmetry is deliberate: a reply attached to the wrong
application is untidy and the owner sees it, while a *rejection* recorded on
the wrong one is silent and wrong — the application reads as dead, the owner
stops chasing it, and nothing ever contradicts the record.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

import structlog
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.enums import (
    OUTCOME_FOR_CLASSIFICATION,
    ApplicationStatus,
    Classification,
)
from packages.core.models import Application, InboundMessage
from packages.core.state import transition
from packages.inbox.alias import find_alias
from packages.inbox.classify import ClassificationResult, classify_message
from packages.inbox.match import infer

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
    #: "alias" when an exact tag identified the application, "inferred" when
    #: it was matched on sender and content, "unlinked" when neither.
    link_method: str = "unlinked"
    link_confidence: float | None = None
    #: Signals behind an inferred link, for the owner to sanity-check.
    link_signals: list[str] = field(default_factory=list)

    @property
    def inferred(self) -> bool:
        return self.link_method == "inferred"

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
    candidate_id: uuid.UUID | None = None,
    provider: object | None = None,
) -> RoutingResult:
    """Store, classify, and act on one message. Does not commit.

    Idempotent on `message_id`: IMAP re-delivers, and a rejection recorded
    twice must not look like two rejections.

    That is what this said while keying on (from_addr, application_id,
    subject) instead. The triple is not an identity — it collides on exactly
    the mail that legitimately repeats, and an OTP resend is the sharp case:
    same sender, same subject, a *new* code, dropped here before reaching the
    block below that hands the code to the state machine. The application then
    sits in `needs_otp` holding the expired one.
    """
    verdict = result or await classify_message(email.subject, email.body, provider=provider)
    routing = RoutingResult(message_id=email.message_id, classification=verdict.classification)

    alias = find_alias(email.to_addr, email.cc_addr, email.delivered_to)
    application: Application | None = None

    if alias is not None:
        application = await session.get(Application, alias.application_id)
        if application is None:
            routing.unrouted_reason = f"alias names unknown application {alias.application_id}"
            return routing
        routing.link_method = "alias"

    elif candidate_id is not None:
        # No tag of ours, so this is mail for something applied to by hand —
        # a §2.5 manual completion, an unresolved aggregator lead, or an
        # application made outside Jobrunner entirely. Guess, carefully.
        link = await infer(
            session,
            candidate_id=candidate_id,
            from_addr=email.from_addr,
            subject=email.subject,
            body=email.body,
        )
        if link is not None:
            application = await session.get(Application, uuid.UUID(link.application_id))
            if application is not None:
                routing.link_method = "inferred"
                routing.link_confidence = link.confidence
                routing.link_signals = link.signals

    if application is None:
        routing.unrouted_reason = (
            "no application alias in To/Cc/Delivered-To, and no confident match"
            if candidate_id is not None
            else "no application alias in To/Cc/Delivered-To"
        )
        log.info("inbound_unrouted", message_id=email.message_id, reason=routing.unrouted_reason)
        return routing

    routing.application_id = str(application.id)

    row = {
        "candidate_id": application.candidate_id,
        "application_id": application.id,
        "message_id": email.message_id or None,
        "from_addr": email.from_addr,
        "subject": email.subject,
        "body": email.body,
        "classification": verdict.classification.value,
        "link_method": routing.link_method,
        "link_confidence": routing.link_confidence,
        "at": email.received_at or datetime.now(UTC),
    }

    # Claim the message by inserting it, rather than looking first and then
    # inserting. Two statements are two chances: `run_pool` runs several
    # workers, `search(None, "UNSEEN")` hands the same ids to every
    # `handle_inbox` that asks before one of them FETCHes and marks them seen,
    # and both would pass a SELECT and then both apply the outcome or the OTP
    # below. `uq_inbound_messages_candidate_message` is what makes losing the
    # race a no-op instead of a second row.
    #
    # Scoped to the candidate rather than the application: a message is the
    # same message wherever it linked, and the id is what says so.
    if email.message_id:
        claim = (
            pg_insert(InboundMessage)
            .values(**row)
            .on_conflict_do_nothing(
                index_elements=["candidate_id", "message_id"],
                index_where=InboundMessage.message_id.isnot(None),
            )
            .returning(InboundMessage.id)
        )
        if await session.scalar(claim) is None:
            log.debug("inbound_duplicate_skipped", message_id=email.message_id)
            routing.unrouted_reason = "already recorded"
            return routing
    else:
        # No id to claim on, so this path keeps the old triple and with it the
        # old race. It is reachable only from a caller building an InboundEmail
        # by hand — `imap.py` digests the bytes when the header is absent — and
        # there is no key a unique index could be built on.
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
        session.add(InboundMessage(**row))

    # An inferred link attaches the message and stops there — no outcome, and
    # no state transition either. Attaching a reply to the wrong application
    # is untidy and the owner can see it; recording a rejection on the wrong
    # one is silent and wrong, and an OTP on the wrong one resumes a run that
    # was not waiting for it. Only an exact alias may conclude anything.
    if routing.link_method != "alias":
        await session.flush()
        log.info(
            "inbound_attached_without_conclusion",
            application_id=routing.application_id,
            confidence=routing.link_confidence,
            signals=routing.link_signals,
        )
        return routing

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
    "assessment": 3,
    "interview": 4,
    "rejected": 5,
    "offer": 5,
}


def _outcome_rank(outcome: object) -> int:
    return _OUTCOME_RANK.get(str(getattr(outcome, "value", outcome)), 0)


def _outcome_rank_of(existing: str | None) -> int:
    return _OUTCOME_RANK.get(existing or "awaiting", 0)
