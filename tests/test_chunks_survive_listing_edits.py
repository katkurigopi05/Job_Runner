"""A listing edit that leaves the description alone keeps the posting's chunks and vectors.

A chunk is current while its `content_hash` is the posting's, and that hash
covers the title, the location and the board's office facts as well as the
description. So a Workday listing that changed "2 Locations" to a city made
every chunk stale: the next pass deleted them, their Qwen vectors went with
them, and the same text went back to the GPU. On 2026-11-18, 114 open
postings held 1,915 vectors cut from a description that had not changed.

A chunk is cut from the description alone, so when the crawler stores an edit
whose normalized description is unchanged it moves the chunks to the new hash.
"""

from __future__ import annotations

import uuid

import numpy as np
from sqlalchemy import select

from packages.core.models import Company, Posting
from packages.core.models_chunks import LARGE_EMBEDDING_DIM, PostingChunk, PostingChunkVector
from packages.crawler.crawl import _store
from packages.crawler.extract import ExtractedPosting
from packages.matching.chunks import OVERLAP, normalize, spans

BODY = " ".join(f"Requirement {i}: build reliable data pipelines in Python." for i in range(24))
QWEN = "Qwen/Qwen3-Embedding-4B"


def _listing(*, location: str, content_hash: str, body: str = BODY) -> ExtractedPosting:
    return ExtractedPosting(
        ats_type="workday",
        external_id="R100",
        url="https://acme.wd1.myworkdayjobs.com/jobs/job/R100",
        title="Data Engineer",
        location=location,
        description_raw=body,
        content_hash=content_hash,
    )


async def _stored(session, location: str = "2 Locations", content_hash: str = "first"):
    company = Company(name=f"Acme {uuid.uuid4().hex[:6]}")
    session.add(company)
    await session.flush()
    listing = _listing(location=location, content_hash=content_hash)
    await _store(session, company, [listing], us_only=False, max_age_days=0)
    posting = await session.scalar(select(Posting).where(Posting.company_id == company.id))
    assert posting is not None
    text = normalize(BODY)
    for ordinal, (start, length) in enumerate(spans(text)):
        chunk = PostingChunk(
            posting_id=posting.id,
            ordinal=ordinal,
            start=start,
            length=length,
            content_hash=posting.content_hash,
            embedding_model="sentence-transformers",
            embedding=[0.0] * 384,
            overlap=OVERLAP,
        )
        session.add(chunk)
        await session.flush()
        session.add(
            PostingChunkVector(
                chunk_id=chunk.id,
                embedding_model=QWEN,
                embedding=np.ones(LARGE_EMBEDDING_DIM, dtype=np.float32) / LARGE_EMBEDDING_DIM**0.5,
            )
        )
    await session.flush()
    return company, posting


async def _chunks(session, posting) -> list[tuple[str | None, int]]:
    rows = await session.execute(
        select(PostingChunk.content_hash, PostingChunk.ordinal)
        .where(PostingChunk.posting_id == posting.id)
        .order_by(PostingChunk.ordinal)
    )
    return [(row.content_hash, row.ordinal) for row in rows]


async def _vectors(session, posting) -> int:
    return len(
        (
            await session.scalars(
                select(PostingChunkVector.chunk_id)
                .join(PostingChunk, PostingChunk.id == PostingChunkVector.chunk_id)
                .where(PostingChunk.posting_id == posting.id)
            )
        ).all()
    )


async def test_a_location_edit_moves_the_chunks_to_the_new_hash(db_session) -> None:
    company, posting = await _stored(db_session)
    before = await _vectors(db_session, posting)

    edit = _listing(location="San Jose, CA", content_hash="second")
    await _store(db_session, company, [edit], us_only=False, max_age_days=0)

    assert {content_hash for content_hash, _ in await _chunks(db_session, posting)} == {"second"}
    assert await _vectors(db_session, posting) == before > 0


async def test_a_whitespace_only_change_is_the_same_text(db_session) -> None:
    company, posting = await _stored(db_session)

    spaced = _listing(location="2 Locations", content_hash="second", body=BODY.replace(" ", "  "))
    await _store(db_session, company, [spaced], us_only=False, max_age_days=0)

    assert {content_hash for content_hash, _ in await _chunks(db_session, posting)} == {"second"}


async def test_a_description_edit_leaves_the_chunks_stale(db_session) -> None:
    company, posting = await _stored(db_session)

    edit = _listing(location="2 Locations", content_hash="second", body=BODY + " Kafka too.")
    await _store(db_session, company, [edit], us_only=False, max_age_days=0)

    assert {content_hash for content_hash, _ in await _chunks(db_session, posting)} == {"first"}


async def test_chunks_already_stale_stay_stale(db_session) -> None:
    """Chunks cut from an older version than the one being replaced say nothing about it."""
    company, posting = await _stored(db_session)
    await db_session.execute(
        PostingChunk.__table__.update()
        .where(PostingChunk.posting_id == posting.id)
        .values(content_hash="older")
    )

    edit = _listing(location="San Jose, CA", content_hash="second")
    await _store(db_session, company, [edit], us_only=False, max_age_days=0)

    assert {content_hash for content_hash, _ in await _chunks(db_session, posting)} == {"older"}


async def test_an_unchanged_listing_touches_no_chunk(db_session) -> None:
    company, posting = await _stored(db_session)

    again = _listing(location="2 Locations", content_hash="first")
    await _store(db_session, company, [again], us_only=False, max_age_days=0)

    assert {content_hash for content_hash, _ in await _chunks(db_session, posting)} == {"first"}


async def test_one_store_moves_only_the_postings_whose_text_is_unchanged(db_session) -> None:
    company, kept = await _stored(db_session)
    other = ExtractedPosting(
        ats_type="workday",
        external_id="R200",
        url="https://acme.wd1.myworkdayjobs.com/jobs/job/R200",
        title="Analytics Engineer",
        location="2 Locations",
        description_raw=BODY + " Second role.",
        content_hash="other-first",
    )
    await _store(db_session, company, [other], us_only=False, max_age_days=0)
    changed = await db_session.scalar(select(Posting).where(Posting.external_id == "R200"))
    assert changed is not None
    db_session.add(
        PostingChunk(
            posting_id=changed.id,
            ordinal=0,
            start=0,
            length=500,
            content_hash="other-first",
            embedding_model="sentence-transformers",
            embedding=[0.0] * 384,
            overlap=OVERLAP,
        )
    )
    await db_session.flush()

    moved = _listing(location="San Jose, CA", content_hash="second")
    rewritten = other.model_copy(
        update={"description_raw": BODY + " Kafka too.", "content_hash": "other-second"}
    )
    await _store(db_session, company, [moved, rewritten], us_only=False, max_age_days=0)

    assert {h for h, _ in await _chunks(db_session, kept)} == {"second"}
    assert {h for h, _ in await _chunks(db_session, changed)} == {"other-first"}


async def test_a_listing_that_had_no_text_moves_nothing(db_session) -> None:
    """The description backfill: the old row had no text, so no chunk was cut from it."""
    from packages.crawler.crawl import _same_description

    company = Company(name=f"Acme {uuid.uuid4().hex[:6]}")
    db_session.add(company)
    await db_session.flush()
    bare = _listing(location="2 Locations", content_hash="listed", body="")
    bare = bare.model_copy(update={"description_raw": None})
    await _store(db_session, company, [bare], us_only=False, max_age_days=0)
    posting = await db_session.scalar(select(Posting).where(Posting.company_id == company.id))
    assert posting is not None

    filled = _listing(location="2 Locations", content_hash="filled")
    assert await _same_description(db_session, {posting.id: ("listed", filled)}) == []


async def test_text_read_after_another_edit_is_not_trusted(db_session) -> None:
    """The chunks at the hash this crawl read were cut from text since replaced."""
    from packages.crawler.crawl import _same_description

    _, posting = await _stored(db_session)
    # Another worker stored a new hash after this crawl read "first".
    posting.content_hash = "someone-else"
    await db_session.flush()

    edit = _listing(location="San Jose, CA", content_hash="second")
    assert await _same_description(db_session, {posting.id: ("first", edit)}) == []
