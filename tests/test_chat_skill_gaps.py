"""The assistant, synced with the owner's résumés: "what am I lagging on?".

The model is handed a table — the skills the owner's top matches require or
prefer, and which of them their résumés list — and explains it. It is never
handed the résumé text: only skill names, so a question asked of a remote
provider sends the smallest thing that answers it (§2.8).
"""

from __future__ import annotations

import re
import uuid

from httpx import AsyncClient

from packages.core.models import Candidate, Company, Match, Posting, Profile, Resume, User
from packages.llm.provider import StubProvider

#: Distinctive enough that finding it in the prompt means the text leaked.
RESUME_LINES = ["Built Python services at Initech", "Streamed events with Kafka"]


def _recorder() -> tuple[type[StubProvider], dict[str, str]]:
    seen: dict[str, str] = {}

    class Recorder(StubProvider):
        async def complete(self, system, user, *, max_tokens=1024, temperature=0.2) -> str:
            seen["system"] = system
            seen["user"] = user
            return "ok"

    return Recorder, seen


def _requirements(required=(), preferred=()) -> dict:
    def entries(keys):
        return [{"skill": k, "label": k, "quote": k} for k in keys]

    return {"skills": {"required": entries(required), "preferred": entries(preferred)}}


async def _owner(session) -> tuple[Profile, Company]:
    suffix = uuid.uuid4().hex[:8]
    user = User(email=f"o-{suffix}@example.com")
    session.add(user)
    await session.flush()
    candidate = Candidate(user_id=user.id, name="Owner", email=f"c-{suffix}@example.com")
    session.add(candidate)
    await session.flush()
    profile = Profile(candidate_id=candidate.id, label="p")
    company = Company(name="Streamco", ats_type="greenhouse")
    resume = Resume(
        candidate_id=candidate.id,
        version=1,
        storage_ref="r1",
        is_default=True,
        parsed_json={"raw_lines": RESUME_LINES},
    )
    session.add_all([profile, company, resume])
    await session.flush()
    return profile, company


async def _target(session, profile, company, title, body, requirements, score=0.5) -> Posting:
    posting = Posting(
        company_id=company.id,
        url=f"https://boards.greenhouse.io/streamco/jobs/{uuid.uuid4().hex[:8]}",
        title=title,
        location="Remote, US",
        description_raw=body,
        requirements_json=requirements,
    )
    session.add(posting)
    await session.flush()
    session.add(Match(profile_id=profile.id, posting_id=posting.id, score=score))
    await session.flush()
    return posting


async def _ask(client, monkeypatch, question: str) -> tuple[dict, dict[str, str]]:
    import apps.api.routers.chat as chat_module

    recorder, seen = _recorder()
    monkeypatch.setattr(chat_module.llm_router, "build_provider", lambda name=None: recorder())
    answered = await client.post("/chat", json={"message": question})
    assert answered.status_code == 200, answered.text
    return answered.json(), seen


async def test_a_lagging_question_gets_the_gap_table_and_never_the_resume_text(
    client: AsyncClient, worker_session, monkeypatch
) -> None:
    profile, company = await _owner(worker_session)
    await _target(
        worker_session,
        profile,
        company,
        "Platform Engineer",
        "Platform work.",
        _requirements(required=["kubernetes", "python"]),
    )
    await _target(
        worker_session,
        profile,
        company,
        "SRE",
        "Reliability work.",
        _requirements(required=["kubernetes"], preferred=["terraform"]),
    )
    await worker_session.commit()

    _, seen = await _ask(client, monkeypatch, "What am I lagging on?")

    context = seen["user"]
    assert "SKILL GAPS" in context
    missing = context.split("not on any résumé", 1)[1].split("already on a résumé", 1)[0]
    assert "Kubernetes: required by 2, preferred by 0" in missing
    assert "Terraform: required by 0, preferred by 1" in missing
    assert "Python" not in missing, "a skill the résumé lists is not a gap"
    assert "Initech" not in context, "résumé text must not reach the model"


async def test_a_question_about_something_else_carries_no_resume_section(
    client: AsyncClient, worker_session, monkeypatch
) -> None:
    profile, company = await _owner(worker_session)
    await _target(
        worker_session,
        profile,
        company,
        "Platform Engineer",
        "Platform work.",
        _requirements(required=["kubernetes"]),
    )
    await worker_session.commit()

    _, seen = await _ask(client, monkeypatch, "what needs me?")

    assert "SKILL GAPS" not in seen["user"]
    assert "Kubernetes" not in seen["user"]


async def test_each_posting_found_for_a_gap_question_says_what_it_asks_that_you_lack(
    client: AsyncClient, worker_session, monkeypatch
) -> None:
    """ "What am I missing for Kafka jobs?" is about those postings, not the feed."""
    profile, company = await _owner(worker_session)
    await _target(
        worker_session,
        profile,
        company,
        "Data Engineer",
        "You will build Kafka pipelines.",
        _requirements(required=["kafka", "kubernetes"]),
    )
    await worker_session.commit()

    body, seen = await _ask(client, monkeypatch, "What am I missing for Kafka jobs?")

    assert body["sources"], "the Kafka posting was found"
    passage = seen["user"].split("[P1]", 1)[1].split("SKILL GAPS", 1)[0]
    assert "not on your résumés: Kubernetes" in passage
    assert "Kafka" not in passage.split("not on your résumés:", 1)[1].splitlines()[0]


async def test_comparing_against_the_resume_is_not_a_protected_question(
    client: AsyncClient, worker_session, monkeypatch
) -> None:
    """§2.2 refuses drafting employment history; asking what is missing is not that."""
    await _owner(worker_session)
    await worker_session.commit()

    body, _ = await _ask(client, monkeypatch, "What skills am I lacking compared to my resume?")

    assert body["provider"] != "refused"


async def test_postings_found_for_a_gap_question_are_summarised_before_they_are_listed(
    client: AsyncClient, worker_session, monkeypatch
) -> None:
    """The one question a LangGraph agent won in the 2026-10-04 comparison.

    Asked "What am I missing for Kafka jobs?", the agent searched, then built
    "Terraform: missing in the Faire, New Relic and Anthropic postings" itself.
    The shipped assistant had every per-posting line in its context and
    answered from the feed-wide table instead. The summary the agent built is
    a count, so the code builds it, first, and the model reads it in one call.
    """
    profile, company = await _owner(worker_session)
    await _target(
        worker_session,
        profile,
        company,
        "Data Engineer",
        "You will build Kafka pipelines.",
        _requirements(required=["kafka", "terraform", "scala"]),
    )
    await _target(
        worker_session,
        profile,
        company,
        "Platform Engineer",
        "Kafka and Terraform for the platform.",
        _requirements(required=["kafka", "terraform"]),
    )
    await worker_session.commit()

    _, seen = await _ask(client, monkeypatch, "What am I missing for Kafka jobs?")

    context = seen["user"]
    across = next(line for line in context.splitlines() if "across these postings" in line)
    assert re.search(r"Terraform \(P\d, P\d\), Scala \(P\d\)", across), across
    assert "Kafka" not in across.split(":", 1)[1], "a skill the résumé lists is not missing"
    assert context.index("across these postings") < context.index("[P1]"), "summary first"
