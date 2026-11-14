"""Existing HNSW databases receive evidence tables without losing existing data."""

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine

from tests.test_migration_host_delay_floor import _alembic
from tests.test_migration_posting_dedupe import scratch_database  # noqa: F401


async def test_studio_migration_upgrades_existing_hnsw_and_roundtrips(scratch_database):  # noqa: F811
    _alembic("upgrade", "d7e9f1a3b5c7", scratch_database)
    _alembic("upgrade", "head", scratch_database)
    engine = create_async_engine(scratch_database)
    try:
        async with engine.connect() as connection:
            assert await connection.scalar(sa.text("SELECT to_regclass('internships')"))
            assert await connection.scalar(sa.text("SELECT to_regclass('certifications')"))
            assert await connection.scalar(
                sa.text("SELECT to_regclass('ix_posting_chunks_embedding_hnsw')")
            )
        _alembic("downgrade", "d7e9f1a3b5c7", scratch_database)
        async with engine.connect() as connection:
            assert await connection.scalar(sa.text("SELECT to_regclass('internships')")) is None
        _alembic("upgrade", "head", scratch_database)
    finally:
        await engine.dispose()


async def test_studio_migration_preserves_tables_from_early_development(scratch_database):  # noqa: F811
    _alembic("upgrade", "head", scratch_database)
    engine = create_async_engine(scratch_database)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                sa.text("INSERT INTO users(email) VALUES ('owner@example.com')")
            )
            await connection.execute(
                sa.text(
                    "INSERT INTO candidates(user_id,name,email,email_mode) "
                    "SELECT id,'Alex Rivera','alex@example.com','self' FROM users"
                )
            )
            await connection.execute(
                sa.text(
                    "INSERT INTO certifications(candidate_id,name,issuer,verified) "
                    "SELECT id,'Example Certification','Example Issuer',true FROM candidates"
                )
            )
        # Simulate the original development lineage: tables already existed
        # while the migration marker remained at HNSW.
        _alembic("stamp", "d7e9f1a3b5c7", scratch_database)
        _alembic("upgrade", "head", scratch_database)
        async with engine.connect() as connection:
            assert (
                await connection.scalar(sa.text("SELECT name FROM certifications"))
                == "Example Certification"
            )
    finally:
        await engine.dispose()
