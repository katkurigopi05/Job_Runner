"""Opt-in admission uses the search rule, retaining unknown and held jobs."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from packages.core.config import Settings, get_settings
from packages.core.models import Posting
from packages.crawler.crawl import _store
from tests.test_crawl_skips_abroad import _company, _posting, _stored


def test_search_area_admission_defaults_off():
    assert Settings(_env_file=None).crawler_store_search_area_only is False


@pytest.mark.parametrize(
    "location,description,sources,kept",
    [
        ("Austin, TX", "On-site role.", None, False),
        ("New York, NY (Hybrid)", "Hybrid role.", None, False),
        ("San Francisco, CA", "On-site role.", None, True),
        ("Los Angeles, CA", "Hybrid role.", None, True),
        ("Remote - Austin, TX", None, None, True),
        ("Austin, TX", "This position is fully remote.", None, True),
        ("Toronto, ON; San Francisco, CA", None, None, True),
        ("Austin, TX; San Francisco, CA", None, None, True),
        ("Austin, TX or Remote", None, None, True),
        (None, None, None, True),
        ("Remote", None, None, True),
        ("In-Office", None, None, True),
        ("6 Locations", None, None, True),
        ("Unrecognized Place", None, None, True),
        ("Austin, TX; Unrecognized Place", None, None, True),
        ("United States", None, None, True),
        (
            "2 Locations",
            None,
            [
                {"raw": "Austin, TX", "country_code": "US", "source_remote": False},
                {"raw": "Unrecognized Place"},
            ],
            True,
        ),
        (
            "2 Locations",
            None,
            [
                {"raw": "Austin, TX", "country_code": "US", "source_remote": False},
                {"raw": "Boston, MA", "country_code": "US", "source_remote": False},
            ],
            False,
        ),
    ],
)
async def test_admission_only_rejects_positive_outside_evidence(
    db_session, monkeypatch, location, description, sources, kept
):
    monkeypatch.setattr(get_settings(), "crawler_store_search_area_only", True)
    monkeypatch.setattr(get_settings(), "search_remote_outside_california", True)
    company = await _company(db_session)
    item = _posting("job", location).model_copy(
        update={
            "description_raw": description,
            "source_locations": sources,
        }
    )
    result = await _store(db_session, company, [item], max_age_days=0, us_only=True)
    assert await _stored(db_session) == ({"job"} if kept else set())
    assert result.outside_area == (0 if kept else 1)


@pytest.mark.parametrize(
    "enabled,us_only,remote_only",
    [
        (False, True, True),
        (True, False, True),
        (True, True, False),
    ],
)
async def test_disabled_or_widened_settings_keep_other_states(
    db_session, monkeypatch, enabled, us_only, remote_only
):
    monkeypatch.setattr(get_settings(), "crawler_store_search_area_only", enabled)
    monkeypatch.setattr(get_settings(), "search_remote_outside_california", remote_only)
    company = await _company(db_session)
    result = await _store(
        db_session, company, [_posting("job", "Austin, TX")], max_age_days=0, us_only=us_only
    )
    assert await _stored(db_session) == {"job"}
    assert result.outside_area == 0


async def test_held_outside_posting_is_updated_seen_and_reopened(db_session, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "crawler_store_search_area_only", False)
    company = await _company(db_session)
    before = datetime.now(UTC) - timedelta(days=1)
    item = _posting("held", "Austin, TX")
    await _store(db_session, company, [item], max_age_days=0, now=before)
    held = await db_session.scalar(select(Posting).where(Posting.external_id == "held"))
    held.closed_at = before
    await db_session.flush()
    monkeypatch.setattr(settings, "crawler_store_search_area_only", True)
    now = datetime.now(UTC)
    item = item.model_copy(
        update={"description_raw": "Updated job text", "content_hash": "changed"}
    )
    result = await _store(db_session, company, [item], max_age_days=0, now=now)
    await db_session.refresh(held)
    assert result.outside_area == 0
    assert held.last_seen_at == now and held.closed_at is None
    assert held.description_raw == "Updated job text"
    assert held.id in result.updated


async def test_unchanged_held_outside_posting_still_gets_seen_stamp(db_session, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "crawler_store_search_area_only", False)
    company = await _company(db_session)
    before = datetime.now(UTC) - timedelta(days=1)
    item = _posting("held", "Austin, TX")
    await _store(db_session, company, [item], max_age_days=0, now=before)
    monkeypatch.setattr(settings, "crawler_store_search_area_only", True)
    now = datetime.now(UTC)
    result = await _store(db_session, company, [item], max_age_days=0, now=now)
    held = await db_session.scalar(select(Posting).where(Posting.external_id == "held"))
    assert held.last_seen_at == now and held.closed_at is None
    assert result.outside_area == 0 and result.changed == []
