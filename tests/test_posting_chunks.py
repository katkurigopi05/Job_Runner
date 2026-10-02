"""Chunked search: the owner's DP-800 recipe, applied to job postings.

A posting's single vector covers its first 512 tokens; a skill named in the
requirements, which come last, is invisible to it. Chunks of 500 characters
overlapping by 25 make every part of a posting searchable. These tests use
the lexical embedder as the chunk model, which is deterministic; production
chunks with bge-small, which `chunk_embedder` requires.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from packages.core.models import Company, CorpusStats, Posting
from packages.core.models_chunks import PostingChunk
from packages.matching.chunks import CHUNK_SIZE, OVERLAP, chunk_postings, normalize, spans
from packages.matching.embed import LexicalEmbedder
from packages.matching.retrieve import retrieve

LEXICAL = LexicalEmbedder()
FILLER = "Our team values ownership and curiosity in everything we ship. " * 40  # ~2,500 chars


# --- where the chunks fall ---------------------------------------------------------


def test_chunks_are_500_characters_overlapping_by_25() -> None:
    body = "x" * 1_200
    cut = spans(body)

    assert cut == [(0, 500), (475, 500), (950, 250)]
    assert all(length <= CHUNK_SIZE for _, length in cut)
    for (start, length), (next_start, _) in zip(cut, cut[1:], strict=False):
        assert start + length - next_start == OVERLAP


def test_every_character_is_in_some_chunk() -> None:
    body = normalize(FILLER + " Build Kafka pipelines.")
    covered = set()
    for start, length in spans(body):
        covered.update(range(start, start + length))
    assert covered == set(range(len(body)))


def test_a_short_or_empty_text() -> None:
    assert spans("Short.") == [(0, 6)]
    assert spans("") == []


# --- the chunking pass -------------------------------------------------------------


async def _company(session) -> Company:
    company = Company(name="Acme", ats_type="greenhouse")
    session.add(company)
    await session.flush()
    return company


async def _posting(session, company, title, body, *, seen_days_ago=0, closed=False, hash_="h1"):
    posting = Posting(
        company_id=company.id,
        external_id=uuid.uuid4().hex[:8],
        url=f"https://x.test/{uuid.uuid4().hex[:8]}",
        title=title,
        description_raw=body,
        content_hash=hash_,
        first_seen_at=datetime.now(UTC) - timedelta(days=seen_days_ago),
        closed_at=datetime.now(UTC) if closed else None,
    )
    session.add(posting)
    await session.flush()
    return posting


async def _chunks(session, posting) -> list[PostingChunk]:
    rows = await session.scalars(
        select(PostingChunk)
        .where(PostingChunk.posting_id == posting.id)
        .order_by(PostingChunk.ordinal)
    )
    return list(rows.all())


async def test_open_postings_are_chunked_and_the_offsets_read_back(db_session) -> None:
    company = await _company(db_session)
    open_ = await _posting(db_session, company, "Data Engineer", FILLER + " Build Kafka pipelines.")
    await _posting(db_session, company, "Closed", FILLER, closed=True)

    assert await chunk_postings(db_session, embedder=LEXICAL) == 1

    chunks = await _chunks(db_session, open_)
    body = normalize(open_.description_raw)
    assert len(chunks) == len(spans(body))
    assert body[chunks[-1].start : chunks[-1].start + chunks[-1].length].endswith(
        "Kafka pipelines."
    )
    assert {c.embedding_model for c in chunks} == {LEXICAL.name}


async def test_chunking_twice_does_nothing_the_second_time(db_session) -> None:
    company = await _company(db_session)
    await _posting(db_session, company, "Data Engineer", FILLER)

    await chunk_postings(db_session, embedder=LEXICAL)
    assert await chunk_postings(db_session, embedder=LEXICAL) == 0


async def test_an_edited_posting_is_chunked_again(db_session) -> None:
    """Old offsets point into text that is no longer there."""
    company = await _company(db_session)
    posting = await _posting(db_session, company, "Data Engineer", FILLER)
    await chunk_postings(db_session, embedder=LEXICAL)

    posting.description_raw = "Now a short posting about Rust."
    posting.content_hash = "h2"
    await db_session.flush()
    assert await chunk_postings(db_session, embedder=LEXICAL) == 1

    chunks = await _chunks(db_session, posting)
    assert [(c.start, c.length, c.content_hash) for c in chunks] == [(0, 31, "h2")]


async def test_without_a_semantic_model_nothing_is_chunked(db_session) -> None:
    """The suite runs EMBEDDING_BACKEND=lexical, where chunk vectors would add nothing."""
    company = await _company(db_session)
    await _posting(db_session, company, "Data Engineer", FILLER)

    assert await chunk_postings(db_session) == 0
    assert await db_session.scalar(select(func.count()).select_from(PostingChunk)) == 0


async def test_deleting_a_posting_deletes_its_chunks(db_session) -> None:
    company = await _company(db_session)
    posting = await _posting(db_session, company, "Data Engineer", FILLER)
    await chunk_postings(db_session, embedder=LEXICAL)

    await db_session.delete(posting)
    await db_session.flush()

    assert await db_session.scalar(select(func.count()).select_from(PostingChunk)) == 0


# --- search reads the chunks -------------------------------------------------------


async def _common(session, *words: str) -> None:
    session.add(CorpusStats(revision=1, total_documents=100, counts_json=dict.fromkeys(words, 90)))
    await session.flush()


async def test_vector_search_finds_what_the_end_of_a_posting_says(db_session) -> None:
    """The whole point. The matching text is 2,500 characters in.

    No posting here has a single vector, so only chunks can answer, and the
    excerpt handed to the model is the chunk that matched, not the opening.
    """
    await _common(db_session, "data", "engineering")
    company = await _company(db_session)
    deep = await _posting(
        db_session, company, "Engineer", FILLER + " You will own data engineering for payments."
    )
    await _posting(db_session, company, "Chef", "Laminated doughs and early mornings.")
    await chunk_postings(db_session, embedder=LEXICAL)

    found = await retrieve(db_session, "any data engineering roles?")

    assert [p.posting_id for p in found.passages] == [deep.id]
    assert "data engineering for payments" in found.passages[0].excerpt
    assert found.passages[0].excerpt.startswith("…"), "the chunk, cut from the middle"
    assert (found.searched, found.unsearchable) == (2, 0), "both chunked, both reachable"


async def test_keyword_matches_are_ordered_by_their_best_chunk(db_session) -> None:
    """Equal keyword scores, so the vectors decide, and they read chunks.

    Each posting says "kafka" once as far as the keyword scan is concerned
    (presence, not count), so the keyword order is newest first: C, B, A.
    Their final chunks repeat it 3, 2 and 1 times, so the chunk order is A,
    B, C. Fused by rank, A ties C and B falls last: C, A, B. Without the
    chunks every posting takes the middle vector rank and the order stays
    C, B, A.
    """
    company = await _company(db_session)
    a = await _posting(db_session, company, "A", FILLER + " Kafka Kafka Kafka.", seen_days_ago=3)
    b = await _posting(db_session, company, "B", FILLER + " Kafka Kafka.", seen_days_ago=2)
    c = await _posting(db_session, company, "C", FILLER + " Kafka.", seen_days_ago=1)
    await chunk_postings(db_session, embedder=LEXICAL)

    found = await retrieve(db_session, "kafka roles")

    assert [p.posting_id for p in found.passages] == [c.id, a.id, b.id]


async def test_a_chunk_from_old_text_is_not_used(db_session) -> None:
    """A posting edited since it was chunked is searched as if unchunked."""
    await _common(db_session, "data", "engineering")
    company = await _company(db_session)
    posting = await _posting(db_session, company, "Engineer", FILLER + " data engineering.")
    await chunk_postings(db_session, embedder=LEXICAL)
    posting.content_hash = "edited"
    await db_session.flush()

    found = await retrieve(db_session, "any data engineering roles?")

    assert found.passages == ()
