"""A required-skill question reads the saved skill links, not every posting again.

After the extractor moved to version 5, every stored reading from an older
version was re-extracted in memory for each such question: 13 s for Kafka and
81 s for Python on the live data (2026-11-18). `make skill-links` had already
read every posting with version 5, so a linked posting's answer is its link.
The classes and quotes are the ones `required_skills.demand_class` gives.
"""

from __future__ import annotations

from sqlalchemy import select

from packages.core.models import Posting
from packages.matching import retrieve as retrieve_module
from packages.matching import skill_links
from packages.matching.retrieve import retrieve
from tests.test_chat_title_words import _company
from tests.test_retrieval_required_skills import _post


async def _mixed(session):
    company = await _company(session, "Example Labs")
    direct = await _post(session, company, "Requirements\nKafka experience")
    await _post(session, company, "Requirements\nKafka or Kinesis")
    await _post(session, company, "Preferred\nKafka")
    await _post(session, company, "We use Kafka.")
    unread = await _post(session, company, "Kafka role")
    unread.description_raw = None
    unread.title = "Kafka Specialist"
    await _post(session, company, "Requirements\nKafka", location="London, UK")
    await session.flush()
    return direct


async def _link_all(session) -> None:
    ids = list((await session.scalars(select(Posting.id))).all())
    await skill_links.link_postings(session, ids)


def _counts(found):
    count = found.required_skill
    return (count.required, count.options, count.preferred, count.named, count.unread)


async def test_linked_postings_give_the_same_answer(db_session) -> None:
    direct = await _mixed(db_session)
    before = await retrieve(db_session, "jobs that require Kafka")
    await _link_all(db_session)

    after = await retrieve(db_session, "jobs that require Kafka")

    assert _counts(after) == _counts(before) == (1, 1, 1, 1, 1)
    assert [p.posting_id for p in after.passages] == [direct.id]
    assert [p.excerpt for p in after.passages] == [p.excerpt for p in before.passages]


async def test_linked_postings_are_not_read_again(db_session, monkeypatch) -> None:
    await _mixed(db_session)
    await _link_all(db_session)
    real = retrieve_module.fresh_requirements
    read_again: list[str] = []

    def recording(stored, version, description):  # noqa: ANN001, ANN202
        if description and description.strip():
            read_again.append(description)
        return real(stored, version, description)

    monkeypatch.setattr(retrieve_module, "fresh_requirements", recording)

    found = await retrieve(db_session, "jobs that require Kafka")

    assert _counts(found) == (1, 1, 1, 1, 1)
    assert read_again == []


async def test_a_link_from_older_text_is_not_trusted(db_session, monkeypatch) -> None:
    direct = await _mixed(db_session)
    await _link_all(db_session)
    direct.content_hash = "edited since it was linked"
    await db_session.flush()
    real = retrieve_module.fresh_requirements
    read_again: list[str] = []

    def recording(stored, version, description):  # noqa: ANN001, ANN202
        if description and description.strip():
            read_again.append(description)
        return real(stored, version, description)

    monkeypatch.setattr(retrieve_module, "fresh_requirements", recording)

    found = await retrieve(db_session, "jobs that require Kafka")

    assert read_again == [direct.description_raw]
    assert _counts(found) == (1, 1, 1, 1, 1)
