"""'Software jobs' asks for a kind of job, so it is read from titles.

The owner asked "software job roles" on 2026-11-02. The search read
"software" as a word to find in descriptions, counted 3,776 postings that
mention it, and opened with a UX designer of scientific software; a
software-asset-management lead and a social-media director were in the list.
1,269 open titles were software engineers or developers.

Mapping the word onto the role table was tried first and measured on the
live database: "software jobs" became the software engineer role, but
"backend roles" fell from 634 postings to 32 and "frontend positions" from
214 to 11, because "Software Engineer, Backend" is not a backend-engineer
title to the table. So the word is read from titles instead, the way "any
roles with AI" already is: every title that has it, grouped by kind, and the
role named with the word after them.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from packages.core.models import Company, Posting
from packages.matching import retrieve as R


@pytest.fixture(autouse=True)
def no_models(monkeypatch):
    monkeypatch.setattr(R, "get_reranker", lambda: None)
    monkeypatch.setattr(R, "_encoders", lambda *args: {})


@pytest.mark.parametrize(
    ("question", "words", "qualifiers"),
    [
        ("software job roles", {"software"}, set()),
        ("any software jobs?", {"software"}, set()),
        ("software engineering positions", {"software"}, {"engineering"}),
        ("software development jobs", {"software"}, {"development"}),
    ],
)
def test_a_job_type_word_before_a_job_noun_is_found(question, words, qualifiers):
    assert R._job_type_words(question) == (frozenset(words), frozenset(qualifiers))


@pytest.mark.parametrize(
    "question",
    [
        "roles that mention software",
        "jobs using software testing tools",
        "software",
        "data jobs",
        "software engineer jobs",
        # Measured and left to the keyword search, which puts titles first.
        "backend roles",
        "front-end jobs",
        "full stack openings",
    ],
)
def test_the_word_as_a_subject_or_a_named_role_is_not(question):
    assert R._job_type_words(question) == (frozenset(), frozenset())


async def posting(session, title, body="A role.", location="San Francisco, CA"):
    company = Company(name="ExampleCo " + uuid.uuid4().hex[:8], ats_type="greenhouse")
    session.add(company)
    await session.flush()
    session.add(
        Posting(
            company_id=company.id,
            title=title,
            location=location,
            url="https://jobs.example.test/" + uuid.uuid4().hex,
            description_raw=body,
            content_hash=uuid.uuid4().hex,
            first_seen_at=datetime.now(UTC),
        )
    )
    await session.flush()


async def the_owners_mix(session) -> None:
    """Roughly what came back on 2026-11-02, in miniature."""
    await posting(session, "Software Engineer, Production Test")
    await posting(session, "Senior Software Engineer")
    await posting(session, "Senior Software Developer")
    await posting(session, "UX/UI Designer - Scientific Software")
    await posting(session, "Product Manager", "You will work closely with software teams.")
    await posting(session, "Software Engineer, Backend")
    await posting(session, "Backend Engineer")
    await posting(session, "Data Engineer", "Our backend runs on Postgres.")


async def test_software_job_roles_are_titles_with_software_engineers_first(db_session):
    await the_owners_mix(db_session)

    found = await R.retrieve(db_session, "software job roles")

    assert found.title_words == ("software",)
    assert found.title_total == 5
    assert found.match_total is None, "titles counted, not mentions"
    titles = [passage.title for passage in found.passages]
    assert "Product Manager" not in titles, "a mention in the description is not the job"
    assert titles[-1] == "UX/UI Designer - Scientific Software", "listed, after the engineers"
    assert "software engineer" in found.kinds[0][0]


async def test_a_qualifier_says_what_kind_and_is_not_required_in_the_title(db_session):
    await the_owners_mix(db_session)

    found = await R.retrieve(db_session, "software development jobs")

    assert found.title_words == ("software",)
    assert "Senior Software Developer" in {passage.title for passage in found.passages}


async def test_backend_roles_stay_a_keyword_search_with_titles_first(db_session):
    """A description saying "backend" is often a backend role, so it is kept."""
    await the_owners_mix(db_session)

    found = await R.retrieve(db_session, "backend roles")

    assert found.title_words == ()
    titles = [passage.title for passage in found.passages]
    assert set(titles[:2]) == {"Software Engineer, Backend", "Backend Engineer"}
    assert "Data Engineer" in titles


async def test_asking_what_mentions_the_word_is_still_a_word_search(db_session):
    await the_owners_mix(db_session)

    found = await R.retrieve(db_session, "which roles mention software")

    assert found.title_words == ()
    listed = {passage.title for passage in found.passages} | {more.title for more in found.more}
    assert "Product Manager" in listed, "a description that says it is a match"
    assert found.match_total == 6


async def test_a_job_type_with_another_subject_is_searched_as_before(db_session):
    await the_owners_mix(db_session)

    found = await R.retrieve(db_session, "backend roles using postgres")

    assert found.title_words == ()
    # A description search: the one posting that says Postgres is found.
    assert "Data Engineer" in {passage.title for passage in found.passages}
