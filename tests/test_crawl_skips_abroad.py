"""The crawler does not store a new posting whose every office is abroad.

The owner's search area has no place for these, so storing them only cost an
embedding each and filled the table: on 2026-10-28 a sweep of the owner's
boards added 12,573 postings in six hours, 3,703 of them wholly abroad, and
9,175 were deleted by hand. The search area already hid them from the feed.

What the rule must not do is drop a job the owner could hold: one American
office keeps a posting, and so does a location no rule can place.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from packages.core.models import Company, Posting
from packages.crawler.crawl import _store
from packages.crawler.extract import ExtractedPosting
from packages.matching.locality import wholly_abroad

ABROAD = ["London, United Kingdom", "Pune, India", "Toronto, ON", "BRA-São Paulo", "Singapore"]
KEPT = [
    "San Francisco, CA",
    "Remote - US",
    "San Francisco, CA; Toronto, ON; Remote US & Canada",
    "2 Locations",
    "Remote",
    None,
    "",
    "Dublin",
    "Dublin, CA",
    "Ontario",
]


@pytest.mark.parametrize("location", ABROAD)
def test_a_wholly_foreign_location_is_abroad(location: str) -> None:
    assert wholly_abroad(location)


@pytest.mark.parametrize("location", KEPT)
def test_an_american_unplaced_or_ambiguous_location_is_not(location: str | None) -> None:
    assert not wholly_abroad(location)


def _posting(external_id: str, location: str | None) -> ExtractedPosting:
    return ExtractedPosting(
        external_id=external_id,
        url=f"https://boards.greenhouse.io/acme/jobs/{external_id}",
        title="Backend Engineer",
        location=location,
        description_raw="<p>Python</p>",
        ats_type="greenhouse",
        content_hash=f"hash-{external_id}",
    )


async def _company(db_session) -> Company:
    company = Company(name="Acme", ats_type="greenhouse", slug="acme")
    db_session.add(company)
    await db_session.flush()
    return company


async def _stored(db_session) -> set[str]:
    return set((await db_session.scalars(select(Posting.external_id))).all())


async def test_new_postings_wholly_abroad_are_not_stored(db_session) -> None:
    company = await _company(db_session)
    listed = [
        _posting("sf", "San Francisco, CA"),
        _posting("mixed", "San Francisco, CA; Toronto, ON"),
        _posting("unplaced", "2 Locations"),
        _posting("london", "London, United Kingdom"),
        _posting("pune", "Pune, India"),
    ]
    result = await _store(db_session, company, listed, max_age_days=0, us_only=True)
    assert result.abroad == 2
    assert await _stored(db_session) == {"sf", "mixed", "unplaced"}


async def test_a_held_posting_abroad_is_still_stamped_as_seen(db_session) -> None:
    """Skipping only applies to new rows, like the age rule.

    `_close_missing` closes whatever a crawl did not stamp, so skipping a held
    posting would close one the board still lists.
    """
    company = await _company(db_session)
    await _store(db_session, company, [_posting("london", "London")], max_age_days=0, us_only=False)
    result = await _store(
        db_session, company, [_posting("london", "London")], max_age_days=0, us_only=True
    )
    assert result.abroad == 0
    assert await _stored(db_session) == {"london"}


async def test_us_only_off_stores_everything(db_session) -> None:
    company = await _company(db_session)
    result = await _store(
        db_session, company, [_posting("london", "London")], max_age_days=0, us_only=False
    )
    assert result.abroad == 0
    assert await _stored(db_session) == {"london"}
