"""The database rejects malformed or contradictory location metadata."""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from packages.core.models import Posting
from tests.test_migration_host_delay_floor import _alembic
from tests.test_migration_posting_dedupe import scratch_database  # noqa: F401

US = {"raw": "San Francisco, CA", "country_code": "US", "state_code": "CA", "is_remote": True}
GB = {"raw": "Glasgow (GB)", "country_code": "GB", "state_code": None, "is_remote": False}
INVALID = [
    {"country_code": "us"},
    {"state_code": "CA"},
    {"country_code": "GB", "state_code": "CA"},
    {"country_code": "US", "state_code": "ZZ"},
    {"location_version": 0},
    {"locations_json": {}},
    {"locations_json": ["San Francisco"]},
    {"locations_json": [{"raw": ""}]},
    {"locations_json": [{"raw": "Office", "country_code": "us"}]},
    {"locations_json": [{"raw": "Office", "state_code": "CA", "country_code": "GB"}]},
    {"locations_json": [{"raw": "Office", "is_remote": "yes"}]},
    {"locations_json": [{"raw": "Office", "source_remote": "yes"}]},
    {"country_code": "US", "locations_json": [GB]},
    {"country_code": "US", "state_code": "NY", "locations_json": [US]},
    {"is_remote": False, "locations_json": [US]},
]


@pytest.mark.parametrize("fields", INVALID)
async def test_direct_writes_cannot_bypass_location_rules(db_session, fields):
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            db_session.add(Posting(url="https://example.test/invalid", **fields))
            await db_session.flush()


async def test_unknown_and_mixed_locations_remain_valid(db_session):
    rows = [
        Posting(url="https://example.test/legacy"),
        Posting(url="https://example.test/unknown", location_version=1, locations_json=[]),
        Posting(url="https://example.test/mixed", location_version=1, locations_json=[US, GB]),
        Posting(
            url="https://example.test/us",
            country_code="US",
            state_code="CA",
            is_remote=True,
            location_version=1,
            locations_json=[US],
        ),
    ]
    db_session.add_all(rows)
    await db_session.flush()


async def test_versioned_writes_require_a_location_array(db_session):
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await db_session.execute(
                sa.text(
                    "INSERT INTO postings(url,location_version) VALUES ('https://example.test/1',1)"
                )
            )


async def test_migration_enforces_the_same_rules_and_rolls_back(scratch_database):  # noqa: F811
    _alembic("upgrade", "f2b4d6e8a0c1", scratch_database)
    engine = create_async_engine(scratch_database)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                sa.text(
                    "INSERT INTO postings(url,location) "
                    "VALUES ('https://example.test/original','2 Locations')"
                )
            )
        _alembic("upgrade", "a3c5e7f9b1d2", scratch_database)
        async with engine.begin() as connection:
            for fields in INVALID:
                with pytest.raises(IntegrityError):
                    async with connection.begin_nested():
                        await connection.execute(
                            sa.insert(Posting).values(url="https://example.test/invalid", **fields)
                        )
        _alembic("downgrade", "f2b4d6e8a0c1", scratch_database)
        async with engine.begin() as connection:
            assert (
                await connection.scalar(sa.text("SELECT location FROM postings")) == "2 Locations"
            )
            await connection.execute(
                sa.text("UPDATE postings SET country_code='GB',state_code='CA'")
            )
    finally:
        await engine.dispose()
