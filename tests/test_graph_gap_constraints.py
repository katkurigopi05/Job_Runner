"""The skill-gaps question takes the same constraints as the projects question.

"Which skill gaps recur across jobs in my area for data engineer roles?" counts
the gaps over the IT jobs the constraint keeps, not over every IT job in the
search area. A place, an employer, a role from the role table and a topic
(found with Qwen) each narrow it, in the saved-links path and in the top-100
fallback alike. A role and a place can also narrow the same question together.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from packages.core.models import Match, Profile
from packages.matching import chunk_search, graph_questions, graph_rag, skill_links
from packages.matching.query_vectors import QueryVector
from tests.test_graph_constraints import fixture
from tests.test_graph_rag import OWNER, posting

GAPS = "Which skill gaps recur across jobs in my area"
PROJECTS = "Which jobs match skills from my projects"
ROLE_PLACE = "for backend engineer roles in the Bay Area"


@pytest.mark.parametrize(
    "question,label",
    [
        (f"{GAPS} for data engineer roles?", "for data engineer roles"),
        (f"{GAPS} in California?", "in California"),
        (f"{GAPS} in London?", "in London"),
        (f"{GAPS} {ROLE_PLACE}?", ROLE_PLACE),
        (f"{GAPS} IN the Bay Area FOR Backend Engineer Role?", ROLE_PLACE),
        ("Which skill gaps recur across my top matches in the Bay Area?", "in the Bay Area"),
        (
            "Which skills do my top matches ask for that I am missing for backend engineer roles?",
            "for backend engineer roles",
        ),
    ],
)
async def test_a_gaps_question_takes_role_and_place_scopes(question, label) -> None:
    query = await graph_rag.graph_intent(question)

    assert query is not None and query.intent == "gaps"
    assert query.constraint is not None and query.constraint.label == label


@pytest.mark.parametrize(
    "suffix",
    [
        "in Narnia",
        "for wizard roles",
        "at Unknown Employer",
        "in London at Acme",
        "in the Bay Area for wizard roles",
        f"{ROLE_PLACE} using Python",
    ],
)
async def test_a_gaps_suffix_it_cannot_read_is_not_a_gaps_question(worker_session, suffix) -> None:
    assert await graph_rag.graph_intent(f"{GAPS} {suffix}?", session=worker_session) is None


@pytest.mark.parametrize("linked", [False, True])
@pytest.mark.parametrize(
    "suffix,indices",
    [
        ("in California", [0, 1, 2]),
        ("in the Bay Area", [0, 1]),
        ("at Acme", [1, 2, 4]),
        ("for backend engineer roles", [0, 2, 4]),
        ("in Paris", [3]),
    ],
)
async def test_each_constraint_narrows_the_gaps_in_both_paths(
    worker_session, linked, suffix, indices
) -> None:
    await fixture(worker_session, linked)
    query = await graph_rag.graph_intent(f"{GAPS} {suffix}?", session=worker_session)
    assert query is not None and query.constraint is not None

    result = await graph_rag.retrieve_graph(worker_session, query)

    assert result.found.searched == len(indices)
    assert result.scope == ("open_postings" if linked else "top_matches")
    label = query.constraint.label
    answer = result.answer([e.label for e in result.evidence])
    assert label in result.summary and label in answer and label in result.answer_note
    if len(indices) > 1:
        # Every fixture job requires PostgreSQL, which the résumé does not list.
        assert f"directly required by {len(indices)} of {len(indices)}" in answer
    if suffix in {"at Acme", "for backend engineer roles"}:
        assert "within your search area" in result.answer_note
    if suffix == "in Paris":
        assert "standing search area waived" in result.summary
        assert "your search area" not in result.answer_note


async def test_unconstrained_gaps_still_read_every_it_job_in_the_area(worker_session) -> None:
    await fixture(worker_session, True)

    result = await graph_rag.retrieve_graph(worker_session, await graph_rag.graph_intent(GAPS))

    # The fixture's five in-area jobs: Paris and London are outside it.
    assert result.found.searched == 4
    assert "across 4 open IT jobs in your area" in result.answer_summary


@pytest.mark.parametrize("linked", [False, True])
async def test_role_and_place_precede_caps_and_gaps_count_distinct_jobs(worker_session, linked):
    jobs = await fixture(worker_session, linked)
    profile = await worker_session.scalar(select(Profile).where(Profile.candidate_id == OWNER))
    second_profile = Profile(candidate_id=OWNER, label="second graph profile")
    eligible = [
        posting(company_id=jobs[0].company_id, location="Oakland, CA"),
        posting(
            "Requirements\nPython is required.",
            company_id=jobs[0].company_id,
            location="San Jose, CA",
        ),
    ]
    outsiders = [
        posting(company_id=jobs[0].company_id, location=place, title=role)
        for place, role in [
            ("Los Angeles, CA", "Backend Engineer"),
            ("San Jose, CA", "Data Engineer"),
        ]
        for _ in range(graph_rag.TARGET_LIMIT + 1)
    ]
    worker_session.add_all([second_profile, *eligible, *outsiders])
    await worker_session.flush()
    worker_session.add_all(
        [Match(profile_id=profile.id, posting_id=job.id, score=0.5) for job in eligible]
        + [Match(profile_id=profile.id, posting_id=job.id, score=1.0) for job in outsiders]
        + [Match(profile_id=second_profile.id, posting_id=jobs[0].id, score=0.85)]
    )
    if linked:
        await skill_links.link_postings(worker_session, [job.id for job in [*eligible, *outsiders]])
    await worker_session.commit()

    project_query = await graph_rag.graph_intent(f"{PROJECTS} {ROLE_PLACE}?")
    assert project_query is not None
    projects = await graph_rag.retrieve_graph(worker_session, project_query)
    expected_ids = {job.id for job in [jobs[0], *eligible]}
    assert {p.posting_id for p in projects.found.passages} == expected_ids
    assert projects.found.searched == 3

    gap_query = await graph_rag.graph_intent(f"{GAPS} in the Bay Area for backend engineer roles?")
    assert gap_query is not None
    gaps = await graph_rag.retrieve_graph(worker_session, gap_query)
    assert projects.scope == gaps.scope == ("open_postings" if linked else "top_matches")
    assert gaps.found.searched == 3
    assert {p.posting_id for p in gaps.found.passages} <= {jobs[0].id, eligible[0].id}
    answer = gaps.answer([e.label for e in gaps.evidence])
    assert "**PostgreSQL** — directly required by 2 of 3" in answer
    assert ROLE_PLACE in answer


@pytest.mark.parametrize("linked", [False, True])
@pytest.mark.parametrize("base", [PROJECTS, GAPS])
async def test_empty_role_place_intersection_names_both_filters(worker_session, linked, base):
    await fixture(worker_session, linked)
    # The fixture has data engineer roles and jobs in Paris, but no data engineer jobs there.
    query = await graph_rag.graph_intent(f"{base} for data engineer roles in Paris?")
    assert query is not None
    result = await graph_rag.retrieve_graph(worker_session, query)
    assert result.found.searched == 0 and not result.evidence
    assert "for data engineer roles in Paris" in result.answer_summary
    assert result.answer_summary.startswith("No ")


async def test_a_gaps_topic_keeps_only_jobs_about_it(worker_session, monkeypatch) -> None:
    jobs = await fixture(worker_session, True)
    about = [jobs[1].id, jobs[2].id]

    async def embed(text, *, keep_alive=None, client=None):  # noqa: ANN001, ANN202
        return QueryVector(space="qwen", build="test", values=(1.0,), seconds=0.0)

    async def read(self, session, *, among, companies, limit):  # noqa: ANN001, ANN202
        return [(posting_id, 0, 10) for posting_id in about], True

    monkeypatch.setattr(graph_questions, "embed_question", embed)
    monkeypatch.setattr(chunk_search.ShortlistRanking, "read", read)

    query = await graph_rag.graph_intent(
        f"{GAPS} related to streaming pipelines?", session=worker_session
    )
    assert query is not None and query.intent == "gaps"
    result = await graph_rag.retrieve_graph(worker_session, query)

    assert result.found.searched == 2
    assert "related to streaming pipelines" in result.answer_summary
    assert "directly required by 2 of 2" in result.answer([e.label for e in result.evidence])


async def test_chat_answers_a_constrained_gaps_question_from_the_graph(
    client, worker_session, monkeypatch
) -> None:
    from apps.api.routers import chat
    from packages.llm.provider import StubProvider

    jobs = await fixture(worker_session, True)
    second = posting(company_id=jobs[0].company_id, location="Oakland, CA")
    worker_session.add(second)
    await worker_session.flush()
    await skill_links.link_postings(worker_session, [second.id])
    await worker_session.commit()

    class Selector(StubProvider):
        async def complete(self, system, user, **kwargs):  # noqa: ANN001, ANN003, ANN202
            return '{"ids": ["P1"]}'

    monkeypatch.setattr(chat, "provider_for", lambda selected: (Selector(), "local-model"))

    response = await client.post("/chat", json={"message": f"{GAPS} {ROLE_PLACE}?"})

    assert response.status_code == 200
    result = response.json()
    assert result["retrieval_method"] == "graph"
    assert ROLE_PLACE in result["reply"]
    assert "**PostgreSQL** — directly required by 2 of 2 open IT jobs" in result["reply"]
    cited = {source["posting_id"] for source in result["sources"]}
    assert cited and cited <= {
        str(jobs[0].id),
        str(second.id),
    }


@pytest.mark.parametrize("base", [GAPS, "Which jobs match my projects"])
@pytest.mark.parametrize(
    "suffix",
    [
        "for " + "for a" * 780,
        "for " + "for a" * 779 + " role",
        "in " + "in a" * 990,
        "at " + "a" * 3900,
    ],
)
async def test_a_pathological_suffix_returns_at_once(worker_session, base, suffix) -> None:
    """CodeQL flagged the suffix patterns on typed text (`ChatRequest` takes 4,000 characters)."""
    import time

    question = f"{base} {suffix}"[:4000]

    started = time.perf_counter()
    found = await graph_rag.graph_intent(question, session=worker_session)

    assert found is None
    assert time.perf_counter() - started < 0.5


@pytest.mark.parametrize(
    "suffix,label",
    [
        ("For Data Engineer Roles", "for data engineer roles"),
        ("for data engineer role", "for data engineer roles"),
        ("IN California", "in California"),
    ],
)
async def test_suffix_words_are_read_in_any_case(suffix, label) -> None:
    query = await graph_rag.graph_intent(f"{GAPS} {suffix}?")

    assert query is not None and query.constraint is not None
    assert query.constraint.label == label
