"""`d8f0a2c4e6b8` adds the table for larger chunk vectors, and takes nothing else with it.

Run through Alembic on a throwaway database, as `test_migration_posting_dedupe`
does, because the suite's schema never runs a migration.
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine

from tests.test_migration_host_delay_floor import _alembic
from tests.test_migration_posting_dedupe import scratch_database  # noqa: F401

BEFORE = "c4e6a8b0d2f4"
UNDER_TEST = "d8f0a2c4e6b8"


async def _scalar(url: str, query: str, **params) -> object:
    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            return (await conn.execute(sa.text(query), params)).scalar()
    finally:
        await engine.dispose()


async def test_the_table_holds_2560_wide_vectors_that_go_with_their_chunk(scratch_database):  # noqa: F811
    _alembic("upgrade", UNDER_TEST, scratch_database)

    column = await _scalar(
        scratch_database,
        "SELECT format_type(atttypid, atttypmod) FROM pg_attribute "
        "WHERE attrelid = 'posting_chunk_vectors'::regclass AND attname = 'embedding'",
    )
    assert column == "halfvec(2560)"

    engine = create_async_engine(scratch_database)
    try:
        async with engine.begin() as conn:
            company = uuid.uuid4()
            posting = uuid.uuid4()
            chunk = uuid.uuid4()
            await conn.execute(
                sa.text(
                    "INSERT INTO companies (id, name, ats_type) VALUES (:id, 'Acme', 'greenhouse')"
                ),
                {"id": company},
            )
            await conn.execute(
                sa.text(
                    "INSERT INTO postings (id, company_id, url, title, first_seen_at) "
                    "VALUES (:id, :company, 'https://jobs.example.test/1', 'Engineer', now())"
                ),
                {"id": posting, "company": company},
            )
            await conn.execute(
                sa.text(
                    "INSERT INTO posting_chunks (id, posting_id, ordinal, start, length, "
                    "embedding_model, embedding) VALUES (:id, :posting, 0, 0, 10, "
                    "'sentence-transformers', CAST(:small AS halfvec(384)))"
                ),
                {"id": chunk, "posting": posting, "small": "[" + ",".join(["0"] * 384) + "]"},
            )
            await conn.execute(
                sa.text(
                    "INSERT INTO posting_chunk_vectors (chunk_id, embedding_model, embedding) "
                    "VALUES (:chunk, 'Qwen/Qwen3-Embedding-4B', CAST(:large AS halfvec(2560)))"
                ),
                {"chunk": chunk, "large": "[" + ",".join(["0.01"] * 2560) + "]"},
            )
            await conn.execute(sa.text("DELETE FROM posting_chunks WHERE id = :id"), {"id": chunk})
            left = (
                await conn.execute(sa.text("SELECT count(*) FROM posting_chunk_vectors"))
            ).scalar()
    finally:
        await engine.dispose()
    assert left == 0, "a chunk's vectors go with it"

    _alembic("downgrade", BEFORE, scratch_database)
    assert await _scalar(scratch_database, "SELECT to_regclass('posting_chunk_vectors')") is None
    assert await _scalar(scratch_database, "SELECT to_regclass('posting_chunks')") is not None
