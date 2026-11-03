"""Qwen discovery through a 1-bit shortlist, re-ranked exactly.

Graded on 2026-11-03, Qwen + semantic candidates was 2.9 s slower a question
than the shipped search against the owner's limit of 1.0 s, and 1.28 s of it
was one exact scan of the 1.3 GB `posting_chunk_vectors` table. Each vector's
sign bits (`bit(2560)`, 85 MB for the whole table) pick the nearest chunks by
Hamming distance; only those are scored with the full vectors. Measured on
2026-11-02: a shortlist of 500 chunks gave the exact top five on all 112
benchmark questions, at 0.02 s against 1.7 s.
"""

from __future__ import annotations

from sqlalchemy import event, select

from packages.core.models_chunks import PostingChunkVector
from packages.matching import chunk_search as CS
from packages.matching import retrieve as R
from packages.matching.chunk_vectors import QWEN_4B
from tests.test_qwen_search import posting, vector


async def _spread(session, n=8):
    """Postings whose Qwen chunks sit at known angles from the question."""
    return [(await posting(session, "Platform Builder", large=(1.0, i / 4)))[0] for i in range(n)]


async def _exact(session, **kw):
    return await CS.chunk_ranking(
        session, CS.ChunkSpace(QWEN_4B), vector(2560), companies=None, limit=10000, **kw
    )


async def test_the_bit_column_is_the_sign_of_each_value(db_session):
    _, chunk = await posting(db_session, "Platform Builder", large=(0.5, -0.5))

    bits = await db_session.scalar(
        select(PostingChunkVector.embedding_bits).where(PostingChunkVector.chunk_id == chunk.id)
    )

    text = "".join(bits.as_string().split())  # asyncpg's BitString
    assert text[:3] == "100" and set(text[2:]) == {"0"} and len(text) == 2560


async def test_a_shortlist_covering_every_chunk_is_the_exact_ranking(db_session):
    await _spread(db_session)
    exact, _ = await _exact(db_session, among=None)

    ranked, every = await CS.chunk_ranking(
        db_session,
        CS.ChunkSpace(QWEN_4B, ranking=CS.ShortlistRanking(vector(2560))),
        vector(2560),
        among=None,
        companies=None,
        limit=1,
    )

    assert ranked == exact and every


async def test_a_short_shortlist_widens_until_enough_postings_count(db_session, monkeypatch):
    monkeypatch.setattr(CS, "SHORTLIST_CHUNKS", 2)
    postings = await _spread(db_session)
    wanted = {postings[-1].id, postings[-2].id}  # the two farthest from the question
    exact_hits, _ = await R._nearest_kept(
        db_session,
        CS.ChunkSpace(QWEN_4B),
        vector(2560),
        companies=None,
        want=2,
        counts=lambda h: h.posting_id in wanted,
        window=1,
    )

    hits, _ = await R._nearest_kept(
        db_session,
        CS.ChunkSpace(QWEN_4B, ranking=CS.ShortlistRanking(vector(2560))),
        vector(2560),
        companies=None,
        want=2,
        counts=lambda h: h.posting_id in wanted,
        window=1,
    )

    assert {h.posting_id for h in hits} >= wanted
    assert [h.posting_id for h in hits if h.posting_id in wanted] == [
        h.posting_id for h in exact_hits if h.posting_id in wanted
    ]


async def test_named_postings_and_companies_are_scored_exactly(db_session, monkeypatch):
    monkeypatch.setattr(CS, "SHORTLIST_CHUNKS", 1)  # a shortlist could not hold them
    postings = await _spread(db_session)
    named = [postings[-1].id, postings[0].id]
    ranking = CS.ShortlistRanking(vector(2560))
    space = CS.ChunkSpace(QWEN_4B, ranking=ranking)

    among, _ = await CS.chunk_ranking(
        db_session, space, vector(2560), among=named, companies=None, limit=1
    )
    at, _ = await CS.chunk_ranking(
        db_session, space, vector(2560), among=None, companies=[postings[-1].company_id], limit=1
    )

    assert among == (await _exact(db_session, among=named))[0]
    assert [row[0] for row in at] == [postings[-1].id]


async def test_exact_distances_are_only_computed_for_listed_chunks(db_session):
    await _spread(db_session)
    statements: list[str] = []
    engine = db_session.bind.sync_engine

    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    space = CS.ChunkSpace(QWEN_4B, ranking=CS.ShortlistRanking(vector(2560)))
    event.listen(engine, "before_cursor_execute", record)
    try:
        for _ in range(3):  # stages of one question reuse the shortlist
            await CS.chunk_ranking(
                db_session, space, vector(2560), among=None, companies=None, limit=1
            )
    finally:
        event.remove(engine, "before_cursor_execute", record)

    exact = [s for s in statements if "posting_chunk_vectors.embedding <=>" in s]
    assert exact and all("= ANY" in s or " IN " in s for s in exact), "never a full exact scan"
    assert sum("<~>" in s for s in statements) == 1, "one shortlist per question"
