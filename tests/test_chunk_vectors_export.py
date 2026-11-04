"""New postings get Qwen vectors through the same export → Colab → import path.

The whole index was embedded once on a Colab TPU (2026-11-02). Every crawl adds
postings after that, and until they have Qwen vectors the assistant searches
them with bge-small. `make export-chunk-vectors-pending` writes out exactly the
postings whose current chunks are missing a Qwen vector, in the format the
importer already checks against today's text.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import delete, select

from packages.core.models_chunks import PostingChunk, PostingChunkVector
from packages.matching import chunk_vectors as CV
from tests.test_chunk_vectors_import import BODY, _exported, _posting, _vectors


async def _cover(session, posting) -> None:
    exported = [_exported(posting)]
    await CV.import_vectors(session, exported, _vectors(len(exported[0].chunks)))


async def test_a_posting_without_qwen_vectors_is_exported_with_its_current_chunks(db_session):
    posting = await _posting(db_session)

    exported, report = await CV.pending(db_session)

    assert exported == (_exported(posting),)
    assert (report.postings, report.chunks, report.covered, report.not_chunked) == (1, 3, 0, 0)


async def test_covered_closed_and_textless_postings_are_left_out(db_session):
    covered = await _posting(db_session)
    await _cover(db_session, covered)
    closed = await _posting(db_session)
    closed.closed_at = datetime.now(UTC)
    textless = await _posting(db_session, chunked=False)
    textless.description_raw = "   "
    await db_session.flush()

    exported, report = await CV.pending(db_session)

    assert exported == ()
    assert (report.postings, report.covered, report.not_chunked) == (0, 1, 0)


async def test_a_partly_covered_posting_is_exported_whole(db_session):
    posting = await _posting(db_session)
    await _cover(db_session, posting)
    first = await db_session.scalar(
        select(PostingChunk.id).where(
            PostingChunk.posting_id == posting.id, PostingChunk.ordinal == 0
        )
    )
    await db_session.execute(delete(PostingChunkVector).where(PostingChunkVector.chunk_id == first))

    exported, _ = await CV.pending(db_session)

    assert exported == (_exported(posting),)


async def test_postings_that_need_chunking_first_are_counted_not_exported(db_session):
    await _posting(db_session, chunked=False)
    stale = await _posting(db_session)
    await db_session.execute(
        PostingChunk.__table__.update()
        .where(PostingChunk.posting_id == stale.id)
        .values(content_hash="older")
    )

    exported, report = await CV.pending(db_session)

    assert exported == ()
    assert report.not_chunked == 2


async def test_export_then_import_leaves_nothing_pending(db_session, tmp_path):
    first, second = await _posting(db_session), await _posting(db_session, BODY + " Kafka too.")
    exported, report = await CV.pending(db_session)
    path = tmp_path / "pending.json.gz"
    CV.write_export(path, exported)

    imported = await CV.import_vectors(db_session, CV.load_export(path), _vectors(report.chunks))

    assert imported.imported == 2 and imported.chunks_imported == report.chunks
    assert (await CV.pending(db_session))[0] == ()
    assert {e.id for e in exported} == {str(first.id), str(second.id)}
