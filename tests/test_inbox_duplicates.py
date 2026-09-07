"""Two different messages are not one message.

`route_message`'s docstring has always promised idempotency on `message_id`.
There was no such column, and the de-duplication keyed on
(from_addr, application_id, subject) — which is not an identity but a
heuristic, and it collides on precisely the mail that legitimately repeats.

The sharp case is an OTP resend, because the duplicate check returns *before*
the block that hands a code to the state machine. Measured before the fix:

    first  111111 -> status='running'   changed=True   otp='111111'
    resend 222222 -> status='needs_otp' changed=False  otp='111111'

The owner clicks "resend code" — which is what you do when the first one
expired — and the application sits in `needs_otp` holding the dead code.

Fixing it needed `imap.py` fixed too: the stand-in for a message with no
`Message-ID` was `f"no-id-{id(raw)}"`, a memory address. Unstable between
runs, so a re-delivered message would be recorded twice; reusable after
collection, so a later message could inherit a freed address and be dropped.
Harmless only while nothing keyed on it.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from packages.core.enums import ApplicationStatus
from packages.core.models import Application, Candidate, InboundMessage, Profile, User
from packages.inbox.alias import alias_for
from packages.inbox.imap import _synthetic_id
from packages.inbox.route import InboundEmail, route_message
from tests.conftest import TEST_DATABASE_URL

BASE = "owner@gmail.com"


async def _build(session, suffix: str, status: str):
    """The rows one application needs, in whatever session is given."""
    user = User(email=f"u-{suffix}@example.com")
    session.add(user)
    await session.flush()
    candidate = Candidate(user_id=user.id, name="Jane", email=f"j-{suffix}@example.com")
    session.add(candidate)
    await session.flush()
    profile = Profile(candidate_id=candidate.id, label="default")
    session.add(profile)
    await session.flush()
    application = Application(
        candidate_id=candidate.id,
        profile_id=profile.id,
        url=f"https://x.test/{suffix}",
        ats="greenhouse",
        status=status,
    )
    session.add(application)
    await session.flush()
    return application


async def _parked(db_session, status: str = ApplicationStatus.NEEDS_OTP.value):
    """One application waiting in `status`, with the rows it needs behind it."""
    return await _build(db_session, uuid.uuid4().hex[:8], status)


def _mail(application, *, body: str, subject: str, message_id: str | None = None):
    """A reply carrying this application's alias. A fresh id unless one is given."""
    return InboundEmail(
        message_id=message_id or f"<{uuid.uuid4()}@mail>",
        from_addr="no-reply@greenhouse.io",
        to_addr=alias_for(BASE, application.id),
        subject=subject,
        body=body,
        received_at=datetime.now(UTC),
    )


async def test_a_resent_code_reaches_the_application(db_session) -> None:
    """The defect, end to end: same sender, same subject, a new code."""
    application = await _parked(db_session)

    first = _mail(application, subject="Your verification code", body="Your code is 111111.")
    await route_message(db_session, first)
    assert application.review_json["otp"] == "111111"

    # The first code failed or expired, so the application parks again and the
    # owner asks for another. Real resends reuse the subject exactly.
    application.status = ApplicationStatus.NEEDS_OTP.value
    await db_session.flush()

    second = _mail(application, subject="Your verification code", body="Your code is 222222.")
    result = await route_message(db_session, second)

    assert result.unrouted_reason != "already recorded", "the resend was dropped"
    assert result.status_changed, "the resend never reached the state machine"
    assert application.review_json["otp"] == "222222", "the application kept the dead code"
    assert application.status == ApplicationStatus.RUNNING.value


async def test_a_second_message_on_one_thread_is_not_a_duplicate(db_session) -> None:
    """The same collision without an OTP: a follow-up keeps the subject."""
    application = await _parked(db_session, ApplicationStatus.QUEUED.value)

    await route_message(db_session, _mail(application, subject="Following up", body="Hello."))
    await route_message(
        db_session, _mail(application, subject="Following up", body="Any news on this?")
    )

    stored = (await db_session.scalars(select(InboundMessage))).all()
    assert len(stored) == 2, "a second, different message was swallowed as a duplicate"


async def test_the_same_message_delivered_twice_is_recorded_once(db_session) -> None:
    """What §10 actually asks for. IMAP re-delivers; the id is unchanged."""
    application = await _parked(db_session, ApplicationStatus.QUEUED.value)
    same_id = f"<{uuid.uuid4()}@mail>"

    await route_message(
        db_session, _mail(application, subject="Interview", body="Hi.", message_id=same_id)
    )
    again = await route_message(
        db_session, _mail(application, subject="Interview", body="Hi.", message_id=same_id)
    )

    assert again.unrouted_reason == "already recorded"
    stored = (await db_session.scalars(select(InboundMessage))).all()
    assert len(stored) == 1


async def test_the_recorded_message_carries_its_id(db_session) -> None:
    """A column nothing writes cannot de-duplicate anything."""
    application = await _parked(db_session, ApplicationStatus.QUEUED.value)
    mail = _mail(application, subject="Interview", body="Hi.")

    await route_message(db_session, mail)

    stored = (await db_session.scalars(select(InboundMessage))).one()
    assert stored.message_id == mail.message_id


async def test_two_workers_racing_one_message_record_it_once() -> None:
    """The check and the insert used to be two statements, and two chances.

    `run_pool` runs several workers, and `search(None, "UNSEEN")` hands the
    same ids to every `handle_inbox` that asks before one of them FETCHes and
    marks them seen. Both would pass a SELECT, both would insert, and both
    would apply the outcome or the OTP below it.

    The interleaving is explicit, because that is the one that used to break:
    the second routing starts while the first is still *uncommitted*. A SELECT
    cannot see the other transaction's row, so the old code found nothing and
    inserted a second time. The unique index makes the second insert wait, and
    once the first commits it becomes a no-op.

    Two things this had to get right to be a test at all. It does not use the
    `db_session` fixture — that runs inside a transaction which is rolled back,
    so nothing it writes reaches a second connection and both routings would
    simply find no application. And it commits the first session *while* the
    second is blocked; committing both at the end deadlocks, which is how the
    first draft hung rather than failed.
    """
    engine = create_async_engine(TEST_DATABASE_URL)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    suffix = uuid.uuid4().hex[:8]
    try:
        async with maker() as setup:
            application = await _build(setup, suffix, ApplicationStatus.QUEUED.value)
            await setup.commit()
            application_id, candidate_id = application.id, application.candidate_id

        mail = InboundEmail(
            message_id=f"<{uuid.uuid4()}@mail>",
            from_addr="no-reply@greenhouse.io",
            to_addr=alias_for(BASE, application_id),
            subject="Interview",
            body="Hi.",
            received_at=datetime.now(UTC),
        )

        async with maker() as one, maker() as two:
            first = await route_message(one, mail)  # inserted, not committed
            second = asyncio.create_task(route_message(two, mail))
            await asyncio.sleep(0.2)  # let it reach the insert
            await one.commit()  # now the conflict resolves
            result = await asyncio.wait_for(second, timeout=30)
            await two.commit()

        assert first.unrouted_reason != "already recorded"
        assert result.unrouted_reason == "already recorded"

        async with maker() as reader:
            stored = (
                await reader.scalars(
                    select(InboundMessage).where(InboundMessage.candidate_id == candidate_id)
                )
            ).all()
        assert len(stored) == 1, f"{len(stored)} rows for one message"
    finally:
        async with maker() as cleanup:
            await cleanup.execute(delete(User).where(User.email == f"u-{suffix}@example.com"))
            await cleanup.commit()
        await engine.dispose()


# --------------------------------------------------------------------------
# The stand-in for a message with no Message-ID header
# --------------------------------------------------------------------------


def test_a_message_with_no_id_hashes_the_same_every_time() -> None:
    """`id(raw)` was a memory address; a re-delivery got a fresh one.

    The two objects have to be genuinely distinct for this to test anything.
    `bytes(raw)` on a bytes input returns *the same object* in CPython, so a
    first draft of this passed against the old implementation as well —
    round-tripping through a bytearray is what forces a separate allocation.
    """
    text = b"From: a@b.test\nSubject: no id here\n\nbody"
    delivered_again = bytes(bytearray(text))
    assert delivered_again is not text, "the two deliveries are the same object"
    assert _synthetic_id(text) == _synthetic_id(delivered_again)


def test_two_different_messages_never_share_a_synthetic_id() -> None:
    """A freed address can be reused; a digest cannot be.

    This one cannot fail against the old implementation — two simultaneously
    live objects always have different addresses — so it documents the
    property rather than guarding it. The guard is the test above.
    """
    assert _synthetic_id(b"one message") != _synthetic_id(b"another message")


@pytest.mark.parametrize("raw", [b"", b"From: a@b.test\n\n"])
def test_every_message_gets_an_id(raw: bytes) -> None:
    """Including the degenerate ones, so the id path is never skipped."""
    assert _synthetic_id(raw).startswith("sha256-")
