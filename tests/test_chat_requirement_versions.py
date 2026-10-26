"""Selected chat evidence uses current readings without rewriting stored postings."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from apps.api.routers.chat import _requirements_of
from packages.core.models import Posting
from packages.matching.requirements import EXTRACTOR_VERSION, extract
from packages.matching.retrieve import Passage, Retrieval


async def _posting(session, *, reading, version, description) -> Posting:
    posting = Posting(
        url=f"https://example.test/jobs/{uuid.uuid4().hex}",
        title="Platform Engineer",
        description_raw=description,
        requirements_json=reading,
        requirements_version=version,
    )
    session.add(posting)
    await session.flush()
    return posting


def _found(*postings: Posting) -> Retrieval:
    return Retrieval(
        attempted=True,
        passages=tuple(
            Passage(
                label=f"P{index}",
                posting_id=posting.id,
                title=posting.title,
                company=None,
                location=None,
                url=posting.url,
                excerpt="An excerpt does not replace the full requirements reading.",
                application_status=None,
            )
            for index, posting in enumerate(postings, 1)
        ),
    )


async def test_current_selected_requirements_are_used_unchanged(db_session) -> None:
    stored = extract("Requirements\n- Experience with Kafka").as_json()
    posting = await _posting(
        db_session,
        reading=stored,
        version=EXTRACTOR_VERSION,
        description="Essential\n- PyTorch",
    )

    assert await _requirements_of(db_session, _found(posting)) == {posting.id: stored}


async def test_stale_selected_requirements_are_reread_without_writes(
    db_session, monkeypatch
) -> None:
    description = "Essential\n- Experience with PyTorch 2.0 or TensorFlow\n- Deploy PyTorch models"
    stored = {"version": 2, "skills": {"unclassified": [{"skill": "pytorch", "quote": "old"}]}}
    posting = await _posting(db_session, reading=stored, version=2, description=description)
    execute = db_session.execute
    seen = []

    async def read_only(statement, *args, **kwargs):
        assert statement.is_select, "Refreshing prompt evidence must never write a posting"
        seen.append(statement)
        return await execute(statement, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(db_session, "execute", read_only)
        reading = await _requirements_of(db_session, _found(posting))

    assert reading == {posting.id: extract(description).as_json()}
    assert seen
    persisted = (
        await db_session.execute(
            select(Posting.requirements_json, Posting.requirements_version).where(
                Posting.id == posting.id
            )
        )
    ).one()
    assert persisted == (stored, 2)


@pytest.mark.parametrize("description", [None, "", " \n "])
async def test_stale_reading_with_no_raw_text_stays_unknown(db_session, description) -> None:
    stored = {"version": 2, "skills": {"required": [{"skill": "kafka", "quote": "old"}]}}
    posting = await _posting(db_session, reading=stored, version=2, description=description)

    assert await _requirements_of(db_session, _found(posting)) == {posting.id: None}


async def test_missing_selected_reading_is_read_from_available_raw_text(db_session) -> None:
    description = "Essential skills\n- PyTorch"
    posting = await _posting(db_session, reading=None, version=None, description=description)

    assert await _requirements_of(db_session, _found(posting)) == {
        posting.id: extract(description).as_json()
    }


async def test_raw_text_is_fetched_only_for_stale_or_missing_selected_ids(
    db_session, monkeypatch
) -> None:
    current = await _posting(
        db_session,
        reading=extract("Requirements\n- Kafka").as_json(),
        version=EXTRACTOR_VERSION,
        description="Current selected body must not be fetched",
    )
    stale = await _posting(
        db_session, reading={"version": 2}, version=2, description="Essential\n- PyTorch"
    )
    missing = await _posting(
        db_session, reading=None, version=None, description="Requirements\n- Kafka"
    )
    await _posting(
        db_session, reading={"version": 2}, version=2, description="Unselected candidate body"
    )
    execute = db_session.execute
    raw_queries = []

    async def track(statement, *args, **kwargs):
        columns = {column.name for column in statement.selected_columns}
        if "description_raw" in columns:
            raw_queries.append(statement)
        return await execute(statement, *args, **kwargs)

    monkeypatch.setattr(db_session, "execute", track)
    await _requirements_of(db_session, _found(current, stale, missing))

    assert len(raw_queries) == 1
    assert {column.name for column in raw_queries[0].selected_columns} == {"id", "description_raw"}
    params = raw_queries[0].compile().params
    queried_ids = {
        value for values in params.values() if isinstance(values, list) for value in values
    }
    assert queried_ids == {stale.id, missing.id}


async def test_no_selected_passages_makes_no_requirements_query() -> None:
    session = AsyncMock()

    assert await _requirements_of(session, Retrieval(attempted=True)) == {}
    session.execute.assert_not_awaited()
