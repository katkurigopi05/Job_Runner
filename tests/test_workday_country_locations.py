"""Workday country codes must reach both the search and ingestion filters."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from packages.core.models import Company, Posting, Profile
from packages.crawler.crawl import _store
from packages.crawler.extract import ExtractedPosting
from packages.matching.filters import location_matches
from packages.matching.locality import Locality, area_exclusion, locality_of, wholly_abroad
from packages.matching.search import SearchFilters, matches

FOREIGN = [
    "Glasgow (GB) - Zonda",
    "Home-based  - Newcastle-upon-Tyne (GB)",
    "Birmingham (GB) - OTM",
    "GB Chester",
    "Uxbridge - GB",
    "Farnborough, GBR",
    "PH, Taguig",
    "IT-FI-FLORENCE-VIA FELICE MATTEUCCI 2",
    "(CAN) ON DRYDEN 03024 WM SUPERCENTER",
    "MYS - Selangor - Petaling Jaya (Ascent Paradigm)",
    "PL, WROCLAW",
    "Cruiserath - IE",
    "Leiden – NL",
    "IT, Milano, Pioltello",
    "Wuxi",
    "Rungis",
    "Limonest",
    "Penang",
    "Ho Chi Minh City",
    "El Salvador",
    "Caracas, Venezuela",
    "Rheingau-Taunus-Kreis, Idstein, Deutschland",
]


@pytest.mark.parametrize("location", FOREIGN)
def test_foreign_source_locations_are_excluded_from_every_us_search(location: str) -> None:
    posting = Posting(title="Data Scientist", location=location, description_raw="")
    assert locality_of(location) is Locality.ELSEWHERE
    assert wholly_abroad(location)
    assert area_exclusion(
        location, title=posting.title, description="", remote_outside_california=True
    )
    assert not matches(posting, SearchFilters(us_only=True)).kept
    assert not location_matches(Profile(location="San Francisco, CA"), posting)


@pytest.mark.parametrize(
    "location",
    [
        "San Francisco, CA; Glasgow (GB) - Zonda",
        "San Francisco, CA; PH, Taguig",
        "Remote - US or Canada",
        "GB, Glasgow / Austin, TX - Remote",
        "Glasgow, KY - Remote",
        "Birmingham, AL - Remote",
        "Boise, ID - Remote",
        "Dublin, CA",
        "2 Locations",
        "In-Office",
        "IT Support Center",
        "NO LOCATION",
        None,
    ],
)
def test_american_offices_and_unresolved_locations_are_preserved(location: str | None) -> None:
    posting = Posting(title="Data Scientist", location=location, description_raw="")
    assert not wholly_abroad(location)
    assert matches(posting, SearchFilters(us_only=True)).kept


async def test_a_workday_glasgow_listing_is_not_inserted(db_session) -> None:
    company = Company(name="CoStar", ats_type="workday", slug="costar")
    db_session.add(company)
    await db_session.flush()
    foreign = ExtractedPosting(
        external_id="R40232",
        url="https://costar.wd1.myworkdayjobs.com/CoStarCareers/job/Glasgow-GB/R40232",
        title="Data Scientist II - Zonda - Glasgow, Scotland, United Kingdom",
        location="Glasgow (GB) - Zonda",
        ats_type="workday",
        content_hash="glasgow",
    )
    american = foreign.model_copy(
        update={"external_id": "US1", "location": "San Francisco, CA", "content_hash": "sf"}
    )
    result = await _store(db_session, company, [foreign, american], max_age_days=0, us_only=True)
    assert result.abroad == 1
    assert (await db_session.scalars(select(Posting.external_id))).all() == ["US1"]
