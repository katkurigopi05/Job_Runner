"""Routing mail that carries no alias of ours.

The alias path only ever sees replies to applications *we* sent. Everything
the owner finished by hand — a §2.5 manual completion, an unresolved
aggregator lead, anything applied for outside Jobrunner — replies to their
plain address, and before this the tracker never learned those happened.

The tests that matter most here are the ones asserting what an inferred link
refuses to do.
"""

from __future__ import annotations

import uuid

from packages.core.enums import ApplicationStatus
from packages.core.models import Application, Company, Posting
from packages.inbox.match import infer, is_generic, sender_domain
from packages.inbox.route import InboundEmail, route_message


async def _posting_for(db_session, *, company_name: str, domain: str, title: str) -> Posting:
    company = Company(name=company_name, domain=domain)
    db_session.add(company)
    await db_session.flush()

    posting = Posting(
        company_id=company.id,
        ats_type="greenhouse",
        external_id=uuid.uuid4().hex[:8],
        url=f"https://boards.greenhouse.io/{company_name.lower()}/jobs/1",
        title=title,
        description_raw="Build things.",
        content_hash=uuid.uuid4().hex,
    )
    db_session.add(posting)
    await db_session.flush()
    return posting


# --------------------------------------------------------------------------
# Signals
# --------------------------------------------------------------------------


def test_sender_domain_is_read_from_a_display_name_address() -> None:
    assert sender_domain("Jane Recruiter <jane@acme.com>") == "acme.com"


def test_consumer_mail_hosts_identify_no_employer() -> None:
    assert is_generic("gmail.com")
    assert is_generic("outlook.com")
    assert not is_generic("acme.com")


def test_ats_notification_hosts_are_treated_as_generic() -> None:
    """Mail sent via Greenhouse says nothing about *which* company."""
    assert is_generic("us.greenhouse-mail.io")
    assert is_generic("mail.greenhouse.io")
    assert not is_generic("greenhouse-partners.com")


# --------------------------------------------------------------------------
# Inference
# --------------------------------------------------------------------------


async def test_a_reply_from_the_company_domain_is_matched(db_session, application) -> None:
    posting = await _posting_for(
        db_session, company_name="Acme", domain="acme.com", title="Senior Backend Engineer"
    )
    application.posting_id = posting.id
    await db_session.flush()

    link = await infer(
        db_session,
        candidate_id=application.candidate_id,
        from_addr="jane@acme.com",
        subject="Your application",
        body="Thanks for applying.",
    )

    assert link is not None
    assert link.application_id == str(application.id)


async def test_a_gmail_reply_alone_is_not_enough(db_session, application) -> None:
    """A recruiter writing from Gmail matches every application equally."""
    posting = await _posting_for(
        db_session, company_name="Acme", domain="acme.com", title="Senior Backend Engineer"
    )
    application.posting_id = posting.id
    await db_session.flush()

    assert (
        await infer(
            db_session,
            candidate_id=application.candidate_id,
            from_addr="recruiter@gmail.com",
            subject="Following up",
            body="Are you free Thursday?",
        )
        is None
    )


async def test_two_applications_to_one_company_are_left_ambiguous(db_session, application) -> None:
    """The normal case, not the exception. Choosing on a hair of difference
    is how a reply lands on the wrong application."""
    posting = await _posting_for(
        db_session, company_name="Acme", domain="acme.com", title="Backend Engineer"
    )
    other_posting = await _posting_for(
        db_session, company_name="Acme", domain="acme.com", title="Platform Engineer"
    )
    application.posting_id = posting.id
    second = Application(
        candidate_id=application.candidate_id,
        profile_id=application.profile_id,
        posting_id=other_posting.id,
        url="https://boards.greenhouse.io/acme/jobs/2",
        ats="greenhouse",
        status="queued",
    )
    db_session.add(second)
    await db_session.flush()

    assert (
        await infer(
            db_session,
            candidate_id=application.candidate_id,
            from_addr="jane@acme.com",
            subject="Your application at Acme",
            body="We reviewed it.",
        )
        is None
    )


async def test_the_posting_title_breaks_the_tie(db_session, application) -> None:
    """Ambiguity is resolvable when the message says which role."""
    posting = await _posting_for(
        db_session, company_name="Acme", domain="acme.com", title="Staff Platform Engineer"
    )
    other = await _posting_for(
        db_session, company_name="Acme", domain="acme.com", title="Backend Engineer"
    )
    application.posting_id = posting.id
    db_session.add(
        Application(
            candidate_id=application.candidate_id,
            profile_id=application.profile_id,
            posting_id=other.id,
            url="https://boards.greenhouse.io/acme/jobs/2",
            ats="greenhouse",
            status="queued",
        )
    )
    await db_session.flush()

    link = await infer(
        db_session,
        candidate_id=application.candidate_id,
        from_addr="jane@acme.com",
        subject="Staff Platform Engineer — next steps",
        body="We would like to speak.",
    )

    assert link is not None
    assert link.application_id == str(application.id)


# --------------------------------------------------------------------------
# What an inferred link refuses to do
# --------------------------------------------------------------------------


async def test_an_inferred_link_attaches_the_message(db_session, application) -> None:
    posting = await _posting_for(
        db_session, company_name="Acme", domain="acme.com", title="Senior Backend Engineer"
    )
    application.posting_id = posting.id
    await db_session.flush()

    result = await route_message(
        db_session,
        InboundEmail(
            message_id="<1@acme.com>",
            from_addr="jane@acme.com",
            to_addr="owner@gmail.com",
            subject="Your application",
            body="Thank you for applying to Acme.",
        ),
        candidate_id=application.candidate_id,
    )

    assert result.routed
    assert result.inferred
    assert result.link_confidence is not None
    assert result.link_signals


async def test_an_inferred_rejection_does_not_record_an_outcome(db_session, application) -> None:
    """The asymmetry the whole design rests on.

    A reply attached to the wrong application is untidy and the owner can see
    it. A rejection recorded on the wrong one is silent: the application reads
    as dead, the owner stops chasing it, and nothing ever contradicts it.
    """
    posting = await _posting_for(
        db_session, company_name="Acme", domain="acme.com", title="Senior Backend Engineer"
    )
    application.posting_id = posting.id
    await db_session.flush()

    result = await route_message(
        db_session,
        InboundEmail(
            message_id="<2@acme.com>",
            from_addr="jane@acme.com",
            to_addr="owner@gmail.com",
            subject="Your application",
            body="Unfortunately we have decided to move forward with other candidates.",
        ),
        candidate_id=application.candidate_id,
    )

    assert result.inferred
    assert result.outcome_set is None
    await db_session.refresh(application)
    assert application.outcome is None


async def test_an_inferred_otp_does_not_move_status(db_session, application) -> None:
    """An OTP on the wrong application resumes a run that was not waiting."""
    posting = await _posting_for(
        db_session, company_name="Acme", domain="acme.com", title="Senior Backend Engineer"
    )
    application.posting_id = posting.id
    application.status = ApplicationStatus.NEEDS_OTP.value
    await db_session.flush()

    result = await route_message(
        db_session,
        InboundEmail(
            message_id="<3@acme.com>",
            from_addr="jane@acme.com",
            to_addr="owner@gmail.com",
            subject="Your verification code is 123456",
            body="Use code 123456 to continue.",
        ),
        candidate_id=application.candidate_id,
    )

    assert result.inferred
    assert not result.status_changed
    await db_session.refresh(application)
    assert application.status == ApplicationStatus.NEEDS_OTP.value


async def test_the_stored_message_records_how_it_was_linked(db_session, application) -> None:
    """An inferred link must never be readable as an exact one."""
    from sqlalchemy import select

    from packages.core.models import InboundMessage

    posting = await _posting_for(
        db_session, company_name="Acme", domain="acme.com", title="Senior Backend Engineer"
    )
    application.posting_id = posting.id
    await db_session.flush()

    await route_message(
        db_session,
        InboundEmail(
            message_id="<4@acme.com>",
            from_addr="jane@acme.com",
            to_addr="owner@gmail.com",
            subject="Hello",
            body="Thanks for applying to Acme.",
        ),
        candidate_id=application.candidate_id,
    )

    stored = (await db_session.scalars(select(InboundMessage))).all()[-1]
    assert stored.link_method == "inferred"
    assert stored.link_confidence is not None


async def test_an_alias_still_concludes(db_session, application) -> None:
    """Inference must not have weakened the exact path."""
    from packages.inbox.alias import alias_for

    result = await route_message(
        db_session,
        InboundEmail(
            message_id="<5@acme.com>",
            from_addr="jane@acme.com",
            to_addr=alias_for("owner@gmail.com", application.id),
            subject="Your application",
            body="Unfortunately we are not moving forward.",
        ),
        candidate_id=application.candidate_id,
    )

    assert result.link_method == "alias"
    assert result.outcome_set == "rejected"


async def test_without_a_candidate_nothing_is_inferred(db_session, application) -> None:
    """Inference needs to know whose applications to search; with none or
    several owners it sits out rather than guessing across people."""
    result = await route_message(
        db_session,
        InboundEmail(
            message_id="<6@acme.com>",
            from_addr="jane@acme.com",
            to_addr="owner@gmail.com",
            subject="Your application",
            body="Thanks for applying to Acme.",
        ),
    )

    assert not result.routed
    assert result.link_method == "unlinked"
