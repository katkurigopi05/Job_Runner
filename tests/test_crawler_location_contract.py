"""Every crawler entry point must honor the same stored-location contract."""

from __future__ import annotations

import json

import httpx
import pytest
from sqlalchemy import select

from packages.core.config import get_settings
from packages.core.models import Company, Posting
from packages.crawler.aggregators import RemotiveSource
from packages.crawler.crawl import _store
from packages.crawler.discover import ingest
from packages.crawler.extract import ExtractedPosting
from packages.crawler.fetch import PoliteFetcher
from packages.matching.location_fields import LOCATION_VERSION
from tests.test_discovery import limiter, transport


@pytest.fixture(autouse=True)
def us_only(monkeypatch):
    monkeypatch.setenv("SEARCH_US_ONLY", "true")
    get_settings.cache_clear()
    yield
    monkeypatch.undo()
    get_settings.cache_clear()


@pytest.mark.parametrize(
    "ats", ["workday", "greenhouse", "lever", "ashby", "workable", "breezy", "rippling", "bespoke"]
)
async def test_every_board_writer_normalizes_and_rejects_foreign_jobs(db_session, ats):
    company = Company(name="Acme", ats_type=ats)
    db_session.add(company)
    await db_session.flush()
    postings = [
        ExtractedPosting(
            external_id=str(index),
            url=f"https://example.test/{index}",
            title="Data Scientist",
            location=location,
            ats_type=ats,
            content_hash=str(index),
        )
        for index, location in enumerate(
            [
                "Glasgow (GB) - Zonda",
                "San Francisco, CA; Glasgow (GB) - Zonda",
                "2 Locations",
            ]
        )
    ]
    report = await _store(db_session, company, postings, max_age_days=0)
    assert report.abroad == 1
    rows = (await db_session.scalars(select(Posting).order_by(Posting.external_id))).all()
    assert [row.external_id for row in rows] == ["1", "2"]
    assert all(row.location_version == LOCATION_VERSION for row in rows)
    assert all(isinstance(row.locations_json, list) for row in rows)
    assert rows[0].country_code is None  # US and GB offices remain distinct.
    assert rows[1].country_code is rows[1].state_code is rows[1].is_remote is None


async def test_aggregator_discovery_uses_the_same_location_admission(db_session):
    feed = {
        "jobs": [
            {
                "id": index,
                "title": "Data Scientist",
                "url": f"https://remotive.com/j/{index}",
                "company_name": "Acme",
                "candidate_required_location": location,
                "description": "Python data science",
            }
            for index, location in enumerate(["Glasgow (GB) - Zonda", "San Francisco, CA"], 1)
        ]
    }
    fetcher = PoliteFetcher(
        transport=transport({"remotive.com/api": httpx.Response(200, text=json.dumps(feed))}),
        rate_limiter=limiter(),
    )
    result = await ingest(db_session, fetcher, sources=(RemotiveSource(),), resolve_ats=False)
    assert result.new_postings == 1
    assert result.abroad == 1
    stored = (await db_session.scalars(select(Posting))).one()
    assert (stored.country_code, stored.state_code) == ("US", "CA")
    assert stored.location_version == LOCATION_VERSION
