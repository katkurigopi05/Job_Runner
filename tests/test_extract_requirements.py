"""Requirement backfills must not overwrite a crawler that won the race."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import event, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.sql.dml import Update

from packages.core import db as core_db
from packages.core.models import Posting
from packages.matching.requirements import EXTRACTOR_VERSION, extract
from scripts import extract_requirements as backfill

BODY = "Requirements\n- Python\nSalary: $120,000-$150,000 per year."
CRAWLED = {"crawler": "reading must survive"}
CRAWLED_AT = datetime(2026, 11, 19, tzinfo=UTC)


async def _posting(session, *, version=2, content_hash="original", ident=None):
    posting = Posting(
        id=ident or uuid.uuid4(),
        url=f"https://example.test/jobs/{uuid.uuid4().hex}",
        description_raw=BODY,
        content_hash=content_hash,
        requirements_json={"old": True},
        requirements_version=version,
    )
    session.add(posting)
    await session.commit()
    return posting


@pytest.mark.parametrize("race", ["changed_text", "current_reading"])
async def test_run_preserves_crawler_reading_before_write(
    worker_session, committing_sessionmaker, monkeypatch, race
):
    """Runs against the original script too: intervene at its actual SQL write."""
    posting = await _posting(worker_session)
    raced = False

    class RacingSession(AsyncSession):
        async def execute(self, statement, *args, **kwargs):
            nonlocal raced
            if isinstance(statement, Update) and not raced:
                raced = True
                fields = {
                    "requirements_json": CRAWLED,
                    "requirements_extracted_at": CRAWLED_AT,
                    "salary_min": 99,
                    "salary_max": 101,
                    "salary_currency": "EUR",
                    "salary_period": "hour",
                }
                if race == "changed_text":
                    # Leave it stale to prove the hash guard independently.
                    fields.update(content_hash="changed", description_raw="Requirements\n- Go")
                else:
                    # Keep the hash to prove the version guard independently.
                    fields.update(requirements_version=EXTRACTOR_VERSION)
                async with committing_sessionmaker() as crawler:
                    await crawler.execute(
                        update(Posting).where(Posting.id == posting.id).values(**fields)
                    )
                    await crawler.commit()
            return await super().execute(statement, *args, **kwargs)

    maker = async_sessionmaker(worker_session.bind, class_=RacingSession)
    monkeypatch.setattr(core_db, "get_sessionmaker", lambda: maker)
    report = await backfill.run(limit=1)
    assert raced
    await worker_session.refresh(posting)
    assert posting.requirements_json == CRAWLED
    assert posting.requirements_extracted_at == CRAWLED_AT
    assert (posting.salary_min, posting.salary_max) == (99, 101)
    assert (posting.salary_currency, posting.salary_period) == ("EUR", "hour")
    assert posting.requirements_version == (2 if race == "changed_text" else EXTRACTOR_VERSION)
    assert "0 postings extracted" in report
    assert "1 skipped" in report


@pytest.mark.parametrize("version", [None, 2, 3, 4])
async def test_stale_snapshot_is_written_at_current_version(worker_session, version):
    posting = await _posting(worker_session, version=version)
    rows = await backfill.read_batch(worker_session, limit=500)
    assert [row.posting_id for row in rows] == [posting.id]
    assert rows[0].content_hash == "original"
    result = await backfill.write_batch(worker_session, rows)
    await worker_session.commit()
    await worker_session.refresh(posting)
    assert (result.extracted, result.skipped, result.with_pay) == (1, 0, 1)
    assert posting.requirements_version == EXTRACTOR_VERSION
    assert posting.requirements_json == extract(BODY).as_json()
    assert posting.requirements_extracted_at is not None
    assert (posting.salary_min, posting.salary_max) == (120000, 150000)
    assert posting.content_hash == "original"


async def test_current_and_future_versions_are_never_selected(worker_session):
    await _posting(worker_session, version=EXTRACTOR_VERSION)
    await _posting(worker_session, version=EXTRACTOR_VERSION + 1)
    assert await backfill.read_batch(worker_session, limit=500) == []


@pytest.mark.parametrize("new_hash", [None, "changed"])
async def test_read_and_write_allow_crawler_commit_between_them(
    worker_session, committing_sessionmaker, new_hash
):
    posting = await _posting(worker_session, content_hash=None if new_hash else "original")
    rows = await backfill.read_batch(worker_session, limit=500)
    async with committing_sessionmaker() as crawler:
        await crawler.execute(
            update(Posting)
            .where(Posting.id == posting.id)
            .values(content_hash=new_hash, requirements_json=CRAWLED)
        )
        await crawler.commit()
    result = await backfill.write_batch(worker_session, rows)
    await worker_session.commit()
    await worker_session.refresh(posting)
    assert (result.extracted, result.skipped) == (0, 1)
    assert posting.requirements_json == CRAWLED


async def test_unchanged_null_hash_is_safe_and_second_run_changes_nothing(
    worker_session, committing_sessionmaker, monkeypatch
):
    posting = await _posting(worker_session, content_hash=None)
    monkeypatch.setattr(core_db, "get_sessionmaker", lambda: committing_sessionmaker)
    assert "1 postings extracted" in await backfill.run()
    await worker_session.refresh(posting)
    before = (posting.requirements_json, posting.requirements_extracted_at)
    assert await backfill.read_batch(worker_session, limit=500) == []
    assert "0 postings extracted" in await backfill.run()
    await worker_session.refresh(posting)
    assert (posting.requirements_json, posting.requirements_extracted_at) == before


async def test_ordered_batches_move_past_skips_until_next_run(
    worker_session, committing_sessionmaker, monkeypatch
):
    postings = [await _posting(worker_session, ident=uuid.UUID(int=i)) for i in [3, 1, 2]]
    original_read = backfill.read_batch
    batches = []

    async def read(session, **kwargs):
        rows = await original_read(session, **kwargs)
        batches.append([row.posting_id.int for row in rows])
        if len(batches) == 1:
            async with committing_sessionmaker() as crawler:
                await crawler.execute(
                    update(Posting).where(Posting.id == uuid.UUID(int=1)).values(content_hash="new")
                )
                await crawler.commit()
        return rows

    monkeypatch.setattr(backfill, "read_batch", read)
    monkeypatch.setattr(core_db, "get_sessionmaker", lambda: committing_sessionmaker)
    report = await backfill.run(batch_size=2)
    assert batches == [[1, 2], [3], []]
    assert "2 postings extracted" in report and "1 skipped" in report
    assert "2 state pay" in report
    monkeypatch.setattr(backfill, "read_batch", original_read)
    assert "1 postings extracted" in await backfill.run(batch_size=2)
    assert "0 postings extracted" in await backfill.run(batch_size=2)
    for posting in postings:
        await worker_session.refresh(posting)
        assert posting.requirements_version == EXTRACTOR_VERSION


async def test_large_write_is_split_below_asyncpg_parameter_limit(worker_session):
    # Enough rows to exceed 32,767 parameters if one VALUES table is used.
    table = Posting.__table__
    await worker_session.execute(
        table.insert(),
        [
            {
                "id": uuid.UUID(int=i + 1),
                "url": f"https://example.test/{i}",
                "requirements_version": 2,
                "description_raw": BODY if i < 5000 else None,
                "content_hash": "original" if i < 5000 else None,
            }
            for i in range(5500)
        ],
    )
    rows = await backfill.read_batch(worker_session, limit=5500)
    parameters = []

    def observe(conn, cursor, statement, params, context, executemany):
        if statement.startswith("UPDATE"):
            parameters.append(len(params))

    connection = await worker_session.connection()
    event.listen(connection.sync_connection, "before_cursor_execute", observe)
    try:
        result = await backfill.write_batch(worker_session, rows)
    finally:
        event.remove(connection.sync_connection, "before_cursor_execute", observe)
    assert (result.extracted, result.with_pay) == (5500, 5000)
    assert len(parameters) > 1
    assert max(parameters) <= 32767
    assert not (
        await worker_session.scalars(
            select(Posting.id).where(Posting.requirements_version < EXTRACTOR_VERSION)
        )
    ).all()
