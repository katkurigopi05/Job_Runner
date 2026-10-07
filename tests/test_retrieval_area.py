"""The chat search keeps to the owner's search area, as the feed does.

Measured before this: the feed's top 200 were all in the Bay Area, and 11 of
the chat assistant's 25 answers to five ordinary questions were in London,
Tokyo, Singapore, Toronto and Stockholm. The search read a posting's location
only to print it. The rule is the feed's own (`search.area_exclusion`): the
United States, and outside California remote only.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient

from packages.core.config import get_settings
from packages.core.models import Company, CorpusStats, Posting
from packages.llm.provider import StubProvider
from packages.matching.embed import LexicalEmbedder
from packages.matching.retrieve import KEYWORD_POOL, retrieve

LEXICAL = LexicalEmbedder()


@pytest.fixture(autouse=True)
def standing_area(monkeypatch):
    """The shipped preference, whatever the machine running this has set."""
    monkeypatch.setenv("SEARCH_US_ONLY", "true")
    monkeypatch.setenv("SEARCH_REMOTE_OUTSIDE_CALIFORNIA", "true")
    get_settings.cache_clear()
    yield
    monkeypatch.undo()
    get_settings.cache_clear()


async def _company(session, name: str = "Acme") -> Company:
    company = Company(name=name, ats_type="greenhouse")
    session.add(company)
    await session.flush()
    return company


async def _posting(
    session, company: Company, location: str, title: str, body: str, *, embedded: bool = False
) -> Posting:
    posting = Posting(
        company_id=company.id,
        external_id=uuid.uuid4().hex[:8],
        url=f"https://x.test/{uuid.uuid4().hex[:8]}",
        title=title,
        location=location,
        description_raw=body,
        description_embedding=LEXICAL.encode_one(f"{title}\n{body}") if embedded else None,
        embedding_model=LEXICAL.name if embedded else None,
    )
    session.add(posting)
    await session.flush()
    return posting


ML = ("Machine Learning Engineer", "Train ranking models in PyTorch.")


async def test_a_posting_abroad_is_left_out_and_counted(db_session) -> None:
    company = await _company(db_session)
    here = await _posting(db_session, company, "San Francisco, CA", *ML)
    await _posting(db_session, company, "UK - London", *ML)
    await _posting(db_session, company, "Toronto, Canada", *ML)

    found = await retrieve(db_session, "machine learning engineer jobs")

    assert [p.posting_id for p in found.passages] == [here.id]
    assert found.outside_area == 2
    assert found.area is not None


async def test_on_site_in_another_state_is_left_out_and_remote_is_kept(db_session) -> None:
    company = await _company(db_session)
    remote = await _posting(db_session, company, "Remote, US", *ML)
    california = await _posting(db_session, company, "San Jose, CA", *ML)
    await _posting(db_session, company, "Austin, TX", *ML)

    found = await retrieve(db_session, "machine learning engineer jobs")

    assert {p.posting_id for p in found.passages} == {remote.id, california.id}
    assert found.outside_area == 1


async def test_a_question_naming_a_place_abroad_searches_it(db_session) -> None:
    """Filtering London out of "jobs in London" would answer a question unasked."""
    company = await _company(db_session)
    london = await _posting(db_session, company, "UK - London", *ML)

    found = await retrieve(db_session, "machine learning engineer jobs in London")

    assert [p.posting_id for p in found.passages] == [london.id]
    assert found.area is None and found.area_waived


async def test_the_owner_can_turn_the_area_off(db_session, monkeypatch) -> None:
    monkeypatch.setenv("SEARCH_US_ONLY", "false")
    get_settings.cache_clear()
    company = await _company(db_session)
    london = await _posting(db_session, company, "UK - London", *ML)

    found = await retrieve(db_session, "machine learning engineer jobs")

    assert [p.posting_id for p in found.passages] == [london.id]
    assert found.area is None and not found.area_waived


async def test_an_in_area_posting_below_a_pool_of_foreign_ones_is_found(db_session) -> None:
    """The area cuts the pool before the re-ranking, not after.

    Every foreign posting here names PyTorch in its title as well, which
    counts twice, so all of them outrank the one in San Francisco. Filtering
    the usual 30 would leave nothing.
    """
    company = await _company(db_session)
    for n in range(KEYWORD_POOL + 5):
        await _posting(db_session, company, "UK - London", f"PyTorch Engineer {n}", "PyTorch.")
    here = await _posting(db_session, company, "San Francisco, CA", "Engineer", "PyTorch.")

    found = await retrieve(db_session, "pytorch jobs")

    assert [p.posting_id for p in found.passages] == [here.id]


async def test_answers_from_vectors_alone_keep_to_the_area_too(db_session) -> None:
    """ "Any data engineering roles?" has no term to search for; vectors answer."""
    db_session.add(
        CorpusStats(revision=1, total_documents=100, counts_json={"data": 90, "engineering": 90})
    )
    company = await _company(db_session)
    body = "Build streaming data pipelines on Kafka for the engineering team."
    here = await _posting(db_session, company, "Remote, US", "Data Engineer", body, embedded=True)
    await _posting(db_session, company, "Tokyo, Japan", "Data Engineer", body, embedded=True)

    found = await retrieve(db_session, "any data engineering roles?")

    assert [p.posting_id for p in found.passages] == [here.id]
    assert found.outside_area == 1


async def test_a_company_hiring_only_abroad_says_so(
    client: AsyncClient, worker_session, monkeypatch
) -> None:
    """Otherwise "none at Faculty" reads as "Faculty is not hiring"."""
    import apps.api.routers.chat as chat_module

    company = await _company(worker_session, "Faculty")
    await _posting(worker_session, company, "UK - London", *ML)
    await worker_session.commit()

    seen: dict[str, str] = {}

    class Recorder(StubProvider):
        async def complete(self, system, user, *, max_tokens=1024, temperature=0.2) -> str:
            seen["user"] = user
            return "ok"

    monkeypatch.setattr(chat_module.llm_router, "build_provider", lambda name=None: Recorder())

    answered = await client.post("/chat", json={"message": "jobs at Faculty"})

    assert answered.status_code == 200
    assert "1 matching posting outside it left out" in seen["user"]
    assert "no open postings at Faculty in the search area" in seen["user"]


async def test_a_place_asked_for_ranks_the_postings_located_there(db_session) -> None:
    """ "software engineer jobs in London" returned three roles in San Jose.

    Found on 2026-10-06. The place a question names waives the area and is
    then only a keyword, and a posting matched it the same whether it was
    located in London or listed London among its employer's offices. With 28
    software engineer postings located there, the five shown were in San Jose,
    Gurgaon and "United States": ties went to the newest. A word in the
    location field is the posting saying where it is, and counts as a word in
    the title does.
    """
    company = await _company(db_session)
    mentions = [
        await _posting(
            db_session,
            company,
            "San Jose, California",
            "Software Engineer",
            f"Build streaming. Our offices are in San Jose, London and Tokyo. Team {n}.",
        )
        for n in range(4)
    ]
    located = await _posting(
        db_session, company, "London, UK", "Software Engineer", "Build streaming for the home."
    )

    found = await retrieve(db_session, "software engineer jobs in London")

    assert found.passages[0].posting_id == located.id
    assert {p.posting_id for p in found.passages} >= {m.id for m in mentions[:1]}, (
        "a mention is still a match, behind the postings that are there"
    )


async def test_a_title_that_is_only_the_roles_name_does_not_outrank_the_place(db_session) -> None:
    """The shape it had on the owner's database, which the test above did not have.

    The three postings in San Jose were titled exactly "Software Engineer" and
    do not mention London at all. A posting whose whole title is in the
    question goes first, and for a question about software engineers that is
    every posting titled "Software Engineer", wherever it is. The London ones
    are titled "Software Engineer, Payments" and were listed after them.
    """
    company = await _company(db_session)
    for n in range(3):
        await _posting(
            db_session,
            company,
            "San Jose, California",
            "Software Engineer",
            f"Build streaming {n}.",
        )
    located = await _posting(
        db_session, company, "London, UK", "Software Engineer, Payments", "Build payments."
    )

    found = await retrieve(db_session, "software engineer jobs in London")

    assert [p.posting_id for p in found.passages] == [located.id]
    assert len(found.more) == 0, "the three in San Jose say nothing of London"


async def test_a_role_asked_for_alone_still_lists_its_exact_titles_first(db_session) -> None:
    """And what must not move: with nothing more asked, the title rule stands."""
    company = await _company(db_session)
    longer = await _posting(
        db_session, company, "San Francisco, CA", "Staff Software Engineer, Payments", "Build."
    )
    exact = await _posting(db_session, company, "San Francisco, CA", "Software Engineer", "Build.")

    found = await retrieve(db_session, "software engineer jobs")

    assert [p.posting_id for p in found.passages][0] == exact.id
    assert longer.id in {p.posting_id for p in found.passages}
