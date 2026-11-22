"""`fd52bc47f90b` drops bge-small's chunk vectors and keeps the chunks and their Qwen vectors.

The chunk rows are what every Qwen vector belongs to, and deleting one deletes
its vectors (cascade), so the migration must take the columns and nothing
else. The downgrade cannot bring the vectors back; what it must not do is
leave the old chunker believing every chunk is stale, which would cut every
posting again and delete the Qwen vectors that way. Run through Alembic on a
throwaway database, as `test_migration_posting_chunk_vectors` does.
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine

from tests.test_migration_host_delay_floor import _alembic
from tests.test_migration_posting_dedupe import scratch_database  # noqa: F401

BEFORE = "68e2a1b945d0"
UNDER_TEST = "fd52bc47f90b"
QWEN = "Qwen/Qwen3-Embedding-4B"


async def _scalar(url: str, query: str, **params) -> object:
    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            return (await conn.execute(sa.text(query), params)).scalar()
    finally:
        await engine.dispose()


async def _columns(url: str) -> set[str]:
    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            rows = await conn.execute(
                sa.text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'posting_chunks'"
                )
            )
            return {row[0] for row in rows}
    finally:
        await engine.dispose()


async def _chunk_with_both_vectors(url: str) -> uuid.UUID:
    """One chunk as the owner's database holds them before: a bge-small and a Qwen vector."""
    company, posting, chunk = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
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
                    "content_hash, overlap, embedding_model, embedding) VALUES (:id, :posting, "
                    "0, 0, 10, 'h1', 125, 'sentence-transformers', CAST(:small AS halfvec(384)))"
                ),
                {"id": chunk, "posting": posting, "small": "[" + ",".join(["0.1"] * 384) + "]"},
            )
            await conn.execute(
                sa.text(
                    "INSERT INTO posting_chunk_vectors (chunk_id, embedding_model, embedding) "
                    "VALUES (:chunk, :model, CAST(:large AS halfvec(2560)))"
                ),
                {"chunk": chunk, "model": QWEN, "large": "[" + ",".join(["0.01"] * 2560) + "]"},
            )
    finally:
        await engine.dispose()
    return chunk


async def test_the_bge_vectors_go_and_the_chunks_keep_their_qwen_vectors(scratch_database):  # noqa: F811
    _alembic("upgrade", BEFORE, scratch_database)
    chunk = await _chunk_with_both_vectors(scratch_database)

    _alembic("upgrade", UNDER_TEST, scratch_database)

    assert not {"embedding", "embedding_model"} & await _columns(scratch_database)
    assert (
        await _scalar(scratch_database, "SELECT to_regclass('ix_posting_chunks_embedding_hnsw')")
        is None
    )
    assert (
        await _scalar(
            scratch_database,
            "SELECT (start, length, content_hash, overlap)::text FROM posting_chunks "
            "WHERE id = :id",
            id=chunk,
        )
        == "(0,10,h1,125)"
    )
    assert (
        await _scalar(
            scratch_database,
            "SELECT count(*) FROM posting_chunk_vectors "
            "WHERE chunk_id = :id AND embedding_model = :m",
            id=chunk,
            m=QWEN,
        )
        == 1
    )


async def test_the_downgrade_stamps_chunks_so_the_old_chunker_keeps_them(scratch_database):  # noqa: F811
    """No vector comes back, but every chunk reads as bge-small's and current."""
    _alembic("upgrade", BEFORE, scratch_database)
    chunk = await _chunk_with_both_vectors(scratch_database)
    _alembic("upgrade", UNDER_TEST, scratch_database)

    _alembic("downgrade", BEFORE, scratch_database)

    assert {"embedding", "embedding_model"} <= await _columns(scratch_database)
    assert (
        await _scalar(
            scratch_database,
            "SELECT embedding_model FROM posting_chunks WHERE id = :id",
            id=chunk,
        )
        == "sentence-transformers"
    )
    assert (
        await _scalar(
            scratch_database,
            "SELECT embedding IS NULL FROM posting_chunks WHERE id = :id",
            id=chunk,
        )
        is True
    )
    assert await _scalar(scratch_database, "SELECT to_regclass('ix_posting_chunks_embedding_hnsw')")
    assert (
        await _scalar(
            scratch_database,
            "SELECT count(*) FROM posting_chunk_vectors WHERE chunk_id = :id",
            id=chunk,
        )
        == 1
    )
    _alembic("upgrade", "head", scratch_database)
