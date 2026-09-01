"""The seniority filter, once something actually sets a target.

`filters.seniority_ok` has always been able to reject a rung mismatch, and in
every real run it passed everything: no production caller set
`target_seniority`, so the argument defaulted to None and the filter returned
True unconditionally. It was reachable only from the benchmark.

The gap has a number on it. On the Gate 5 labeled set, arming the target takes
P@10 from 0.900 to 1.000 — the posting it removes is a Junior Backend Engineer
with an otherwise excellent technology match, which is exactly the kind of role
a cosine score cannot refuse on its own.

It stays opt-in. NULL means "do not filter on level", so every profile that
predates the column behaves exactly as it did.
"""

from __future__ import annotations

import uuid

import pytest

from packages.core.enums import SeniorityLevel
from packages.core.models import Posting, Profile
from packages.matching.filters import SENIORITY_LEVELS, apply_filters, seniority_ok

pytestmark = pytest.mark.asyncio


def _profile(target: str | None) -> Profile:
    return Profile(
        id=uuid.uuid4(),
        candidate_id=uuid.uuid4(),
        label="backend",
        location="San Francisco, CA",
        work_auth="US citizen",
        needs_sponsorship=False,
        links_json={},
        answers_kv_json={},
        target_seniority=target,
    )


def _posting(title: str) -> Posting:
    return Posting(
        id=uuid.uuid4(),
        url=f"https://boards.greenhouse.io/acme/jobs/{uuid.uuid4().hex[:6]}",
        title=title,
        description_raw="Python, PostgreSQL.",
        location="San Francisco, CA",
    )


async def test_the_enum_mirrors_the_ladder() -> None:
    """A rung the ladder does not know silently disables the filter.

    `seniority_ok` returns True for a target it cannot place, so a value that
    drifts out of `SENIORITY_LEVELS` reads as "no preference" — an unfiltered
    feed with nothing on screen to explain it. The enum exists to make that
    unrepresentable, which is only true while the two agree.
    """
    assert [level.value for level in SeniorityLevel] == [name for name, _ in SENIORITY_LEVELS]


async def test_no_target_filters_nothing() -> None:
    """The shipped default, and what every pre-existing profile gets."""
    profile = _profile(None)
    assert profile.target_seniority is None
    for title in ("Software Engineering Intern", "Principal Engineer", "Backend Engineer"):
        assert apply_filters(profile, _posting(title)).passed


async def test_a_target_rejects_a_rung_too_far_away() -> None:
    profile = _profile(SeniorityLevel.SENIOR)
    assert not apply_filters(profile, _posting("Software Engineering Intern")).passed
    assert not apply_filters(profile, _posting("Junior Backend Engineer")).passed


async def test_a_target_keeps_one_rung_either_side() -> None:
    """Tolerance is 1: a senior candidate should still see staff and mid."""
    profile = _profile(SeniorityLevel.SENIOR)
    assert apply_filters(profile, _posting("Senior Backend Engineer")).passed
    assert apply_filters(profile, _posting("Principal Engineer")).passed


async def test_a_posting_that_does_not_state_a_rung_is_kept() -> None:
    """Silence is not a mismatch — roughly half of any real board says nothing."""
    assert seniority_ok(_posting("Backend Engineer"), "senior")


async def test_the_profile_supplies_the_target_to_scoring(db_session) -> None:
    """The wiring, which is the part that was missing.

    `score_and_store` reads `profile.target_seniority` when no caller names
    one, so every real path — crawl, discover, rescore — now filters on level.
    Asserted through `apply_filters` rather than the whole scorer because this
    is about where the value comes from, not what the cosine does with it.
    """
    from packages.matching.filters import apply_filters as run

    profile = _profile(SeniorityLevel.SENIOR)
    junior = _posting("Junior Backend Engineer")

    assert run(profile, junior, target_seniority=profile.target_seniority).passed is False
    assert run(profile, junior).passed is False, "the profile's own rung must be used"
