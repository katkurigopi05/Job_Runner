"""Swipe decisions becoming a labeled set.

`Provenance.OWNER` and `Provenance.FEEDBACK` were defined when `labels.py` was
written and nothing ever produced one, so every label in the repo is a
`FIXTURE` — written beside the code that reads it. That is why
`bench_matching` refuses to name a production candidate and why CLAUDE.md §15
says Gate 5 cannot answer the question it was written to ask.

The judgements were already being collected. `/swipe` writes `Match.decision`
on every yes or no. This is the export that was missing in between.
"""

from __future__ import annotations

import uuid

import pytest

from packages.core.models import Candidate, Match, Posting, Profile, User
from packages.matching.feedback import (
    INTERESTED_RELEVANCE,
    MIN_USEFUL_LABELS,
    SKIPPED_RELEVANCE,
    export_decisions,
)
from packages.matching.labels import Provenance, dump_labeled_set, load_labeled_set

pytestmark = pytest.mark.asyncio


async def _owner(session) -> Profile:
    suffix = uuid.uuid4().hex[:8]
    user = User(email=f"o-{suffix}@example.com")
    session.add(user)
    await session.flush()
    candidate = Candidate(user_id=user.id, name="Owner", email=f"c-{suffix}@example.com")
    session.add(candidate)
    await session.flush()
    profile = Profile(
        candidate_id=candidate.id,
        label=f"p-{suffix}",
        location="San Francisco, CA",
        links_json={},
        answers_kv_json={},
    )
    session.add(profile)
    await session.flush()
    return profile


async def _swipe(session, profile: Profile, title: str, decision: str | None) -> None:
    posting = Posting(
        url=f"https://boards.greenhouse.io/acme/jobs/{uuid.uuid4().hex[:8]}",
        external_id=uuid.uuid4().hex[:10],
        title=title,
        description_raw=f"{title}. Python, PostgreSQL.",
        location="Remote - US",
    )
    session.add(posting)
    await session.flush()
    session.add(
        Match(
            profile_id=profile.id,
            posting_id=posting.id,
            score=0.5,
            reasons_json={},
            decision=decision,
        )
    )
    await session.flush()


async def test_a_swipe_becomes_a_graded_posting(db_session) -> None:
    profile = await _owner(db_session)
    await _swipe(db_session, profile, "Senior Backend Engineer", "interested")
    await _swipe(db_session, profile, "Pastry Chef", "skipped")

    labeled, report = await export_decisions(db_session, profile)

    assert labeled is not None
    assert report.interested == 1
    assert report.skipped == 1
    grades = {item.title: item.relevance for item in labeled.items}
    assert grades["Senior Backend Engineer"] == INTERESTED_RELEVANCE
    assert grades["Pastry Chef"] == SKIPPED_RELEVANCE


async def test_the_provenance_is_feedback_not_owner(db_session) -> None:
    """A swipe is inferred evidence, not a grade the owner chose.

    It is binary, so it can never express the gap between "would apply" and
    "would drop everything for" — which is exactly the gap NDCG's `2**rel`
    gain exists to reward. Labelling it `OWNER` would let a coarse signal be
    mistaken for a considered one.
    """
    profile = await _owner(db_session)
    await _swipe(db_session, profile, "Backend Engineer", "interested")

    labeled, _ = await export_decisions(db_session, profile)

    assert labeled is not None
    assert all(item.provenance is Provenance.FEEDBACK for item in labeled.items)
    assert not labeled.is_fixture_only


async def test_an_undecided_match_is_not_a_label(db_session) -> None:
    """Silence is not a judgement. Counting it as `skipped` would grade every
    posting the owner has not got to yet as irrelevant."""
    profile = await _owner(db_session)
    await _swipe(db_session, profile, "Backend Engineer", None)

    labeled, report = await export_decisions(db_session, profile)

    assert labeled is None
    assert report.undecided == 1
    assert report.labeled == 0


async def test_it_says_when_there_is_not_enough_to_measure(db_session) -> None:
    """A number computed from three labels is worse than no number."""
    profile = await _owner(db_session)
    await _swipe(db_session, profile, "Backend Engineer", "interested")

    _, report = await export_decisions(db_session, profile)

    assert report.labeled < MIN_USEFUL_LABELS
    assert str(MIN_USEFUL_LABELS) in report.summary()
    assert "One class only" in report.summary()


async def test_the_export_round_trips_through_the_loader(db_session, tmp_path) -> None:
    """The writer sits next to the reader so the schema cannot drift.

    An exported set that `load_labeled_set` refuses is a file the benchmark
    cannot read, which would make the whole path silently useless.
    """
    profile = await _owner(db_session)
    await _swipe(db_session, profile, "Senior Backend Engineer", "interested")
    await _swipe(db_session, profile, "Line Cook", "skipped")

    labeled, _ = await export_decisions(db_session, profile)
    assert labeled is not None

    path = dump_labeled_set(labeled, tmp_path / "feedback.yaml")
    reloaded = load_labeled_set(path)

    assert len(reloaded.items) == len(labeled.items)
    assert {i.key for i in reloaded.items} == {i.key for i in labeled.items}
    assert {i.relevance for i in reloaded.items} == {INTERESTED_RELEVANCE, SKIPPED_RELEVANCE}
    assert all(i.provenance is Provenance.FEEDBACK for i in reloaded.items)


async def test_two_profiles_do_not_share_a_corpus(db_session) -> None:
    """A labeled set is defined against one `profile_text`.

    Merging two would average two different people's taste into one number
    that describes neither.
    """
    first = await _owner(db_session)
    second = await _owner(db_session)
    await _swipe(db_session, first, "Backend Engineer", "interested")
    await _swipe(db_session, second, "Data Scientist", "interested")

    first_set, _ = await export_decisions(db_session, first)
    second_set, _ = await export_decisions(db_session, second)

    assert first_set is not None and second_set is not None
    assert {i.title for i in first_set.items} == {"Backend Engineer"}
    assert {i.title for i in second_set.items} == {"Data Scientist"}
