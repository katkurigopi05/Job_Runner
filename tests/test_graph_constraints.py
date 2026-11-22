"""Graph scopes must consume the entire suffix and constrain both retrieval paths."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from packages.core.models import Company, Match, Posting, Profile
from packages.matching import graph_rag, skill_links
from tests.test_graph_rag import OWNER, posting, seed


@pytest.mark.parametrize(
    "suffix",
    [
        "in California",
        "in the Bay Area",
        "in San Francisco",
        "in London",
        "for backend engineer roles",
        "for AI/ML engineer roles",
        "for backend engineer roles in the Bay Area",
        "in the Bay Area for backend engineer roles",
    ],
)
async def test_recognized_suffixes_parse(suffix):
    query = await graph_rag.graph_intent(f"Which jobs match skills from my projects {suffix}?")
    assert query is not None and query.intent == "projects"
    assert query.constraint is not None


@pytest.mark.parametrize(
    "suffix",
    [
        "in Narnia",
        "in California using Python",
        "in London at Acme",
        "in California and Mars",
        "for wizard roles",
        "for senior backend engineer roles",
        "for backend engineer using Python roles",
        "for backend engineer roles in Narnia",
        "in the Bay Area for wizard roles",
        "for senior backend engineer roles in the Bay Area",
        "for backend engineer roles in the Bay Area using Python",
        "in the Bay Area for backend engineer roles using Python",
        "for backend engineer roles in the Bay Area at Acme",
        "in the Bay Area for backend engineer roles about RAG",
        "for backend engineer roles in California in the Bay Area",
        "at Unknown Employer",
    ],
)
async def test_unparsed_constraints_return_none(suffix, worker_session):
    assert (
        await graph_rag.graph_intent(
            f"Which jobs match my projects {suffix}?", session=worker_session
        )
        is None
    )


async def fixture(session, linked):
    base = await seed(session)
    base.location = "San Francisco, CA"
    base.title = "Backend Engineer"
    acme = Company(name="Acme", ats_type="greenhouse")
    empty = Company(name="Empty Company", ats_type="greenhouse")
    session.add_all([acme, empty])
    await session.flush()
    jobs = [
        base,
        posting(company_id=acme.id, location="San Jose, CA", title="Data Engineer"),
        posting(company_id=acme.id, location="Los Angeles, CA", title="Backend Engineer"),
        posting(company_id=acme.id, location="Paris, France", title="Backend Engineer"),
        posting(company_id=acme.id, location="Remote, US", title="Backend Engineer"),
    ]
    session.add_all(jobs[1:])
    await session.flush()
    profile = await session.scalar(select(Profile).where(Profile.candidate_id == OWNER))
    session.add_all(Match(profile_id=profile.id, posting_id=p.id, score=0.6) for p in jobs[1:])
    await session.commit()
    if linked:
        await skill_links.link_postings(
            session, list((await session.scalars(select(Posting.id))).all())
        )
        await session.commit()
    return jobs


@pytest.mark.parametrize("linked", [False, True])
@pytest.mark.parametrize(
    "suffix,indices",
    [
        ("in California", [0, 1, 2]),
        ("in the Bay Area", [0, 1]),
        ("in San Francisco", [0]),
        ("at Acme", [1, 2, 4]),
        ("for backend engineer roles", [0, 2, 4]),
        ("in Paris", [3]),
        ("for backend engineer roles in the Bay Area", [0]),
        ("in the Bay Area for backend engineer roles", [0]),
        ("for backend engineer roles in Paris", [3]),
    ],
)
async def test_each_constraint_applies_in_both_paths(worker_session, linked, suffix, indices):
    jobs = await fixture(worker_session, linked)
    query = await graph_rag.graph_intent(
        f"Which jobs match my projects {suffix}?", session=worker_session
    )
    assert query is not None
    result = await graph_rag.retrieve_graph(worker_session, query)
    assert {p.posting_id for p in result.found.passages} == {jobs[i].id for i in indices}
    assert result.scope == ("open_postings" if linked else "top_matches")
    label = query.constraint.label
    assert label in result.summary and label in result.answer([e.label for e in result.evidence])
    if suffix in {"at Acme", "for backend engineer roles"}:
        assert "within your search area" in result.answer_note
    if suffix.endswith("in Paris"):
        assert "standing search area waived" in result.summary
        assert "your search area" not in result.answer_note


@pytest.mark.parametrize("linked", [False, True])
async def test_known_company_with_no_open_jobs_is_scoped_empty(worker_session, linked):
    await fixture(worker_session, linked)
    query = await graph_rag.graph_intent(
        "Which jobs match my projects at Empty Company?", session=worker_session
    )
    assert query is not None
    result = await graph_rag.retrieve_graph(worker_session, query)
    assert result.found.searched == 0 and not result.evidence
    assert "at Empty Company" in result.answer_summary and "No open" in result.answer_summary


async def test_company_suffix_cannot_hide_extra_words(worker_session):
    await fixture(worker_session, False)
    for name in [
        "Acme using Python",
        "Acme and",
        "Acme in London",
        "Acme for backend engineer roles",
    ]:
        assert (
            await graph_rag.graph_intent(
                f"Which jobs match my projects at {name}?", session=worker_session
            )
            is None
        )


async def test_place_filter_precedes_sample_limit(worker_session):
    jobs = await fixture(worker_session, False)
    profile = await worker_session.scalar(select(Profile).where(Profile.candidate_id == OWNER))
    outsiders = [
        posting(location="Los Angeles, CA", company_id=jobs[0].company_id) for _ in range(101)
    ]
    worker_session.add_all(outsiders)
    await worker_session.flush()
    worker_session.add_all(
        Match(profile_id=profile.id, posting_id=p.id, score=1.0) for p in outsiders
    )
    await worker_session.commit()
    query = await graph_rag.graph_intent("Which jobs match my projects in San Francisco?")
    result = await graph_rag.retrieve_graph(worker_session, query)
    assert [p.posting_id for p in result.found.passages] == [jobs[0].id]
    assert result.found.searched == 1


@pytest.mark.parametrize("path", ["/chat", "/chat/stream"])
@pytest.mark.parametrize(
    "suffix,label",
    [
        ("in San Francisco", "in San Francisco"),
        (
            "in the Bay Area for backend engineer roles",
            "for backend engineer roles in the Bay Area",
        ),
    ],
)
async def test_chat_evidence_obeys_scope(path, suffix, label, client, worker_session, monkeypatch):
    from apps.api.routers import chat
    from packages.llm.provider import StubProvider

    jobs = await fixture(worker_session, True)

    class Selector(StubProvider):
        async def complete(self, system, user, **kwargs):
            return '{"ids": ["P1"]}'

    monkeypatch.setattr(chat, "provider_for", lambda selected: (Selector(), "local-model"))
    response = await client.post(
        path, json={"message": f"Which jobs match skills from my projects {suffix}?"}
    )
    assert response.status_code == 200
    result = (
        json.loads(response.text.splitlines()[-1])["reply"]
        if path.endswith("stream")
        else response.json()
    )
    assert result["retrieval_method"] == "graph"
    assert {s["posting_id"] for s in result["sources"]} == {str(jobs[0].id)}
    assert label in result["reply"]


@pytest.mark.parametrize("linked", [False, True])
async def test_foreign_country_alias_and_city_disambiguation(worker_session, linked):
    jobs = await fixture(worker_session, linked)
    profile = await worker_session.scalar(select(Profile).where(Profile.candidate_id == OWNER))
    misleading = [
        posting(location="Paris, TX", company_id=jobs[0].company_id),
        posting(location="Parisville, France", company_id=jobs[0].company_id),
    ]
    worker_session.add_all(misleading)
    await worker_session.flush()
    worker_session.add_all(
        Match(profile_id=profile.id, posting_id=p.id, score=0.9) for p in misleading
    )
    if linked:
        await skill_links.link_postings(worker_session, [p.id for p in misleading])
    await worker_session.commit()
    country = await graph_rag.graph_intent("Which jobs match my projects in UK?")
    result = await graph_rag.retrieve_graph(worker_session, country)
    assert result.found.passages and all(p.location == "London, UK" for p in result.found.passages)
    city = await graph_rag.graph_intent("Which jobs match my projects in Paris?")
    result = await graph_rag.retrieve_graph(worker_session, city)
    assert [p.posting_id for p in result.found.passages] == [jobs[3].id]
