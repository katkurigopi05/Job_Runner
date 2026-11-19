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


async def test_a_repeat_whose_original_was_skipped_stays_pending(db_session) -> None:
    """The text was edited before the import: nothing is copied, and the repeat goes next time."""
    first = await _posting(db_session)
    repeat = await _posting(db_session)
    exported, _ = await CV.pending(db_session)
    first.content_hash = "edited since the export"
    await db_session.flush()

    report = await CV.import_vectors(db_session, exported, _vectors(len(exported[0].chunks)))

    assert report.changed == 1
    assert (await CV.copy_vectors(db_session)).postings == 0
    assert str(repeat.id) in {posting.id for posting in (await CV.pending(db_session))[0]}


async def test_the_oldest_donor_is_copied(db_session) -> None:
    older = await _posting(db_session)
    older.first_seen_at = datetime.now(UTC) - timedelta(days=2)
    newer = await _posting(db_session)
    await db_session.flush()
    await _cover(db_session, older, seed=1)
    await _cover(db_session, newer, seed=2)
    repeat = await _posting(db_session)

    await CV.copy_vectors(db_session)

    stored = await _stored(db_session)
    assert all(
        np.array_equal(a, b) for a, b in zip(_of(stored, repeat), _of(stored, older), strict=True)
    )


async def test_a_partly_covered_posting_keeps_its_own_vectors(db_session) -> None:
    donor = await _posting(db_session)
    await _cover(db_session, donor, seed=1)
    partial = await _posting(db_session)
    await _cover(db_session, partial, seed=2)
    first = await db_session.scalar(
        select(PostingChunk.id).where(
            PostingChunk.posting_id == partial.id, PostingChunk.ordinal == 0
        )
    )
    await db_session.execute(delete(PostingChunkVector).where(PostingChunkVector.chunk_id == first))
    own = _of(await _stored(db_session), partial)

    copied = await CV.copy_vectors(db_session)

    assert (copied.postings, copied.chunks) == (1, 1)
    after = _of(await _stored(db_session), partial)
    assert np.array_equal(after[0], _of(await _stored(db_session), donor)[0])
    assert all(np.array_equal(a, b) for a, b in zip(after[1:], own, strict=True))


async def test_the_write_checks_the_text_again(db_session, monkeypatch) -> None:
    """A pair chosen before a same-length edit must not be written."""
    from sqlalchemy import literal
    from sqlalchemy.dialects.postgresql import UUID

    donor = await _posting(db_session)
    await _cover(db_session, donor)
    edited_body = BODY.replace("Python", "Golang")
    assert len(edited_body) == len(BODY)
    edited = await _posting(db_session, edited_body)

    def stale_pair(model: str):  # noqa: ANN202 - the pair an earlier read chose
        return select(
            literal(edited.id, UUID(as_uuid=True)).label("target_id"),
            literal(donor.id, UUID(as_uuid=True)).label("donor_id"),
        )

    monkeypatch.setattr(CV, "_copy_pairs", stale_pair)

    copied = await CV.copy_vectors(db_session)

    assert copied.chunks == 0
    assert _of(await _stored(db_session), edited) == []


def test_an_export_records_its_scope(tmp_path) -> None:
    import gzip
    import json

    wide, narrow, old = tmp_path / "wide.json.gz", tmp_path / "narrow.json.gz", tmp_path / "old.gz"
    CV.write_export(wide, (), scope=CV.ALL_SCOPE)
    CV.write_export(narrow, ())
    with gzip.open(old, "wt", encoding="utf-8") as handle:
        json.dump({"postings": []}, handle)

    assert CV.export_scope(wide) == CV.ALL_SCOPE
    assert CV.export_scope(narrow) == CV.OWNER_AREA_SCOPE
    assert CV.export_scope(old) == CV.OWNER_AREA_SCOPE
