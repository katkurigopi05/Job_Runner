"""The same text is embedded once: a posting repeating another's text takes its vectors.

On 2026-11-18, 1,502 open postings with Qwen vectors repeated another's
description word for word (Accenture's "AI Native Software Engineer" 49
times), so about 23,000 vectors had been made on a GPU for text already
embedded. The export now sends each text once, and `copy_vectors` gives a
posting the vectors of another whose current chunks cut the same text, with
no model. A vector still goes only onto the text it was made from: the
descriptions must be equal and every chunk must sit at the same offsets.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
from sqlalchemy import delete, select

from packages.core.models_chunks import PostingChunk, PostingChunkVector
from packages.matching import chunk_vectors as CV
from tests.test_chunk_vectors_import import BODY, _exported, _posting, _stored, _vectors

OTHER = BODY + " Kafka too."


async def _cover(session, posting, body: str = BODY, seed: int = 0) -> None:
    exported = [_exported(posting, body)]
    await CV.import_vectors(session, exported, _vectors(len(exported[0].chunks), seed=seed))


def _of(stored, posting) -> list[np.ndarray]:
    return [
        vector for (posting_id, _), vector in sorted(stored.items()) if posting_id == posting.id
    ]


async def test_a_posting_repeating_embedded_text_is_not_exported_but_copied(db_session) -> None:
    donor = await _posting(db_session)
    await _cover(db_session, donor)
    repeat = await _posting(db_session)

    exported, report = await CV.pending(db_session)

    assert exported == ()
    assert (report.postings, report.copyable) == (0, 1)

    copied = await CV.copy_vectors(db_session)

    assert (copied.postings, copied.chunks) == (1, len(_exported(donor).chunks))
    stored = await _stored(db_session)
    by_ordinal = {o: v for (p, o), v in stored.items() if p == donor.id}
    for (posting_id, ordinal), vector in stored.items():
        if posting_id == repeat.id:
            assert np.array_equal(vector, by_ordinal[ordinal])
    assert (await CV.pending(db_session))[0] == ()
    assert (await CV.copy_vectors(db_session)).postings == 0


async def test_a_batch_sends_each_text_once_and_the_import_fills_the_repeats(db_session) -> None:
    first = await _posting(db_session)
    first.first_seen_at = datetime.now(UTC) - timedelta(days=1)
    repeat = await _posting(db_session)
    other = await _posting(db_session, OTHER)
    await db_session.flush()

    exported, report = await CV.pending(db_session)

    assert [posting.id for posting in exported] == [str(first.id), str(other.id)]
    assert (report.postings, report.repeats, report.copyable) == (2, 1, 0)

    await CV.import_vectors(db_session, exported, _vectors(report.chunks))
    copied = await CV.copy_vectors(db_session)

    assert copied.postings == 1
    stored = await _stored(db_session)
    assert len(_of(stored, repeat)) == len(_of(stored, first))
    assert all(
        np.array_equal(a, b) for a, b in zip(_of(stored, repeat), _of(stored, first), strict=True)
    )
    assert (await CV.pending(db_session))[0] == ()


async def test_different_text_is_never_copied(db_session) -> None:
    donor = await _posting(db_session)
    await _cover(db_session, donor)
    different = await _posting(db_session, OTHER)

    assert (await CV.copy_vectors(db_session)).postings == 0
    assert _of(await _stored(db_session), different) == []


async def test_a_donor_must_be_current_and_fully_covered(db_session) -> None:
    stale = await _posting(db_session)
    await _cover(db_session, stale)
    await db_session.execute(
        PostingChunk.__table__.update()
        .where(PostingChunk.posting_id == stale.id)
        .values(content_hash="older")
    )
    partial = await _posting(db_session)
    await _cover(db_session, partial)
    first = await db_session.scalar(
        select(PostingChunk.id).where(
            PostingChunk.posting_id == partial.id, PostingChunk.ordinal == 0
        )
    )
    await db_session.execute(delete(PostingChunkVector).where(PostingChunkVector.chunk_id == first))
    await _posting(db_session)

    # The partly covered posting is itself a target now, and nothing can donate.
    assert (await CV.copy_vectors(db_session)).postings == 0


async def test_chunks_at_other_offsets_are_not_copied_onto(db_session) -> None:
    donor = await _posting(db_session)
    await _cover(db_session, donor)
    moved = await _posting(db_session)
    await db_session.execute(
        PostingChunk.__table__.update()
        .where(PostingChunk.posting_id == moved.id, PostingChunk.ordinal == 1)
        .values(start=PostingChunk.start + 1)
    )

    assert (await CV.copy_vectors(db_session)).postings == 0
    assert _of(await _stored(db_session), moved) == []


async def test_a_dry_run_counts_and_writes_nothing(db_session) -> None:
    donor = await _posting(db_session)
    await _cover(db_session, donor)
    repeat = await _posting(db_session)

    report = await CV.copy_vectors(db_session, dry_run=True)

    assert (report.postings, report.chunks) == (1, len(_exported(donor).chunks))
    assert _of(await _stored(db_session), repeat) == []


async def test_a_scope_copies_only_what_it_would_export(db_session) -> None:
    donor = await _posting(db_session)
    await _cover(db_session, donor)
    austin = await _posting(db_session)
    austin.location = "Austin, TX"
    await db_session.flush()

    assert (await CV.copy_vectors(db_session, scope=CV.OWNER_AREA)).postings == 0
    assert (await CV.copy_vectors(db_session)).postings == 1


async def test_a_closed_posting_takes_no_copy(db_session) -> None:
    donor = await _posting(db_session)
    await _cover(db_session, donor)
    closed = await _posting(db_session)
    closed.closed_at = datetime.now(UTC)
    await db_session.flush()

    assert (await CV.copy_vectors(db_session)).postings == 0
