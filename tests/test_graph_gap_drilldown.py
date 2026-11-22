"""A recurring gap's count opens onto the jobs behind it and the projects declaring it.

"Kubernetes — directly required by 37 of 912 jobs" was a number the owner could
not check. The answer now carries, per gap, every job counted (fixed when the
answer was written), the first few with the line each requires the skill in,
and the eligible projects whose repositories declare it (`gap_evidence.py`).
What must hold: the count is the distinct jobs listed; a preference never
counts; an excluded project never appears; a declaration is never called
implementation; none of it reaches a model; older answers still read.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from packages.core.models import Candidate, Company, Match, Posting, Profile, User
from packages.core.schemas import ChatReply
from packages.core.schemas_graph import GAP_JOBS_INLINE, GapEvidenceOut, GapProjectOut
from packages.github.code_signals import Classification
from packages.matching.gap_evidence import commit_of
from packages.matching.graph_rag import RESULT_LIMIT, SavedLinks, graph_result
from tests.test_graph_rag import OWNER, posting, project, resume

KUBE = "Requirements\nKubernetes and Terraform are required."
SHA = "0123456789abcdef0123456789abcdef01234567"


def _gaps(postings, projects=(), links=None):
    rows = [(job, f"Company {n}") for n, job in enumerate(postings)]
    return graph_result(OWNER, [resume()], list(projects), rows, "gaps", links=links)


def _gap(result, skill="kubernetes") -> GapEvidenceOut:
    return next(gap for gap in result.gaps if gap.skill == skill)


# --- the count is the jobs listed ---------------------------------------------------


def test_the_count_is_the_distinct_jobs_listed_in_the_order_they_were_read():
    jobs = [posting(body=KUBE, title=f"Platform Engineer {n}") for n in range(3)]
    # A job whose saved links name the skill twice still counts once.
    twice = SavedLinks(
        True, (("kubernetes", "Kubernetes is required"), ("kubernetes", "Run Kubernetes"))
    )
    result = _gaps(jobs, links={jobs[1].id: twice})

    gap = _gap(result)
    assert gap.total == 3 == len(gap.posting_ids) == len(set(gap.posting_ids))
    assert gap.posting_ids == [job.id for job in jobs]
    assert gap.reviewed == 3
    assert [job.quote for job in gap.jobs] == [
        "Kubernetes and Terraform are required",
        "Kubernetes is required",
        "Kubernetes and Terraform are required",
    ]
    assert "Kubernetes** — directly required by 3 of 3" in result.answer([gap.source_label])
    assert "Kubernetes: directly required by 3 of 3" in result.evidence[0].text


def test_only_the_first_jobs_are_carried_in_full_and_every_id_is_kept():
    jobs = [posting(body=KUBE) for _ in range(GAP_JOBS_INLINE + 4)]

    gap = _gap(_gaps(jobs))

    assert gap.total == len(jobs) == len(gap.posting_ids)
    assert [job.posting_id for job in gap.jobs] == gap.posting_ids[:GAP_JOBS_INLINE]


def test_a_preference_or_an_alternative_never_enters_the_list():
    required = [posting(body=KUBE) for _ in range(2)]
    preferred = posting(body="Requirements\nTerraform is required.\nKubernetes is preferred.")
    alternative = posting(body="Requirements\nKubernetes or Nomad experience is required.")

    gap = _gap(_gaps([*required, preferred, alternative]))

    assert gap.posting_ids == [job.id for job in required]
    assert gap.total == 2 and gap.reviewed == 4


def test_a_skill_one_job_requires_is_not_a_recurring_gap():
    result = _gaps([posting(body=KUBE), posting(body="Requirements\nTerraform is required.")])
    assert [gap.skill for gap in result.gaps] == ["terraform"]


def test_every_reported_gap_has_a_drilldown_under_the_label_the_answer_cites():
    jobs = [posting(body=KUBE) for _ in range(2)]
    result = _gaps(jobs)
    assert {gap.source_label for gap in result.gaps} == {item.label for item in result.evidence}
    assert len(result.gaps) <= RESULT_LIMIT
    assert "Evidence details lists the jobs behind each count." in result.answer_note


# --- projects ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "changes",
    [
        {"is_fork": True},
        {"is_private": True},
        {"include": False},
        {"candidate_id": uuid.uuid4()},
        {"source": "untrusted"},
    ],
)
def test_an_excluded_project_stays_excluded(changes):
    kept = project(name="kept", dependency_technologies=["Kubernetes"])
    left_out = project(name="left-out", dependency_technologies=["Kubernetes"], **changes)

    gap = _gap(_gaps([posting(body=KUBE) for _ in range(2)], [kept, left_out]))

    assert [p.project_id for p in gap.projects] == [kept.id]


def test_a_declaration_is_labelled_a_declaration_and_never_implementation():
    manifest = project(name="infra", language="HCL", dependency_technologies=["Kubernetes"])
    language = project(name="tools", language="Go", dependency_technologies=[])
    jobs = [posting(body="Requirements\nGo and Kubernetes are required.") for _ in range(2)]

    result = _gaps(jobs, [manifest, language])

    kube, go = _gap(result), _gap(result, "go")
    assert [(p.repository, p.declared_in) for p in kube.projects] == [
        ("infra", ["dependency_file"])
    ]
    assert [(p.repository, p.declared_in) for p in go.projects] == [("tools", ["primary_language"])]
    assert {p.category for gap in result.gaps for p in gap.projects} == {Classification.DECLARED}
    with pytest.raises(ValidationError, match="declarations only"):
        GapProjectOut(
            project_id=uuid.uuid4(),
            repository="infra",
            url="https://github.com/example/infra",
            category=Classification.IMPLEMENTED,
            declared_in=["dependency_file"],
        )


def test_a_live_checked_repository_is_cited_at_its_commit():
    live = project(
        name="infra",
        full_name="example/infra",
        url=f"https://github.com/example/infra/tree/{SHA}",
        dependency_technologies=["Kubernetes"],
    )
    imported = project(name="other", dependency_technologies=["Kubernetes"])

    gap = _gap(_gaps([posting(body=KUBE) for _ in range(2)], [live, imported]))

    assert [(p.repository, p.commit_sha) for p in gap.projects] == [
        ("example/infra", SHA),
        ("other", None),
    ]
    assert commit_of("https://github.com/example/infra") is None


def test_a_count_and_its_list_cannot_disagree():
    with pytest.raises(ValidationError, match="distinct jobs"):
        GapEvidenceOut(
            skill="kubernetes",
            label="Kubernetes",
            source_label="P1",
            total=3,
            reviewed=5,
            posting_ids=[uuid.uuid4(), uuid.uuid4()],
            jobs=[],
            projects=[],
        )


# --- privacy and older answers ------------------------------------------------------


def test_nothing_of_the_drilldown_reaches_the_selection_prompt():
    secret = project(
        name="private-sounding-name",
        full_name="example/private-sounding-name",
        url=f"https://github.com/example/private-sounding-name/tree/{SHA}",
        dependency_technologies=["Kubernetes"],
    )
    result = _gaps([posting(body=KUBE) for _ in range(2)], [secret])

    prompt = result.prompt("Which skill gaps recur across my strongest matches?")
    assert _gap(result).projects, "the drill-down has the project"
    for private in ("private-sounding-name", SHA, "distinctive private"):
        assert private not in prompt
        assert all(private not in item.model_text for item in result.evidence)


def test_an_answer_saved_before_drilldowns_still_reads():
    older = ChatReply(reply="Kubernetes recurs.", provider="ollama", grounded=True).model_dump(
        mode="json"
    )
    del older["graph_gaps"]
    assert ChatReply.model_validate(older).graph_gaps == []


# --- through the API ---------------------------------------------------------------


async def _seed_gaps(session, *, jobs: int = 3) -> list[Posting]:
    user = User(email="gaps@example.test")
    session.add(user)
    await session.flush()
    company = Company(name="Gap Example", ats_type="greenhouse")
    session.add_all(
        [Candidate(id=OWNER, user_id=user.id, name="Gap Owner", email="o@example.test"), company]
    )
    await session.flush()
    profile = Profile(candidate_id=OWNER, label="gaps")
    session.add_all(
        [
            profile,
            resume(),
            project(
                name="infra-repo",
                full_name="example/infra-repo",
                dependency_technologies=["Kubernetes"],
            ),
            project(name="hidden-repo", dependency_technologies=["Kubernetes"], include=False),
        ]
    )
    await session.flush()
    postings = [
        posting(body=KUBE, company_id=company.id, title=f"Platform Engineer {n}")
        for n in range(jobs)
    ]
    postings.append(
        posting(
            body="Requirements\nTerraform is required.\nKubernetes is preferred.",
            company_id=company.id,
        )
    )
    session.add_all(postings)
    await session.flush()
    session.add_all(
        Match(profile_id=profile.id, posting_id=job.id, score=0.9 - n * 0.01)
        for n, job in enumerate(postings)
    )
    await session.commit()
    return postings


@pytest.mark.parametrize("path,provider", [("/chat", "ollama"), ("/chat/stream", "openrouter")])
async def test_a_gap_answer_carries_its_drilldown_and_the_model_sees_none_of_it(
    path, provider, client, worker_session, monkeypatch
):
    from apps.api.routers import chat
    from packages.llm.provider import StubProvider

    postings = await _seed_gaps(worker_session)
    seen: dict[str, str] = {}

    class Recorder(StubProvider):
        async def complete(self, system, user, **kwargs):
            seen["prompt"] = user
            evidence = json.loads(user.split("\n", 2)[-1].split("\n", 1)[-1])["evidence"]
            kube = next(item["id"] for item in evidence if "Kubernetes:" in item["text"])
            return json.dumps({"ids": [kube]})

    monkeypatch.setattr(chat, "provider_for", lambda selected: (Recorder(), "a-model"))
    response = await client.post(
        path,
        json={
            "message": "Which skill gaps recur across my strongest matches?",
            "provider": provider,
        },
    )

    assert response.status_code == 200, response.text
    reply = (
        json.loads(response.text.splitlines()[-1])["reply"]
        if path.endswith("stream")
        else response.json()
    )
    assert [gap["skill"] for gap in reply["graph_gaps"]] == ["kubernetes"], "only what is cited"
    gap = reply["graph_gaps"][0]
    counted = [str(job.id) for job in postings[:3]]
    assert gap["posting_ids"] == counted and gap["total"] == 3 and gap["reviewed"] == 4
    assert f"directly required by {gap['total']} of {gap['reviewed']}" in reply["reply"]
    cited = {s["posting_id"] for s in reply["sources"] if s["cited"]}
    assert cited <= set(gap["posting_ids"])
    assert [p["repository"] for p in gap["projects"]] == ["example/infra-repo"]
    assert all(p["category"] == "declared" for p in gap["projects"])
    for private in ("infra-repo", "hidden-repo", "distinctive private", *counted[1:]):
        assert private not in seen["prompt"]
        assert private not in reply["reply"]


async def test_later_pages_are_the_same_jobs_read_again(client, worker_session):
    postings = await _seed_gaps(worker_session, jobs=4)
    closed, edited, kept, deleted = postings[:4]
    closed.closed_at = datetime.now(UTC)
    edited.description_raw = "Requirements\nTerraform is required."
    from packages.matching.requirements import extract

    edited.requirements_json = extract(edited.description_raw).as_json()
    await worker_session.delete(deleted)
    await worker_session.commit()
    asked = [str(job_id) for job_id in (kept.id, closed.id, edited.id, deleted.id)]

    response = await client.post(
        "/chat/gap-jobs", json={"skill": "kubernetes", "posting_ids": asked}
    )

    assert response.status_code == 200, response.text
    jobs = response.json()["jobs"]
    assert [job["posting_id"] for job in jobs] == asked[:3], "in order; the deleted one left out"
    assert [job["state"] for job in jobs] == ["current", "closed", "changed"]
    assert jobs[0]["quote"] == "Kubernetes and Terraform are required"
    assert jobs[2]["quote"] is None


@pytest.mark.parametrize(
    "body",
    [
        {"skill": "not_a_skill", "posting_ids": [str(uuid.uuid4())]},
        {"skill": "kubernetes", "posting_ids": []},
        {"skill": "kubernetes", "posting_ids": [str(uuid.uuid4()) for _ in range(51)]},
    ],
)
async def test_gap_jobs_refuses_what_no_answer_could_ask(client, body):
    response = await client.post("/chat/gap-jobs", json=body)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"


async def test_the_preferred_posting_in_the_seed_is_read_but_not_counted(worker_session):
    from packages.matching.graph_rag import retrieve_graph

    postings = await _seed_gaps(worker_session)
    result = await retrieve_graph(worker_session, "gaps")
    gap = _gap(result)
    assert postings[-1].id not in gap.posting_ids
    assert gap.reviewed == result.found.searched == 4
    assert await worker_session.scalar(select(Posting).where(Posting.id == postings[-1].id))
