"""Qwen chunk vectors made elsewhere are attached only to the chunks they were made from.

The owner embedded every chunk of the index with Qwen3-Embedding-4B on a Colab
TPU on 2026-11-02 (224,361 chunks, 27 minutes), from an export of the posting
text. Between the export and the import a posting can change, close, or lose
its chunks, and a vector attached to the wrong text would rank a posting for
words it no longer says. So a posting's vectors are imported only when its
current chunks are the exported text, exactly, and otherwise it is skipped
and counted.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import numpy as np
import pytest
from sqlalchemy import delete, func, select

from packages.core.models import Company, Posting
from packages.core.models_chunks import LARGE_EMBEDDING_DIM, PostingChunk, PostingChunkVector
from packages.matching import chunk_vectors as CV
from packages.matching.chunks import OVERLAP, normalize, spans

#: Long enough for three chunks of 500 characters overlapping by 25.
BODY = " ".join(f"Requirement {i}: build reliable data pipelines in Python." for i in range(24))
#: How many chunks `BODY` is cut into at today's overlap.
CHUNKS = len(spans(normalize(BODY)))


async def _posting(session, body: str = BODY, *, chunked: bool = True) -> Posting:
    company = Company(name="Acme " + uuid.uuid4().hex[:8], ats_type="greenhouse")
    session.add(company)
    await session.flush()
    posting = Posting(
        company_id=company.id,
        title="Data Engineer",
        url="https://jobs.example.test/" + uuid.uuid4().hex,
        description_raw=body,
        content_hash=uuid.uuid4().hex,
        first_seen_at=datetime.now(UTC),
    )
    session.add(posting)
    await session.flush()
    if chunked:
        text = normalize(body)
        for ordinal, (start, length) in enumerate(spans(text)):
            session.add(
                PostingChunk(
                    posting_id=posting.id,
                    ordinal=ordinal,
                    start=start,
                    length=length,
                    content_hash=posting.content_hash,
                    embedding_model="sentence-transformers",
                    embedding=[0.0] * 384,
                    overlap=OVERLAP,
                )
            )
        await session.flush()
    return posting


def _exported(posting: Posting, body: str = BODY) -> CV.Exported:
    text = normalize(body)
    return CV.Exported(id=str(posting.id), chunks=tuple(text[a : a + n] for a, n in spans(text)))


def _vectors(rows: int, seed: int = 0) -> np.ndarray:
    block = np.random.default_rng(seed).standard_normal((rows, LARGE_EMBEDDING_DIM))
    return (block / np.linalg.norm(block, axis=1, keepdims=True)).astype(np.float16)


async def _stored(session) -> dict[tuple[uuid.UUID, int], np.ndarray]:
    rows = await session.execute(
        select(PostingChunk.posting_id, PostingChunk.ordinal, PostingChunkVector.embedding)
        .join(PostingChunkVector, PostingChunkVector.chunk_id == PostingChunk.id)
        .where(PostingChunkVector.embedding_model == CV.QWEN_4B)
    )
    return {
        (posting_id, ordinal): np.asarray(
            embedding.to_numpy() if hasattr(embedding, "to_numpy") else embedding, dtype=np.float32
        )
        for posting_id, ordinal, embedding in rows
    }


async def test_a_posting_whose_chunks_match_gets_its_vectors_in_order(db_session):
    posting = await _posting(db_session)
    exported = [_exported(posting)]
    vectors = _vectors(len(exported[0].chunks))

    report = await CV.import_vectors(db_session, exported, vectors)

    assert report.imported == 1 and report.chunks_imported == len(exported[0].chunks)
    stored = await _stored(db_session)
    for ordinal in range(len(exported[0].chunks)):
        assert np.allclose(
            stored[(posting.id, ordinal)], vectors[ordinal].astype(np.float32), atol=1e-3
        )


async def test_postings_that_moved_on_since_the_export_are_skipped_and_counted(db_session):
    matching = await _posting(db_session)
    edited = await _posting(db_session)  # the text changed after the export
    stale = await _posting(db_session)  # its chunks were cut from an older text
    await db_session.execute(
        PostingChunk.__table__.update()
        .where(PostingChunk.posting_id == stale.id)
        .values(content_hash="older")
    )
    closed = await _posting(db_session)
    closed.closed_at = datetime.now(UTC)
    unchunked = await _posting(db_session, chunked=False)
    await db_session.flush()
    exported = [
        _exported(matching),
        _exported(edited, BODY.replace("Python", "Rust")),
        _exported(stale),
        _exported(closed),
        _exported(unchunked),
        CV.Exported(id=str(uuid.uuid4()), chunks=("a posting that is gone",)),
    ]
    vectors = _vectors(sum(len(e.chunks) for e in exported))

    report = await CV.import_vectors(db_session, exported, vectors)

    assert (report.imported, report.changed, report.not_chunked, report.absent) == (1, 2, 1, 2)
    assert {posting_id for posting_id, _ in await _stored(db_session)} == {matching.id}


async def test_running_it_again_adds_nothing(db_session):
    posting = await _posting(db_session)
    exported = [_exported(posting)]
    vectors = _vectors(len(exported[0].chunks))
    await CV.import_vectors(db_session, exported, vectors)

    again = await CV.import_vectors(db_session, exported, vectors)

    assert again.chunks_imported == 0 and again.chunks_present == len(exported[0].chunks)
    count = await db_session.scalar(select(func.count()).select_from(PostingChunkVector))
    assert count == len(exported[0].chunks)


async def test_a_dry_run_counts_and_writes_nothing(db_session):
    posting = await _posting(db_session)
    exported = [_exported(posting)]

    report = await CV.import_vectors(
        db_session, exported, _vectors(len(exported[0].chunks)), dry_run=True
    )

    assert report.imported == 1 and report.chunks_imported == len(exported[0].chunks)
    assert await _stored(db_session) == {}


@pytest.mark.parametrize(
    ("rows", "width"),
    [
        (CHUNKS, LARGE_EMBEDDING_DIM - 1),
        (CHUNKS - 1, LARGE_EMBEDDING_DIM),
        (CHUNKS + 1, LARGE_EMBEDDING_DIM),
    ],
)
async def test_a_vectors_file_that_does_not_fit_the_export_is_refused(db_session, rows, width):
    posting = await _posting(db_session)
    exported = [_exported(posting)]
    assert len(exported[0].chunks) == CHUNKS

    with pytest.raises(ValueError):
        await CV.import_vectors(db_session, exported, np.zeros((rows, width), dtype=np.float16))


async def test_a_vector_that_is_not_finite_is_refused(db_session):
    posting = await _posting(db_session)
    exported = [_exported(posting)]
    vectors = _vectors(CHUNKS)
    vectors[1, 7] = np.nan

    with pytest.raises(ValueError, match="finite"):
        await CV.import_vectors(db_session, exported, vectors)


async def test_a_chunk_takes_its_large_vector_with_it(db_session):
    posting = await _posting(db_session)
    exported = [_exported(posting)]
    await CV.import_vectors(db_session, exported, _vectors(CHUNKS))

    await db_session.execute(delete(PostingChunk).where(PostingChunk.posting_id == posting.id))

    assert await db_session.scalar(select(func.count()).select_from(PostingChunkVector)) == 0


def test_the_export_file_is_read_in_its_own_order(tmp_path):
    import gzip
    import json

    path = tmp_path / "whole_index.json.gz"
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        json.dump(
            {
                "postings": [
                    {"id": "a", "title": "T", "company": "C", "chunks": ["one", "two"]},
                    {"id": "b", "title": "T", "company": "C", "chunks": ["three"]},
                ],
                "questions": [],
            },
            handle,
        )

    assert CV.load_export(path) == (
        CV.Exported(id="a", chunks=("one", "two")),
        CV.Exported(id="b", chunks=("three",)),
    )
