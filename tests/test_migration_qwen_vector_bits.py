"""`e1a3c5e7f9b2` adds the sign-bit column, filled by Postgres for rows already there.

The owner's 223,576 vectors were imported before this column existed, so the
migration itself must fill it; a generated column does, and keeps it in step
with every later insert. Run through Alembic on a throwaway database, as
`test_migration_posting_dedupe` does.
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine

from tests.test_migration_host_delay_floor import _alembic
from tests.test_migration_posting_dedupe import scratch_database  # noqa: F401

BEFORE = "d8f0a2c4e6b8"
UNDER_TEST = "e1a3c5e7f9b2"


async def _run(url: str, *statements: tuple[str, dict]) -> object:
    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            result = None
            for sql, params in statements:
                executed = await conn.execute(sa.text(sql), params)
                result = executed.scalar() if executed.returns_rows else None
            return result
    finally:
        await engine.dispose()


async def test_existing_vectors_get_their_sign_bits_and_it_goes_back(scratch_database):  # noqa: F811
    _alembic("upgrade", BEFORE, scratch_database)
    company, posting, chunk = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    large = "[" + ",".join(["0.5", "-0.5"] + ["0"] * 2558) + "]"
    await _run(
        scratch_database,
        (
            "INSERT INTO companies (id, name, ats_type) VALUES (:id, 'Acme', 'greenhouse')",
            {"id": company},
        ),
        (
            "INSERT INTO postings (id, company_id, url, title, first_seen_at) "
            "VALUES (:id, :company, 'https://jobs.example.test/1', 'Engineer', now())",
            {"id": posting, "company": company},
        ),
        (
            "INSERT INTO posting_chunks (id, posting_id, ordinal, start, length, embedding_model, "
            "embedding) VALUES (:id, :posting, 0, 0, 10, 'sentence-transformers', "
            "CAST(:small AS halfvec(384)))",
            {"id": chunk, "posting": posting, "small": "[" + ",".join(["0"] * 384) + "]"},
        ),
        (
            "INSERT INTO posting_chunk_vectors (chunk_id, embedding_model, embedding) "
            "VALUES (:chunk, 'Qwen/Qwen3-Embedding-4B', CAST(:large AS halfvec(2560)))",
            {"chunk": chunk, "large": large},
        ),
    )

    _alembic("upgrade", UNDER_TEST, scratch_database)
    bits = await _run(
        scratch_database,
        (
            "SELECT embedding_bits::text FROM posting_chunk_vectors WHERE chunk_id = :c",
            {"c": chunk},
        ),
    )
    assert bits == "10" + "0" * 2558

    _alembic("downgrade", BEFORE, scratch_database)
    assert (
        await _run(
            scratch_database,
            (
                "SELECT count(*) FROM information_schema.columns "
                "WHERE table_name = 'posting_chunk_vectors' AND column_name = 'embedding_bits'",
                {},
            ),
        )
        == 0
    )
