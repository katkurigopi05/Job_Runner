"""The alias was never applied with, so no reply could ever conclude anything.

`packages/inbox/alias.py` calls plus-addressing "the whole mechanism", and
`route.py` enforces it: an inferred link attaches a message and stops, and only
an exact alias may set an outcome or hand an OTP to the state machine.

Both halves were built. The join was not. `alias_for` had no caller outside
this suite, `Settings.inbox_alias_base` had no reader at all, and
`packages/ats/answers.py` typed `candidate.email` — the bare address — into
every form. So the tag never reached an employer, no reply could carry one
back, and no recruiter mail could move an application. Measured before the fix:

    email typed into the form : owner@gmail.com
    alias the reply must carry: owner+app1b7b...@gmail.com
    find_alias(typed)         : None

Gate 6 passed throughout, because `email_for` in `tests/test_inbox.py` builds
the alias with `alias_for` itself — the fixture did the job the product did
not, which is the §15 pattern exactly.

These tests are the join, and the last one is the loop: apply with the address
this pipeline produces, reply to it, and check the outcome lands.
"""

from __future__ import annotations

import inspect
import uuid
from datetime import UTC, datetime

import pytest

from apps.worker import apply_job
from packages.ats.answers import build_answers
from packages.ats.base import Question, QuestionKind
from packages.core.config import get_settings
from packages.core.models import Application, Candidate, Profile, User
from packages.inbox.alias import AliasError, find_alias, reply_address
from packages.inbox.route import InboundEmail, route_message

BASE = "owner@gmail.com"


def _email_question() -> Question:
    """The field this whole mechanism turns on."""
    return Question(key="email", label="Email", kind=QuestionKind.EMAIL, required=True)


async def _setup(db_session, *, email_mode: str, managed_alias: str | None):
    """One candidate, profile and application, with the email mode under test."""
    suffix = uuid.uuid4().hex[:8]
    user = User(email=f"u-{suffix}@example.com")
    db_session.add(user)
    await db_session.flush()

    candidate = Candidate(
        user_id=user.id,
        name="Jane Doe",
        email=f"jane-{suffix}@example.com",
        email_mode=email_mode,
        managed_alias=managed_alias,
    )
    db_session.add(candidate)
    await db_session.flush()

    profile = Profile(candidate_id=candidate.id, label="default")
    db_session.add(profile)
    await db_session.flush()

    application = Application(
        candidate_id=candidate.id,
        profile_id=profile.id,
        url=f"https://x.test/{suffix}",
        ats="greenhouse",
    )
    db_session.add(application)
    await db_session.flush()
    return candidate, profile, application


# --------------------------------------------------------------------------
# Which address the form is handed
# --------------------------------------------------------------------------


async def test_a_managed_candidate_applies_with_this_applications_alias(db_session) -> None:
    """The defect, at the point it happens: what gets typed into the form."""
    candidate, profile, application = await _setup(
        db_session, email_mode="managed", managed_alias=BASE
    )

    typed = build_answers(
        [_email_question()],
        candidate,
        profile,
        reply_to=apply_job._reply_to(candidate, application),
    )["email"]

    parsed = find_alias(typed)
    assert parsed is not None, f"{typed!r} carries no tag, so no reply can route back"
    assert parsed.application_id == application.id
    assert parsed.base_address == BASE


async def test_self_mode_still_applies_with_the_candidates_own_address(db_session) -> None:
    """The shipped default is unchanged: no tag unless the owner asked for one."""
    candidate, profile, application = await _setup(
        db_session, email_mode="self", managed_alias=None
    )

    assert apply_job._reply_to(candidate, application) is None
    typed = build_answers([_email_question()], candidate, profile, reply_to=None)["email"]
    assert typed == candidate.email


async def test_two_applications_get_two_different_aliases(db_session) -> None:
    """One key per application is the point — three roles at one company."""
    candidate, _, first = await _setup(db_session, email_mode="managed", managed_alias=BASE)
    second = Application(
        candidate_id=candidate.id,
        profile_id=first.profile_id,
        url=f"https://x.test/{uuid.uuid4().hex[:8]}",
        ats="greenhouse",
    )
    db_session.add(second)
    await db_session.flush()

    assert apply_job._reply_to(candidate, first) != apply_job._reply_to(candidate, second)


async def test_the_install_wide_mailbox_is_used_when_the_candidate_names_none(
    db_session, monkeypatch
) -> None:
    """`INBOX_ALIAS_BASE` had no reader anywhere. It is the fallback base."""
    candidate, _, application = await _setup(db_session, email_mode="managed", managed_alias=None)
    settings = get_settings()
    monkeypatch.setattr(settings, "inbox_alias_base", BASE)

    parsed = find_alias(apply_job._reply_to(candidate, application) or "")
    assert parsed is not None
    assert parsed.application_id == application.id


async def test_a_managed_candidate_with_no_mailbox_applies_as_themselves(
    db_session, monkeypatch
) -> None:
    """A configuration gap must not fail an application — but it is not silent.

    `_reply_to` logs `applying_with_the_candidate_address_no_usable_alias`; the
    assertion here is only that the run survives and no broken tag is typed.
    """
    candidate, profile, application = await _setup(
        db_session, email_mode="managed", managed_alias=None
    )
    monkeypatch.setattr(get_settings(), "inbox_alias_base", None)

    assert apply_job._reply_to(candidate, application) is None
    typed = build_answers([_email_question()], candidate, profile, reply_to=None)["email"]
    assert typed == candidate.email


@pytest.mark.parametrize(
    "base",
    [
        "owner+existing@gmail.com",
        "not-an-address",
        "owner@@gmail.com",
        "a@b@gmail.com",
        "owner@",
        "@gmail.com",
    ],
)
async def test_an_unusable_base_never_produces_a_tag_that_cannot_route(
    db_session, base: str
) -> None:
    """Every shape here yields an address no employer's form can send to.

    `owner@@gmail.com` is the one worth spelling out. It used to pass — the
    check was `"@" not in base_address`, which a doubled `@` satisfies — and
    produced `owner+app…@@gmail.com`, which `parse_alias` then read back as a
    valid alias of ours. Malformed at the employer, and correct-looking at
    both of our own ends, which is the combination that hides.
    """
    with pytest.raises(AliasError):
        reply_address(email_mode="managed", base_address=base, application_id=uuid.uuid4())


@pytest.mark.parametrize("address", ["owner+app" + "0" * 32 + "@@gmail.com"])
def test_an_address_we_could_not_have_issued_is_not_read_as_ours(address: str) -> None:
    """The inbound half of the same rule.

    `find_alias` decides whether a reply may conclude an outcome, so an address
    we could never have issued must not answer to one. The domain group was
    `.+`, which swallowed the second `@`.
    """
    assert find_alias(address) is None


def test_the_pipeline_passes_the_alias_to_the_form() -> None:
    """The join itself. Dropping this line restores the original defect, and
    every other test in this file would still pass."""
    source = inspect.getsource(apply_job._run_pipeline)
    assert "reply_to=_reply_to(candidate, application)" in source


# --------------------------------------------------------------------------
# The loop: apply with it, reply to it, land the outcome
# --------------------------------------------------------------------------


async def test_a_reply_to_the_applied_with_address_concludes_the_application(
    db_session,
) -> None:
    """End to end, using only the address the pipeline actually produces.

    Before the fix this reached `route_message` as `jane-…@example.com`, took
    the unrouted branch, and set no outcome at all.
    """
    candidate, profile, application = await _setup(
        db_session, email_mode="managed", managed_alias=BASE
    )
    applied_with = build_answers(
        [_email_question()],
        candidate,
        profile,
        reply_to=apply_job._reply_to(candidate, application),
    )["email"]

    result = await route_message(
        db_session,
        InboundEmail(
            message_id=f"<{uuid.uuid4()}@mail>",
            from_addr="recruiter@acme.com",
            to_addr=applied_with,
            subject="Interview invitation",
            body="We would like to schedule a call with you next week.",
            received_at=datetime.now(UTC),
        ),
    )

    assert result.application_id == str(application.id)
    assert result.link_method == "alias", "an inferred link may not conclude anything"
    assert result.outcome_set == "interview"
    assert application.outcome == "interview"
