from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from packages.core.models import Company, Posting
from packages.crawler.crawl import _store
from packages.crawler.details import BoardPages, hydrate
from packages.crawler.extract import ExtractedPosting, posting_hash
from packages.crawler.workday import read_detail
from packages.matching.location_fields import normalize, office_facts, wholly_foreign
from packages.matching.search import SearchFilters, matches


@pytest.mark.parametrize(
    ("raw", "country", "state", "remote"),
    [
        ("San Francisco, CA", "US", "CA", None),
        ("Austin, TX - Remote", "US", "TX", True),
        ("New York, NY - Hybrid", "US", "NY", False),
        ("Washington DC", "US", "DC", None),
        ("Glasgow (GB) - Zonda", "GB", None, None),
        ("PH, Taguig", "PH", None, None),
        ("Toronto, ON, CA", "CA", None, None),
        ("2 Locations", None, None, None),
        (None, None, None, None),
    ],
)
def test_fields_are_read_without_filling_missing_facts(raw, country, state, remote):
    fields = normalize(raw)
    assert (fields["country_code"], fields["state_code"], fields["is_remote"]) == (
        country,
        state,
        remote,
    )


def _posting(location, **kwargs):
    fields = normalize(location, **kwargs)
    return Posting(title="Data Scientist", location=location, **fields)


def test_multiple_offices_preserve_each_country_and_any_reachable_us_office():
    posting = _posting("San Francisco, CA; Glasgow (GB) - Zonda")
    assert posting.country_code is None
    assert {o[1] for o in office_facts(posting.locations_json)} == {"US", "GB"}
    assert matches(posting, SearchFilters(us_only=True)).kept


def test_foreign_remote_option_does_not_make_the_us_office_remote():
    posting = _posting("Austin, TX; Toronto, ON - Remote")
    assert posting.is_remote is None
    assert not matches(posting, SearchFilters(us_only=True)).kept
    assert not matches(_posting("Toronto or Remote"), SearchFilters(us_only=True)).kept
    assert matches(_posting("Austin, TX or Remote"), SearchFilters(us_only=True)).kept
    assert matches(_posting("Nashville, TN (Onsite) or Remote"), SearchFilters(us_only=True)).kept


def test_explicit_source_country_and_mode_decide_an_unrecognized_office():
    posting = _posting(
        "Office 123",
        source_locations=[{"raw": "Office 123", "country_code": "GB"}],
        source_remote=True,
    )
    assert posting.country_code == "GB"
    assert posting.is_remote is True
    assert not matches(posting, SearchFilters(us_only=True)).kept
    reading = normalize("Office 123", source_locations=posting.locations_json)
    assert wholly_foreign(reading)


def test_remote_type_onsite_cannot_be_overridden_by_generic_remote_prose():
    posting = _posting("Austin, TX", description="This is a remote role.", source_remote=False)
    assert not matches(posting, SearchFilters(us_only=True)).kept


def test_country_region_city_parts_keep_their_canadian_context():
    posting = _posting("CA | BC | Burnaby | 5566 Trapp Avenue")
    assert posting.country_code == "CA"
    assert not matches(posting, SearchFilters(us_only=True)).kept


async def test_chat_candidates_and_complete_counts_use_structured_locations(db_session):
    from packages.matching.retrieve import _matching_totals, _open_area_total, retrieve

    company = Company(name="Acme", ats_type="workday")
    db_session.add(company)
    await db_session.flush()
    postings = []
    for country, state, remote in [
        ("GB", None, True),
        ("US", "CA", False),
        ("US", "NY", False),
        ("US", "NY", True),
    ]:
        posting = Posting(
            company_id=company.id,
            title="Data Scientist",
            location="Office 123",
            url=f"https://example.test/{country}/{state}",
            description_raw="Python data science",
            country_code=country,
            state_code=state,
            is_remote=remote,
        )
        db_session.add(posting)
        postings.append(posting)
    await db_session.flush()
    result = await retrieve(db_session, "data scientist jobs")
    assert {p.posting_id for p in result.passages} == {postings[1].id, postings[3].id}
    assert result.role_total == 2
    assert result.outside_area == 2
    assert await _open_area_total(db_session, area=True) == 2
    totals = await _matching_totals(
        db_session, [p.id for p in postings], area=True, level=None, allow_unknown=True
    )
    assert totals.total == 2
    assert totals.outside == 2
    remote_only = await retrieve(db_session, "remote data scientist jobs")
    assert [p.posting_id for p in remote_only.passages] == [postings[3].id]


async def test_location_refresh_reads_existing_descriptions_once(db_session):
    from packages.crawler.backfill import _fill_one, find_pending

    company = Company(
        name="Acme", ats_type="workday", slug="acme.wd5.myworkdayjobs.com|acme|AcmeCareers"
    )
    db_session.add(company)
    await db_session.flush()
    posting = Posting(
        company_id=company.id,
        external_id="1",
        ats_type="workday",
        url="https://acme.wd5.myworkdayjobs.com/AcmeCareers/job/X_1",
        title="Data Scientist",
        location="2 Locations",
        description_raw="Old Python",
        content_hash="old",
    )
    db_session.add(posting)
    await db_session.flush()
    assert await find_pending(db_session) == {}
    pending = await find_pending(db_session, locations=True)
    item = next(iter(pending.values()))[0]
    assert item.refresh_location

    class Fetcher:
        async def fetch(self, url):
            return SimpleNamespace(ok=True, text=_detail(), waited=0)

    assert await _fill_one(Fetcher(), db_session, item, {}) == "filled"
    await db_session.refresh(posting)
    assert posting.country_code == "GB"
    assert posting.location == "Glasgow (GB)"
    assert await find_pending(db_session, locations=True) == {}


def _listing(number=1):
    return ExtractedPosting(
        external_id=str(number),
        url=f"https://example.test/job/{number}",
        title="Data Scientist",
        location="2 Locations",
        ats_type="workday",
    )


def _detail(location="Glasgow (GB)", additional=None, country="United Kingdom", remote="Remote"):
    return json.dumps(
        {
            "jobPostingInfo": {
                "jobDescription": "<p>Python data science</p>",
                "location": location,
                "additionalLocations": additional or [],
                "country": {"descriptor": country},
                "remoteType": {"descriptor": remote},
            }
        }
    )


def test_workday_replaces_counts_with_actual_offices_and_keeps_the_list_label():
    resolved = read_detail(_detail(additional=["Edinburgh, UK"]), _listing())
    assert resolved.location == "Glasgow (GB); Edinburgh, UK"
    assert resolved.listed_location == "2 Locations"
    fields = normalize(resolved.location, source_locations=resolved.source_locations)
    assert fields["country_code"] == "GB"
    assert fields["is_remote"] is True
    assert wholly_foreign(fields)


async def test_resolved_locations_survive_a_crawl_when_the_detail_is_not_selected():
    previous = {}
    listings = [_listing(n) for n in range(6)]
    for item in listings:
        previous[item.external_id] = read_detail(_detail(), item)

    class Fetcher:
        async def fetch(self, url):
            return SimpleNamespace(ok=True, text=_detail(), waited=0)

    pages = BoardPages(postings=listings)
    await hydrate(
        Fetcher(),
        pages,
        previous,
        None,
        detail_url=lambda item: item.url,
        read_detail=read_detail,
        max_age_days=0,
    )
    assert pages.postings[-1].location == "Glasgow (GB)"
    assert pages.postings[-1].listed_location == "2 Locations"
    assert pages.postings[-1].content_hash == previous["5"].content_hash


async def test_ingestion_populates_structured_fields_and_filters_foreign_details(db_session):
    company = Company(name="Acme", ats_type="workday")
    db_session.add(company)
    await db_session.flush()
    abroad = read_detail(_detail(), _listing())
    here = read_detail(_detail("San Francisco, CA", country="United States"), _listing(2))
    result = await _store(db_session, company, [abroad, here], max_age_days=0, us_only=True)
    assert result.abroad == 1
    stored = (await db_session.scalars(select(Posting))).one()
    assert stored.country_code == "US"
    assert stored.state_code == "CA"
    assert stored.is_remote is True
    assert stored.listed_location == "2 Locations"
    here.source_locations[0]["source_remote"] = False
    here.content_hash = posting_hash(here)
    await _store(db_session, company, [here], max_age_days=0, us_only=True)
    await db_session.refresh(stored)
    assert stored.is_remote is False


async def test_local_backfill_is_idempotent_and_preserves_source_text(db_session, monkeypatch):
    from contextlib import asynccontextmanager

    from scripts import extract_locations

    posting = Posting(
        url="https://example.test/1", title="Engineer", location="Austin, TX - Remote"
    )
    db_session.add(posting)
    await db_session.flush()

    @asynccontextmanager
    async def session():
        yield db_session

    monkeypatch.setattr(extract_locations, "get_sessionmaker", lambda: session)
    assert (await extract_locations.run())["processed"] == 1
    assert await extract_locations.run() == {}
    await db_session.refresh(posting)
    assert (posting.country_code, posting.state_code, posting.is_remote) == ("US", "TX", True)
    assert posting.location == "Austin, TX - Remote"
