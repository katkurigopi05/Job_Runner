from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine

from tests.test_migration_host_delay_floor import _alembic
from tests.test_migration_posting_dedupe import scratch_database  # noqa: F401


async def test_location_migration_roundtrip_preserves_the_existing_posting(scratch_database):  # noqa: F811
    before, target = "b5d7f9a1c3e6", "f2b4d6e8a0c1"
    _alembic("upgrade", before, scratch_database)
    engine = create_async_engine(scratch_database)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                sa.text(
                    "INSERT INTO postings(url,title,location,description_raw) "
                    "VALUES ('https://example.test/1','Data Scientist','2 Locations','Python')"
                )
            )
        _alembic("upgrade", target, scratch_database)
        async with engine.begin() as connection:
            row = (
                await connection.execute(
                    sa.text(
                        "SELECT country_code,state_code,is_remote,locations_json,location_version "
                        "FROM postings"
                    )
                )
            ).one()
            assert tuple(row) == (None, None, None, None, None)
            await connection.execute(
                sa.text("UPDATE postings SET country_code='US',state_code='CA',is_remote=true")
            )
        _alembic("downgrade", before, scratch_database)
        async with engine.connect() as connection:
            row = (
                await connection.execute(
                    sa.text("SELECT title,location,description_raw FROM postings")
                )
            ).one()
            assert tuple(row) == ("Data Scientist", "2 Locations", "Python")
        _alembic("upgrade", target, scratch_database)
    finally:
        await engine.dispose()
