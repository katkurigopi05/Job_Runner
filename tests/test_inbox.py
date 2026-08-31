"""Inbox tracker — Gate 6.

Gate 6 asks for two things: a message to `owner+app{id}@gmail.com` lands on
the right application and moves its status, and classification is ≥90%
accurate on 30 hand-labeled recruiter emails.

The first is `test_gate6_alias_routes_to_the_right_application`. The second is
`test_gate6_classification_accuracy`, and it is worth being precise about what
it proves: these 30 emails are realistic recruiter mail I composed, not
messages from a real inbox. The accuracy figure is honest against this set;
the actual test is the owner's own mail, where the wording will be stranger.

The status half of Gate 6 needed a design decision, documented in
packages/inbox/route.py: an employer's decision sets `outcome`, not `status`,
because `submitted` is terminal (CLAUDE.md §6) and the queue depends on that.
An OTP is the exception, and it is the case that genuinely moves status.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from packages.core.enums import ApplicationStatus, Classification, Outcome
from packages.core.models import InboundMessage
from packages.core.state import transition
from packages.inbox.alias import AliasError, alias_for, find_alias, parse_alias
from packages.inbox.classify import RuleClassifier, classify
from packages.inbox.imap import StaticMailSource, parse_message
from packages.inbox.route import InboundEmail, extract_code, route_message

BASE = "owner@gmail.com"


# --------------------------------------------------------------------------
# Aliases
# --------------------------------------------------------------------------


def test_alias_round_trips() -> None:
    application_id = uuid.uuid4()
    alias = alias_for(BASE, application_id)

    parsed = parse_alias(alias)

    assert parsed is not None
    assert parsed.application_id == application_id
    assert parsed.base_address == BASE


def test_alias_shape() -> None:
    alias = alias_for(BASE, uuid.UUID(int=1))
    assert alias.startswith("owner+app")
    assert alias.endswith("@gmail.com")
    assert len(alias.split("@")[0]) <= 64  # local-part limit


def test_two_applications_get_distinct_aliases() -> None:
    """The whole point: two applications to one company still route apart."""
    a = alias_for(BASE, uuid.uuid4())
    b = alias_for(BASE, uuid.uuid4())
    assert a != b


def test_tagged_base_address_is_refused() -> None:
    """`a+b+app...@x` would never route back, so refuse rather than build it."""
    with pytest.raises(AliasError, match="already contains a plus-tag"):
        alias_for("owner+existing@gmail.com", uuid.uuid4())


def test_non_address_is_refused() -> None:
    with pytest.raises(AliasError):
        alias_for("not-an-address", uuid.uuid4())


def test_plain_address_is_not_an_alias() -> None:
    assert parse_alias("recruiter@company.com") is None
    assert parse_alias("owner+newsletter@gmail.com") is None


def test_alias_found_in_a_display_name_header() -> None:
    alias = alias_for(BASE, uuid.UUID(int=7))
    found = find_alias(f'"Ada Lovelace" <{alias}>')
    assert found is not None
    assert found.application_id == uuid.UUID(int=7)


def test_alias_found_in_cc_or_delivered_to() -> None:
    """Replies routinely arrive with the alias somewhere other than To:."""
    alias = alias_for(BASE, uuid.UUID(int=9))

    assert find_alias("someone@else.com", alias, "") is not None
    assert find_alias("someone@else.com", "", alias) is not None


def test_alias_found_among_several_recipients() -> None:
    alias = alias_for(BASE, uuid.UUID(int=11))
    found = find_alias(f"a@b.com, {alias}, c@d.com")
    assert found is not None


def test_no_alias_returns_none() -> None:
    assert find_alias("a@b.com", "c@d.com", None) is None


# --------------------------------------------------------------------------
# Gate 6 — 30 hand-labeled emails
# --------------------------------------------------------------------------

#: (expected classification, subject, body). Realistic recruiter mail.
LABELED: list[tuple[Classification, str, str]] = [
    # Rejections (8) — including ones that borrow positive vocabulary.
    (
        Classification.REJECTION,
        "Your application to Acme",
        "Thank you for your interest. Unfortunately we have decided to move forward"
        " with other candidates.",
    ),
    (
        Classification.REJECTION,
        "Update on your application",
        "After careful review we regret to inform you that we will not be proceeding.",
    ),
    (
        Classification.REJECTION,
        "Acme — Senior Engineer",
        "We have decided not to move forward with your application at this time.",
    ),
    (
        Classification.REJECTION,
        "Application status",
        "You are no longer under consideration for this role. We wish you the best.",
    ),
    (
        Classification.REJECTION,
        "Thanks for your time",
        "We will not be moving forward, but we would like to keep your resume on file.",
    ),
    (
        Classification.REJECTION,
        "Re: Backend Engineer",
        "The position has been filled. Thank you for applying.",
    ),
    (
        Classification.REJECTION,
        "Hiring update",
        "We have chosen to pursue other candidates whose experience aligns more closely.",
    ),
    (
        Classification.REJECTION,
        "Your candidacy",
        "Unfortunately you were not selected for the next round.",
    ),
    # Interviews (7)
    (
        Classification.INTERVIEW,
        "Next steps — Acme",
        "We would like to schedule a call with you this week. What is your availability?",
    ),
    (
        Classification.INTERVIEW,
        "Interview invitation",
        "We would like to invite you to interview with the engineering team.",
    ),
    (
        Classification.INTERVIEW,
        "Chat about the Backend role?",
        "I would like to speak with you about your background. Do you have time Thursday?",
    ),
    (
        Classification.INTERVIEW,
        "Phone screen",
        "Let's set up a call. Please book time using the link below.",
    ),
    (
        Classification.INTERVIEW,
        "Technical interview scheduling",
        "Your technical interview will be 90 minutes. Please confirm a slot.",
    ),
    (
        Classification.INTERVIEW,
        "Acme onsite",
        "We would like to move to an onsite interview. Sharing availability for next week.",
    ),
    (
        Classification.INTERVIEW,
        "Following up",
        "Would you be open to a quick chat? Happy to work around your schedule.",
    ),
    # Offers (3)
    (
        Classification.OFFER,
        "Offer of employment — Acme",
        "We are pleased to offer you the position of Senior Backend Engineer.",
    ),
    (
        Classification.OFFER,
        "Your offer letter",
        "Attached is your offer letter. Please review and sign at your convenience.",
    ),
    (
        Classification.OFFER,
        "Good news!",
        "We are extending you an offer. Details on compensation are below.",
    ),
    # Info requests (4)
    (
        Classification.INFO_REQUEST,
        "Additional information needed",
        "Could you please send your work authorization details before we proceed?",
    ),
    (
        Classification.INFO_REQUEST,
        "Background check",
        "Please complete the background check form linked here.",
    ),
    (
        Classification.INFO_REQUEST,
        "Documents",
        "We need you to upload your identification documents to continue.",
    ),
    (
        Classification.INFO_REQUEST,
        "Quick question",
        "Could you confirm your notice period and earliest start date?",
    ),
    # Acknowledgements (4)
    (
        Classification.ACKNOWLEDGEMENT,
        "We received your application",
        "Thank you for applying to Acme. Our team is reviewing applications now.",
    ),
    (
        Classification.ACKNOWLEDGEMENT,
        "Application received",
        "Your application has been received and is in the queue for review.",
    ),
    (
        Classification.ACKNOWLEDGEMENT,
        "Thanks for applying",
        "Thanks for applying to the Backend Engineer role. We will be in touch.",
    ),
    (
        Classification.ACKNOWLEDGEMENT,
        "Acme careers",
        "We are reviewing your application and will follow up shortly.",
    ),
    # OTP (2)
    (
        Classification.OTP,
        "Your verification code",
        "Your code is 483920. It expires in ten minutes.",
    ),
    (
        Classification.OTP,
        "Confirm your email",
        "Please use security code 771204 to confirm your email address.",
    ),
    # Noise (2)
    (
        Classification.NOISE,
        "New jobs matching your search",
        "Here are 12 recommended jobs this week. Unsubscribe from job alerts.",
    ),
    (
        Classification.NOISE,
        "Our engineering newsletter",
        "This month's newsletter covers our migration to Kubernetes. Webinar Thursday.",
    ),
]


def test_gate6_classification_accuracy() -> None:
    """Gate 6: ≥90% accurate on 30 hand-labeled emails."""
    assert len(LABELED) == 30

    wrong: list[str] = []
    for expected, subject, body in LABELED:
        got = classify(subject, body).classification
        if got is not expected:
            wrong.append(f"{subject!r}: expected {expected.value}, got {got.value}")

    accuracy = (len(LABELED) - len(wrong)) / len(LABELED)
    assert accuracy >= 0.90, f"{accuracy:.0%} accurate; misclassified:\n" + "\n".join(wrong)


def test_a_polite_rejection_is_not_read_as_an_interview() -> None:
    """ "move forward" appears in both; order in RULES is what decides."""
    result = classify(
        "Update", "Unfortunately we will not be moving forward with your application."
    )
    assert result.classification is Classification.REJECTION


def test_classifier_reports_its_evidence() -> None:
    """A wrong verdict should point at the rule that produced it."""
    result = classify("Update", "Unfortunately we are not proceeding.")
    assert result.evidence
    assert result.confident


def test_unrecognized_mail_abstains_rather_than_guessing() -> None:
    result = RuleClassifier().classify("Hello", "Just checking in about the thing.")
    assert result.abstained
    assert result.classification is Classification.NOISE


# --------------------------------------------------------------------------
# Gate 6 — routing
# --------------------------------------------------------------------------


def email_for(application_id, subject: str, body: str, **kwargs) -> InboundEmail:
    return InboundEmail(
        message_id=f"<{uuid.uuid4()}@mail>",
        from_addr="recruiter@acme.com",
        to_addr=alias_for(BASE, application_id),
        subject=subject,
        body=body,
        received_at=datetime.now(UTC),
        **kwargs,
    )


async def test_gate6_alias_routes_to_the_right_application(db_session, application) -> None:
    """Gate 6: a message to the alias lands on that exact application."""
    message = email_for(application.id, "Interview invitation", "We would like to schedule a call.")

    result = await route_message(db_session, message)

    assert result.routed
    assert result.application_id == str(application.id)
    assert result.classification is Classification.INTERVIEW

    stored = (await db_session.scalars(InboundMessage.__table__.select())).all()
    assert len(stored) == 1


async def test_routing_records_the_outcome_not_the_status(db_session, application) -> None:
    """An employer's decision is an outcome; `status` stays the machine's."""
    await transition(db_session, application, ApplicationStatus.RUNNING)
    await transition(db_session, application, ApplicationStatus.SUBMITTED)

    result = await route_message(
        db_session,
        email_for(application.id, "Update", "Unfortunately we are not proceeding."),
    )

    assert result.outcome_set == Outcome.REJECTED
    assert application.outcome == Outcome.REJECTED
    assert application.status == ApplicationStatus.SUBMITTED  # untouched
    assert not result.status_changed


async def test_an_otp_does_move_status(db_session, application) -> None:
    """The one inbound message that legitimately drives the state machine."""
    await transition(db_session, application, ApplicationStatus.RUNNING)
    await transition(db_session, application, ApplicationStatus.NEEDS_OTP)

    result = await route_message(
        db_session,
        email_for(application.id, "Your verification code", "Your code is 483920."),
    )

    assert result.status_changed
    assert application.status == ApplicationStatus.RUNNING
    assert (application.review_json or {}).get("otp") == "483920"


async def test_an_otp_for_an_application_not_awaiting_one_is_inert(db_session, application) -> None:
    result = await route_message(
        db_session,
        email_for(application.id, "Your verification code", "Your code is 111111."),
    )

    assert not result.status_changed
    assert application.status == ApplicationStatus.QUEUED


async def test_an_acknowledgement_never_overwrites_a_rejection(db_session, application) -> None:
    """Mail arrives out of order; a rejection is not undone by a later ack."""
    await route_message(
        db_session, email_for(application.id, "Update", "Unfortunately we are not proceeding.")
    )
    await route_message(
        db_session,
        email_for(application.id, "Received", "Thank you for applying to Acme."),
    )

    assert application.outcome == Outcome.REJECTED


async def test_progress_does_overwrite_a_weaker_outcome(db_session, application) -> None:
    await route_message(
        db_session, email_for(application.id, "Received", "Thank you for applying.")
    )
    assert application.outcome == Outcome.ACKNOWLEDGED

    await route_message(
        db_session,
        email_for(application.id, "Next steps", "We would like to schedule a call."),
    )
    assert application.outcome == Outcome.INTERVIEW


async def test_duplicate_delivery_is_recorded_once(db_session, application) -> None:
    """IMAP re-delivers; a rejection counted twice looks like two rejections."""
    message = email_for(application.id, "Update", "Unfortunately we are not proceeding.")

    await route_message(db_session, message)
    second = await route_message(db_session, message)

    assert second.unrouted_reason == "already recorded"
    stored = (await db_session.scalars(InboundMessage.__table__.select())).all()
    assert len(stored) == 1


async def test_mail_without_an_alias_is_left_unrouted(db_session) -> None:
    message = InboundEmail(
        message_id="<x@mail>",
        from_addr="recruiter@acme.com",
        to_addr="owner@gmail.com",
        subject="Hello",
        body="Are you looking for work?",
    )

    result = await route_message(db_session, message)

    assert not result.routed
    assert result.unrouted_reason is not None


async def test_alias_for_an_unknown_application_is_reported(db_session) -> None:
    message = email_for(uuid.uuid4(), "Update", "Unfortunately not proceeding.")
    result = await route_message(db_session, message)

    assert not result.routed
    assert "unknown application" in (result.unrouted_reason or "")


def test_extract_code() -> None:
    assert extract_code("Your code is 483920.") == "483920"
    assert extract_code("no digits here") is None


# --------------------------------------------------------------------------
# IMAP parsing
# --------------------------------------------------------------------------


def test_parse_plain_message() -> None:
    raw = (
        b"From: recruiter@acme.com\r\n"
        b"To: owner+app00000000000000000000000000000001@gmail.com\r\n"
        b"Subject: Interview invitation\r\n"
        b"Message-ID: <abc@acme.com>\r\n"
        b"Date: Mon, 3 Aug 2026 10:00:00 +0000\r\n"
        b"\r\n"
        b"We would like to schedule a call.\r\n"
    )

    parsed = parse_message(raw)

    assert parsed.from_addr == "recruiter@acme.com"
    assert parsed.subject == "Interview invitation"
    assert "schedule a call" in parsed.body
    assert parsed.received_at is not None


def test_parse_multipart_prefers_plain_text() -> None:
    raw = (
        b"From: r@acme.com\r\nTo: owner@gmail.com\r\nSubject: Hi\r\n"
        b'Content-Type: multipart/alternative; boundary="B"\r\n\r\n'
        b"--B\r\nContent-Type: text/plain\r\n\r\nPlain version.\r\n"
        b"--B\r\nContent-Type: text/html\r\n\r\n<p>HTML version.</p>\r\n"
        b"--B--\r\n"
    )

    parsed = parse_message(raw)

    assert "Plain version" in parsed.body
    assert "HTML" not in parsed.body


def test_html_only_message_is_not_dropped() -> None:
    raw = (
        b"From: r@acme.com\r\nTo: owner@gmail.com\r\nSubject: Hi\r\n"
        b'Content-Type: multipart/alternative; boundary="B"\r\n\r\n'
        b"--B\r\nContent-Type: text/html\r\n\r\n<p>Only HTML here.</p>\r\n"
        b"--B--\r\n"
    )

    parsed = parse_message(raw)

    assert "Only HTML here" in parsed.body
    assert "<p>" not in parsed.body


async def test_static_source_drains_in_batches() -> None:
    source = StaticMailSource([email_for(uuid.uuid4(), "s", "b") for _ in range(5)])

    first = await source.fetch_unread(limit=2)
    rest = await source.fetch_unread(limit=10)

    assert len(first) == 2
    assert len(rest) == 3
    assert await source.fetch_unread() == []


# --------------------------------------------------------------------------
# The worker seam
# --------------------------------------------------------------------------


async def test_inbox_handler_routes_a_batch(
    worker_session, committing_sessionmaker, monkeypatch
) -> None:
    """handle_inbox → build_mail_source → route_message, mailbox faked."""
    from apps.worker import inbox_job
    from packages.core.models import Application, Candidate, Profile, User
    from packages.core.queue import ClaimedTask, enqueue

    user = User(email="inbox-owner@example.com")
    worker_session.add(user)
    await worker_session.flush()
    candidate = Candidate(user_id=user.id, name="Owner", email="inbox@example.com")
    worker_session.add(candidate)
    await worker_session.flush()
    profile = Profile(candidate_id=candidate.id, label="p")
    worker_session.add(profile)
    await worker_session.flush()
    app = Application(
        candidate_id=candidate.id,
        profile_id=profile.id,
        url="https://boards.greenhouse.io/acme/jobs/1",
        status="queued",
    )
    worker_session.add(app)
    await worker_session.flush()

    source = StaticMailSource([email_for(app.id, "Update", "Unfortunately we are not proceeding.")])
    monkeypatch.setattr(inbox_job, "build_mail_source", lambda: source)

    task = await enqueue(worker_session, "inbox", {})
    await worker_session.commit()

    await inbox_job.handle_inbox(
        worker_session, ClaimedTask(task=task, reclaimed=False, previous_owner=None)
    )
    await worker_session.commit()

    await worker_session.refresh(app)
    assert app.outcome == Outcome.REJECTED
    assert app.status == "queued"  # the machine is untouched


async def test_inbox_handler_without_configuration_is_a_no_op(worker_session) -> None:
    """No mailbox configured is a normal state, not an error."""
    from apps.worker import inbox_job
    from packages.core.queue import ClaimedTask, enqueue

    task = await enqueue(worker_session, "inbox", {})
    await worker_session.commit()

    await inbox_job.handle_inbox(
        worker_session, ClaimedTask(task=task, reclaimed=False, previous_owner=None)
    )


async def test_the_one_miss_abstains_rather_than_guessing_wrong() -> None:
    """ "Would you be open to a quick chat?" is the rules' one miss on the set.

    It is deliberately not patched. Adding a pattern to catch a case I wrote
    myself would make the accuracy figure circular. What matters is that the
    rules *abstain* instead of confidently answering wrongly — abstention is
    what the LLM tier is for, and a wrong confident answer is what would
    silently mark an interview invitation as noise.
    """
    from packages.inbox.classify import LLMClassifier
    from packages.llm.provider import StubProvider

    subject, body = "Following up", "Would you be open to a quick chat?"

    rules = RuleClassifier().classify(subject, body)
    assert rules.abstained

    escalated = await LLMClassifier(StubProvider({"Following up": "interview"})).classify(
        subject, body
    )
    assert escalated.classification is Classification.INTERVIEW
    assert not escalated.confident  # a model's guess is marked as one


async def test_llm_tier_is_not_consulted_when_rules_are_certain() -> None:
    """Spend the model on the ambiguous remainder, not the boilerplate."""
    from packages.inbox.classify import LLMClassifier
    from packages.llm.provider import StubProvider

    provider = StubProvider({"Unfortunately": "offer"})  # would be wrong
    result = await LLMClassifier(provider).classify(
        "Update", "Unfortunately we are not proceeding."
    )

    assert result.classification is Classification.REJECTION
    assert provider.calls == []


async def test_llm_failure_falls_back_to_the_rule_verdict() -> None:
    from packages.inbox.classify import LLMClassifier

    class _Broken:
        async def complete(self, system, user, *, max_tokens=1024):
            raise RuntimeError("model down")

    result = await LLMClassifier(_Broken()).classify("Hello", "Just checking in.")
    assert result.classification is Classification.NOISE


#: A rejection as one actually arrives: a req number in the subject, the refusal
#: buried mid-paragraph in warm language, a signature block, the applicant's own
#: optimistic reply quoted underneath, and an ATS footer. Every fixture above is
#: two clean sentences, which is why the rules score 29/30 on them and abstain
#: on this. CLAUDE.md §15 asks for real correspondence; this is not that either,
#: but it is shaped like it, and it is the case the tiers exist for.
REALISTIC_REJECTION = (
    "Re: Your application for Senior Backend Engineer (R-4471) at Acme, Inc.",
    """Hi Gopi,

Thanks so much for taking the time to speak with our team last week. We were
genuinely impressed by your background in distributed systems.

After a lot of discussion, we've decided to move forward with another candidate
whose experience lines up more directly with what this team needs right now.

We'd love to stay in touch. Please do apply again down the road.

Dana Whitfield, Technical Recruiter | Acme, Inc.

> On Tue, Aug 12, 2026 Gopi wrote:
> Hi Dana, I'm very excited about the platform team and wanted to follow up
> on next steps and the interview panel.
--
Sent via Greenhouse. Unsubscribe: https://my.greenhouse.io/notifications/x
""",
)


def test_the_rules_now_catch_a_realistically_worded_rejection() -> None:
    """This used to assert the opposite, and the reason is worth keeping.

    The fixtures say "with other candidates" and the pattern matched.
    Recruiters write "with another candidate", and it did not — one word, and
    the rules abstained on the commonest rejection there is. Measured across
    fourteen phrasings recruiters actually use, the old pattern caught two.

    That mattered most where there is no model to fall through to. Ollama is
    not always running, and the chain's lower tiers are what an inbox poll
    actually has: Bayes reads this message as `interview` at a margin of
    0.073, far below the threshold to adopt, so without the rules it resolved
    to `noise` and the application sat in the tracker looking live.
    """
    from packages.inbox.classify import RuleClassifier

    verdict = RuleClassifier().classify(*REALISTIC_REJECTION)

    assert not verdict.abstained
    assert verdict.classification is Classification.REJECTION


@pytest.mark.parametrize(
    "body",
    [
        "We've decided to move forward with another candidate.",
        "We have chosen to pursue other applicants for this role.",
        "We've decided to go in a different direction.",
        "After careful consideration, we have decided not to proceed with your application.",
        "We won't be progressing your application at this time.",
        "We've selected another candidate whose experience aligns more closely.",
        "We are unable to move forward with your candidacy.",
        "Your application was not successful on this occasion.",
        "We will not be advancing your application.",
        "The role has been filled.",
        "We decided to proceed with a different candidate.",
        "We're moving forward with candidates whose background is a closer match.",
    ],
)
def test_the_rules_read_the_phrasings_recruiters_actually_use(body: str) -> None:
    """Twelve rejections, none phrased the way the fixture corpus phrases them.

    Gate 6's fixtures were written beside the patterns that read them, so they
    agree with each other and prove less than the gate asks. These were
    written against the pattern instead.
    """
    from packages.inbox.classify import RuleClassifier

    verdict = RuleClassifier().classify("Re: your application", body)

    assert verdict.classification is Classification.REJECTION, body


@pytest.mark.parametrize(
    "body",
    [
        "We would like to invite you to a technical interview next week.",
        "We are pleased to offer you the position of Senior Backend Engineer.",
        "Please schedule a time with the hiring manager to move forward with your application.",
        "We are moving forward with your candidacy and would like to set up a call.",
        "Thanks for applying! We are reviewing applications and will be in touch.",
    ],
)
def test_widening_the_rejection_rule_did_not_swallow_the_good_news(body: str) -> None:
    """Rejection is matched first, so a loose pattern here mis-files everything.

    "Moving forward with your application" is an interview and has to stay
    one; only "forward with another/other/a different" reads as a refusal.
    """
    from packages.inbox.classify import RuleClassifier

    verdict = RuleClassifier().classify("Re: your application", body)

    assert verdict.classification is not Classification.REJECTION, body


async def test_the_model_tier_catches_what_the_rules_and_bayes_miss() -> None:
    """The whole point of the chain, on the message that motivated it."""
    from packages.inbox.classify import classify_message

    class SaysRejection:
        async def complete(self, system, user, **kw):
            return "rejection"

    verdict = await classify_message(*REALISTIC_REJECTION, provider=SaysRejection())

    assert verdict.classification is Classification.REJECTION


async def test_an_unconfident_bayes_guess_is_never_adopted() -> None:
    """It reads this rejection as `interview` at 0.073, below BAYES_MIN_MARGIN.

    Filing a rejection as an interview is worse than filing nothing: the owner
    believes they are waiting on a panel that will never be scheduled. With no
    model to defer to, the honest answer is the rules' abstention.
    """
    from packages.inbox.bayes import train_from_corpus
    from packages.inbox.classify import BAYES_MIN_MARGIN, classify_message

    guess = train_from_corpus().classify(*REALISTIC_REJECTION)
    assert guess.classification is Classification.INTERVIEW
    assert guess.margin < BAYES_MIN_MARGIN

    verdict = await classify_message(*REALISTIC_REJECTION, provider=None)

    assert verdict.classification is not Classification.INTERVIEW


async def test_a_confident_bayes_verdict_is_taken_without_a_model() -> None:
    """Bayes is a real tier, not a formality — it resolves Gate 6's abstention."""
    from packages.inbox.classify import BAYES_MIN_MARGIN, RuleClassifier, classify_message

    abstained = [(w, s, b) for w, s, b in LABELED if RuleClassifier().classify(s, b).abstained]
    assert abstained, "the rules abstain on at least one labeled message"

    for want, subject, body in abstained:
        verdict = await classify_message(subject, body, provider=None)
        assert verdict.classification == want
        assert verdict.margin >= BAYES_MIN_MARGIN


async def test_the_rules_still_win_when_they_are_certain() -> None:
    """Bayes scores 27/30 alone against the rules' 29/30. Order is load-bearing."""
    from packages.inbox.classify import classify_message

    class NeverAsked:
        async def complete(self, system, user, **kw):  # pragma: no cover
            raise AssertionError("the model must not be consulted")

    for want, subject, body in LABELED[:8]:
        verdict = await classify_message(subject, body, provider=NeverAsked())
        assert verdict.classification == want


async def test_a_model_outage_does_not_mis_file_a_message() -> None:
    """An inbox poll must survive Ollama being down.

    It used to survive by filing this as `noise`, which is survival in the
    sense that nothing crashed: the rejection landed nowhere and the
    application stayed open. Now the rules read it before any provider is
    consulted, so the outage costs nothing at all.
    """
    from packages.inbox.classify import classify_message

    class Down:
        async def complete(self, system, user, **kw):
            raise RuntimeError("connection refused")

    verdict = await classify_message(*REALISTIC_REJECTION, provider=Down())

    assert verdict.classification is Classification.REJECTION
