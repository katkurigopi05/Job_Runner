"""New postings get Qwen vectors through the same export → Colab → import path.

The whole index was embedded once on a Colab TPU (2026-11-02). Every crawl adds
postings after that, and until they have Qwen vectors the assistant searches
them with bge-small. `make export-chunk-vectors-pending` writes out exactly the
postings whose current chunks are missing a Qwen vector, in the format the
importer already checks against today's text.
"""

from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import pytest
from sqlalchemy import delete, select

from packages.core.models_chunks import PostingChunk, PostingChunkVector
from packages.matching import chunk_vectors as CV
from tests.test_chunk_vectors_import import BODY, _exported, _posting, _stored, _vectors


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


async def _at(session, location: str | None, *, chunked: bool = True):
    posting = await _posting(session, chunked=chunked)
    posting.location = location
    await session.flush()
    return posting


async def test_a_scope_exports_only_california_and_us_remote(db_session):
    # Arrange
    kept = [
        await _at(db_session, "San Francisco, CA"),
        await _at(db_session, "Hybrid - Oakland, CA"),
        await _at(db_session, "Remote - US"),
        await _at(db_session, "2 Locations"),
    ]
    for location in ("Austin, TX", "PH, Taguig", "Toronto, ON"):
        await _at(db_session, location)
    await _at(db_session, "Seattle, WA", chunked=False)
    await _at(db_session, "San Jose, CA", chunked=False)

    # Act
    exported, report = await CV.pending(db_session, scope=CV.OWNER_AREA)

    # Assert: the unchunked Seattle job is out of scope, San Jose waits for chunking.
    assert {posting.id for posting in exported} == {str(posting.id) for posting in kept}
    assert (report.postings, report.not_chunked, report.out_of_scope) == (4, 1, 4)


async def test_no_scope_exports_every_posting_as_before(db_session):
    for location in ("San Francisco, CA", "Austin, TX", "2 Locations"):
        await _at(db_session, location)

    exported, report = await CV.pending(db_session)

    assert (len(exported), report.out_of_scope) == (3, 0)


async def test_export_then_import_leaves_nothing_pending(db_session, tmp_path):
    first, second = await _posting(db_session), await _posting(db_session, BODY + " Kafka too.")
    exported, report = await CV.pending(db_session)
    path = tmp_path / "pending.json.gz"
    CV.write_export(path, exported)

    imported = await CV.import_vectors(db_session, CV.load_export(path), _vectors(report.chunks))

    assert imported.imported == 2 and imported.chunks_imported == report.chunks
    assert (await CV.pending(db_session))[0] == ()
    assert {e.id for e in exported} == {str(first.id), str(second.id)}


async def test_calibration_postings_come_first_and_are_not_counted_as_new(db_session):
    covered = await _posting(db_session)
    await _cover(db_session, covered)
    new = await _posting(db_session)

    exported, report = await CV.pending(db_session, calibrate=3)

    assert [posting.id for posting in exported] == [str(covered.id), str(new.id)]
    assert (report.postings, report.calibration, report.chunks) == (1, 1, len(exported[1].chunks))


async def test_a_batch_that_agrees_with_the_stored_vectors_is_imported(db_session):
    # Arrange: the covered posting's vectors are the seed-0 rows `_cover` stored.
    covered = await _posting(db_session)
    await _cover(db_session, covered)
    new = await _posting(db_session, BODY + " Kafka too.")
    exported, _ = await CV.pending(db_session, calibrate=1)
    made = np.concatenate(
        [_vectors(len(exported[0].chunks)), _vectors(len(exported[1].chunks), seed=1)]
    )

    # Act
    report = await CV.import_vectors(db_session, exported, made)

    # Assert
    assert report.agreement_chunks == len(exported[0].chunks)
    assert report.agreement_min == pytest.approx(1.0, abs=1e-3)
    assert report.imported == 2 and report.chunks_imported == len(exported[1].chunks)
    assert new.id in {posting_id for posting_id, _ in await _stored(db_session)}


async def test_a_batch_that_disagrees_writes_nothing(db_session):
    covered = await _posting(db_session)
    await _cover(db_session, covered)
    new = await _posting(db_session, BODY + " Kafka too.")
    exported, _ = await CV.pending(db_session, calibrate=1)
    made = _vectors(sum(len(posting.chunks) for posting in exported), seed=7)

    with pytest.raises(CV.DisagreementError):
        await CV.import_vectors(db_session, exported, made)

    assert new.id not in {posting_id for posting_id, _ in await _stored(db_session)}
