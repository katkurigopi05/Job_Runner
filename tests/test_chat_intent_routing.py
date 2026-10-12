"""Two ways of asking one thing take one path.

Reproduced on the owner's database on 2026-10-10, each pair through the real
`prepare()`:

    "Any jobs based on AI?"                          240 titles holding "AI"
    "Which roles focus on artificial intelligence?"  a keyword search on
        "intelligence": Governance Intelligence, Customer Intelligence, a
        marketing manager

    "How many applications are waiting for my review?"         not searched
    "Do I have any applications that need me to review them?"  five postings
        with "review" in them, a Clinical Review Nurse among them

    "What am I lagging on?"                          the gap table
    "Which skills am I missing for my target jobs?"  the gap table, and five
        postings that say "target", each with a gap of its own

None of the second questions was misread by a model. Each was routed by one
word, in code, before any model was asked: "intelligence" is rare enough to
search for and "AI" is not; "review" is in a title and "waiting" is in none;
"target" is in 19% of descriptions.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select

from apps.api.routers.chat import asks_about_gaps
from packages.core.models import Application
from packages.matching import retrieve as R
from packages.matching.retrieve import retrieve
from tests.test_chat_skill_gaps import _ask, _owner, _requirements, _target
from tests.test_chat_title_words import _common, _company, _posting


@pytest.fixture(autouse=True)
def _no_reranker(monkeypatch):
    monkeypatch.setattr(R, "get_reranker", lambda: None)


def _listed(found) -> set:
    return {p.posting_id for p in found.passages} | {m.posting_id for m in found.more}


# --------------------------------------------------------------------------
# "artificial intelligence" is "AI"
# --------------------------------------------------------------------------


async def _ai_and_intelligence(session):
    """Titles that say AI, and titles that say "intelligence" and mean something else."""
    await _common(session, "ai", "engineer", "engineers")
    company = await _company(session)
    return {
        "ai": await _posting(session, company, "Staff AI Engineer", days_ago=3),
        "swe": await _posting(session, company, "Software Engineer, AI Infrastructure", days_ago=1),
        "pm": await _posting(session, company, "Product Manager, AI Platform", days_ago=0),
        "governance": await _posting(
            session,
            company,
            "Engineering Manager, Governance Intelligence",
            "Lead engineers building identity governance.",
        ),
        "analyst": await _posting(
            session, company, "Regional Intelligence Analyst", "Threat intelligence for the region."
        ),
        "chef": await _posting(
            session, company, "Chef", "We use artificial intelligence to plan the menu."
        ),
    }


@pytest.mark.parametrize(
    "spelled_out",
    [
        "Which roles focus on artificial intelligence?",
        "any jobs based on Artificial Intelligence",
        "roles in artificial-intelligence",
    ],
)
async def test_the_words_spelled_out_find_what_the_initials_find(db_session, spelled_out) -> None:
    posts = await _ai_and_intelligence(db_session)

    short = await retrieve(db_session, "Any jobs based on AI?")
    long = await retrieve(db_session, spelled_out)

    assert _listed(long) == _listed(short) == {posts[k].id for k in ("ai", "swe", "pm")}
    assert long.title_words == ("AI",)
    assert long.title_total == short.title_total == 3


async def test_a_title_that_says_intelligence_is_not_an_ai_job(db_session) -> None:
    """Both words are rare enough to be searched for, and were, one at a time."""
    posts = await _ai_and_intelligence(db_session)

    found = await retrieve(db_session, "Which roles focus on artificial intelligence?")

    assert not _listed(found) & {posts[k].id for k in ("governance", "analyst", "chef")}


async def test_engineers_working_in_it_are_the_ai_engineers(db_session) -> None:
    """The same postings lead, whichever way round the words come."""
    posts = await _ai_and_intelligence(db_session)

    named = await retrieve(db_session, "Show me AI engineer jobs.")
    asked = await retrieve(
        db_session, "Are there any openings for engineers working in artificial intelligence?"
    )

    assert named.passages[0].posting_id == asked.passages[0].posting_id == posts["ai"].id
    assert _listed(asked) == {posts["ai"].id, posts["swe"].id}
    assert asked.passages[0].kind == "AI engineer"


async def test_intelligence_on_its_own_is_still_a_word_to_search_for(db_session) -> None:
    """Only the phrase is the same thing as the initials."""
    posts = await _ai_and_intelligence(db_session)

    found = await retrieve(db_session, "jobs in threat intelligence")

    assert posts["analyst"].id in _listed(found)
    assert found.title_words == ()


# --------------------------------------------------------------------------
# A question about the owner's applications is not a search for a title
# --------------------------------------------------------------------------


async def _review_titles(session):
    company = await _company(session)
    return {
        "nurse": await _posting(session, company, "Clinical Review Nurse", "Review prior auths."),
        "kafka": await _posting(
            session, company, "Data Engineer", "Streaming with Kafka. Code review is routine."
        ),
    }


@pytest.mark.parametrize(
    "question",
    [
        "How many applications are waiting for my review?",
        "Do I have any applications that need me to review them?",
        "Which of my job applications need review?",
        "is anything in my review queue",
        "any applications waiting on me to review",
    ],
)
async def test_the_owners_review_queue_is_not_searched_for_in_postings(
    db_session, question
) -> None:
    await _review_titles(db_session)

    found = await retrieve(db_session, question)

    assert found.attempted is False, question
    assert found.passages == ()


async def test_a_bare_title_is_still_found_by_its_words(db_session) -> None:
    """What the gate being closed must not cost: §14's "Forward Deployed Engineer"."""
    posts = await _review_titles(db_session)

    found = await retrieve(db_session, "Clinical Review Nurse")

    assert found.attempted is True
    assert found.passages[0].posting_id == posts["nurse"].id


async def test_a_title_with_the_word_and_the_numeral_is_still_a_title(db_session) -> None:
    """ "Applications Engineer I" has an application word and an "I"."""
    company = await _company(db_session)
    wanted = await _posting(db_session, company, "Applications Engineer I", "Support customers.")

    found = await retrieve(db_session, "Applications Engineer I")

    assert found.attempted is True
    assert wanted.id in _listed(found)


async def test_asking_whether_i_applied_to_a_named_job_still_searches(db_session) -> None:
    posts = await _review_titles(db_session)

    found = await retrieve(db_session, "Did I apply to the Clinical Review Nurse job?")

    assert found.attempted is True
    assert posts["nurse"].id in _listed(found)


async def test_a_question_that_asks_both_searches_for_the_half_about_jobs(db_session) -> None:
    """Counts come from the context whatever is searched; "review" belongs to that half."""
    posts = await _review_titles(db_session)

    found = await retrieve(
        db_session, "How many applications need my review, and which jobs use Kafka?"
    )

    assert [p.posting_id for p in found.passages] == [posts["kafka"].id]


async def test_review_in_a_question_about_jobs_is_searched_for(db_session) -> None:
    """Not a framing word: "code review" is something a job can involve."""
    posts = await _review_titles(db_session)

    found = await retrieve(db_session, "which jobs involve clinical review")

    assert found.passages[0].posting_id == posts["nurse"].id


# --------------------------------------------------------------------------
# "my target jobs" names nothing to look for
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "question",
    [
        "Which skills am I missing for my target jobs?",
        "what am I lacking for the jobs I am targeting",
        "any jobs?",
        "show me jobs",
    ],
)
async def test_a_question_that_names_nothing_is_not_searched(db_session, question) -> None:
    """It used to be searched with nothing, and reported as "0 of N searchable"."""
    company = await _company(db_session)
    await _posting(db_session, company, "Growth Marketer", "Reach the target audience.")

    found = await retrieve(db_session, question)

    assert found.attempted is False, question
    assert found.unnamed is True
    assert found.passages == ()


async def test_a_job_called_target_something_is_found_when_asked_for_by_title(db_session) -> None:
    """A framing word in a question, and still a word in a title asked for whole."""
    company = await _company(db_session)
    wanted = await _posting(db_session, company, "Target Account Manager", "Named accounts.")

    found = await retrieve(db_session, "Target Account Manager jobs")

    assert [p.posting_id for p in found.passages] == [wanted.id]


# --------------------------------------------------------------------------
# Whose gaps
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "question",
    [
        "What am I lagging on?",
        "Which skills am I missing for my target jobs?",
        "What should I learn next?",
        "skill gaps",
        "what is missing from the resume",
        "What am I missing for Kafka jobs?",
        "where is my CV weak",
    ],
)
def test_a_question_about_the_owner_is_a_gap_question(question) -> None:
    assert asks_about_gaps(question), question


@pytest.mark.parametrize(
    "question",
    [
        "Which jobs require Python skills?",
        "Which postings are missing a salary?",
        "which roles improve developer productivity",
        "jobs where you learn Rust",
        "what skills do AI engineer jobs ask for",
        "what needs me?",
    ],
)
def test_a_question_about_postings_is_not(question) -> None:
    """Each of these has a gap word in it. The résumé's skill list went into
    the context for all of them, and to a remote provider when one was picked."""
    assert not asks_about_gaps(question), question


# --------------------------------------------------------------------------
# Through the route: what the model is handed
# --------------------------------------------------------------------------


async def _feed(session):
    profile, company = await _owner(session)
    await _target(
        session,
        profile,
        company,
        "Platform Engineer",
        "Run the platform.",
        _requirements(required=["kubernetes"], preferred=["go"]),
        score=0.9,
    )
    # Says "target", is no match of the owner's, and asks for a skill of its own.
    session.add(
        R.Posting(
            company_id=company.id,
            url="https://x.test/marketing",
            title="Growth Marketer",
            description_raw="Reach the target audience with spreadsheets.",
            requirements_json=_requirements(required=["excel"]),
        )
    )
    await session.commit()


async def test_the_generic_gap_question_is_answered_from_the_match_cohort_alone(
    client: AsyncClient, worker_session, monkeypatch
) -> None:
    await _feed(worker_session)

    body, seen = await _ask(client, monkeypatch, "Which skills am I missing for my target jobs?")

    context = seen["user"]
    assert "POSTINGS: not searched" in context
    assert "Growth Marketer" not in context and "Excel" not in context
    assert "Kubernetes: required by 1" in context
    assert body["sources"] == [] and body["more_sources"] == []


async def test_the_gap_table_says_what_it_was_counted_over(
    client: AsyncClient, worker_session, monkeypatch
) -> None:
    """Two postings are open and one is a match. The counts are over the one."""
    await _feed(worker_session)

    _, seen = await _ask(client, monkeypatch, "What am I lagging on?")

    context = seen["user"]
    assert "your top 1 open match" in context
    assert "not all 2 open postings" in context


async def test_a_gap_question_about_named_jobs_still_gets_those_postings(
    client: AsyncClient, worker_session, monkeypatch
) -> None:
    profile, company = await _owner(worker_session)
    await _target(
        worker_session,
        profile,
        company,
        "Stream Engineer",
        "Kafka pipelines.",
        _requirements(required=["kafka", "terraform"]),
    )
    await worker_session.commit()

    body, seen = await _ask(client, monkeypatch, "What am I missing for Kafka jobs?")

    assert [s["title"] for s in body["sources"]] == ["Stream Engineer"]
    assert "not on your résumés: Terraform" in seen["user"]


async def test_the_review_count_is_the_databases_and_no_posting_comes_with_it(
    client: AsyncClient, worker_session, monkeypatch
) -> None:
    """Two applications parked for review and one that is not, counted by SQL."""
    profile, company = await _owner(worker_session)
    await _posting(worker_session, company, "Clinical Review Nurse", "Review prior auths.")
    for number, status in enumerate(("needs_review", "needs_review", "submitted")):
        worker_session.add(
            Application(
                candidate_id=profile.candidate_id,
                profile_id=profile.id,
                url=f"https://boards.greenhouse.io/streamco/jobs/{number}",
                ats="greenhouse",
                status=status,
            )
        )
    await worker_session.commit()
    waiting = await worker_session.scalar(
        select(func.count()).select_from(Application).where(Application.status == "needs_review")
    )

    body, seen = await _ask(
        client, monkeypatch, "Do I have any applications that need me to review them?"
    )

    assert waiting == 2
    assert f"needs_review: {waiting}" in seen["user"]
    assert "POSTINGS: not searched" in seen["user"]
    assert body["sources"] == []


async def test_what_is_waiting_is_said_to_be_part_of_what_was_counted(
    client: AsyncClient, worker_session, monkeypatch
) -> None:
    """One application is parked for review. It is in the count by status and
    in the list of what is waiting, and the local model answered "you have one
    that needs your review. Additionally, there is one in the review
    category": one application, read twice, on 2026-10-11."""
    profile, _ = await _owner(worker_session)
    worker_session.add(
        Application(
            candidate_id=profile.candidate_id,
            profile_id=profile.id,
            url="https://boards.greenhouse.io/streamco/jobs/7",
            ats="greenhouse",
            status="needs_review",
        )
    )
    await worker_session.commit()

    _, seen = await _ask(client, monkeypatch, "Do I have any applications that need me to review?")

    lines = seen["user"].splitlines()
    under = lines[lines.index("WAITING ON YOU:") + 1]
    assert "already counted above" in under and "not more applications" in under


# --------------------------------------------------------------------------
# Applying to a job is not the owner's applications
# --------------------------------------------------------------------------
#
# Found by review of the gate above, the same day. It fired on the verb too,
# and the words it set aside include ones a job can be about: "Which jobs can
# I apply to that use a message queue?" was searched without "queue".


async def test_a_job_to_apply_to_is_searched_for_all_it_says(db_session) -> None:
    company = await _company(db_session)
    queue = await _posting(db_session, company, "Backend Engineer", "Run a durable queue.")
    await _posting(db_session, company, "Support Agent", "Send each customer a message.")

    found = await retrieve(db_session, "Which jobs can I apply to that use a message queue?")

    assert queue.id in _listed(found), "the one word that says what the job is"


async def test_a_job_i_could_apply_to_keeps_its_review(db_session) -> None:
    posts = await _review_titles(db_session)

    found = await retrieve(db_session, "jobs I could apply to involving clinical review")

    assert found.passages[0].posting_id == posts["nurse"].id


def test_only_the_noun_and_a_standing_make_it_the_owners_applications() -> None:
    """ "queue" and "wait" are out: a job can use one and involve the other."""
    assert "application" in R._APPLICATION_NOUNS and "apply" not in R._APPLICATION_NOUNS
    assert not {"queue", "wait"} & R._APPLICATION_STATE
    assert {"review", "waiting", "pending", "queued"} <= R._APPLICATION_STATE
    assert R._FIRST_PERSON.search("do i have any") and not R._FIRST_PERSON.search("i/o bound")
