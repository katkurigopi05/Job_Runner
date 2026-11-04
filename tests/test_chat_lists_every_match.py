"""A reply lists every posting it found, not the first hundred.

The owner asked for software jobs on 2026-11-02 and said the list "should
show every single role". A reply carried the five the model read and at most
100 more, so 892 software titles stopped at 105 with nothing past them, and a
search by mention listed the 30 it ranked out of 1,619 it counted. Each list
now carries everything its count covers, in the order the search ranked it.
"""

from __future__ import annotations

import pytest

from packages.core.config import get_settings
from packages.matching import retrieve as R
from tests.test_chat_title_words import _company, _posting

#: More than the 100 a reply used to list after the five shown.
MANY = 130


@pytest.fixture(autouse=True)
def setting(monkeypatch):
    monkeypatch.setattr(R, "get_reranker", lambda: None)
    monkeypatch.setenv("SEARCH_US_ONLY", "true")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def _many(session, title: str, body: str = "A role.", count: int = MANY) -> set:
    company = await _company(session, "Acme")
    ids = set()
    for index in range(count):
        posting = await _posting(session, company, f"{title} {index}", body)
        posting.location = "San Francisco, CA"
        ids.add(posting.id)
    await session.flush()
    return ids


def _listed(found) -> list:
    return [p.posting_id for p in (*found.passages, *found.more)]


async def test_a_kind_of_job_read_from_titles_lists_every_title(db_session):
    ids = await _many(db_session, "Software Engineer")

    found = await R.retrieve(db_session, "software jobs")

    assert found.title_total == MANY
    assert len(found.passages) == 5
    assert set(_listed(found)) == ids
    assert len(_listed(found)) == MANY, "each posting once"


async def test_a_role_lists_every_posting_of_the_role(db_session):
    ids = await _many(db_session, "Data Scientist")

    found = await R.retrieve(db_session, "data scientist roles")

    assert found.role_total == MANY
    assert set(_listed(found)) == ids


async def test_a_search_by_mention_lists_every_posting_it_counted(db_session):
    said = await _many(db_session, "Platform Specialist", "Kafka.")
    titled = await _many(db_session, "Kafka Platform Engineer", "Streams.", count=3)

    found = await R.retrieve(db_session, "jobs using Kafka")

    assert found.match_total == MANY + 3
    listed = _listed(found)
    assert set(listed) == said | titled
    assert len(listed) == MANY + 3, "each posting once"
    # The search's own order comes first: a title that says it counts twice.
    assert set(listed[:3]) == titled


async def test_an_employer_lists_every_open_posting(db_session):
    ids = await _many(db_session, "Account Executive")

    found = await R.retrieve(db_session, "jobs at Acme")

    assert found.match_total == MANY
    assert set(_listed(found)) == ids
