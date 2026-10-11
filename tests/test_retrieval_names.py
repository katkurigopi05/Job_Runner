"""A question that names a company or a job title finds it.

Measured on the owner's data before these rules: 10 of 18 such questions
found what they named. Bare titles were never searched ("Forward Deployed
Engineer" names no job word and no company), bracketed and short company
names did not count ("Weights & Biases (CoreWeave)", "Mistral AI"), and an
exact title lost to postings that only mentioned its words. After: 17 of 18,
the eighteenth being a company with no open postings, which now says so.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient

from packages.core.models import Company, CorpusStats, Posting
from packages.llm.provider import StubProvider
from packages.matching.retrieve import retrieve


async def _company(session, name: str) -> Company:
    company = Company(name=name, ats_type="greenhouse")
    session.add(company)
    await session.flush()
    return company


async def _posting(session, company, title, body="A role.", *, days_ago=1, closed=False):
    posting = Posting(
        company_id=company.id,
        external_id=uuid.uuid4().hex[:8],
        url=f"https://x.test/{uuid.uuid4().hex[:8]}",
        title=title,
        description_raw=body,
        first_seen_at=datetime.now(UTC) - timedelta(days=days_ago),
        closed_at=datetime.now(UTC) if closed else None,
    )
    session.add(posting)
    await session.flush()
    return posting


async def _stats(session, **shares: int) -> None:
    """Corpus statistics over 100 postings, `word=count`."""
    session.add(CorpusStats(revision=1, total_documents=100, counts_json=shares))
    await session.flush()


# --- job titles --------------------------------------------------------------------


async def test_a_bare_job_title_is_searched(db_session) -> None:
    company = await _company(db_session, "Hightouch")
    fde = await _posting(db_session, company, "Forward Deployed Engineer")

    found = await retrieve(db_session, "Forward Deployed Engineer")

    assert found.attempted
    assert found.passages[0].posting_id == fde.id


async def test_the_exact_title_beats_postings_that_mention_its_words(db_session) -> None:
    """ "it" is a stopword, so only the whole title could tell these apart."""
    instacart = await _company(db_session, "Instacart")
    other = await _company(db_session, "Shield AI")
    exact = await _posting(db_session, instacart, "Director, IT Operations", days_ago=5)
    for n in range(3):
        await _posting(
            db_session, other, f"Compensation Director, Operations {n}", "Director of operations."
        )

    found = await retrieve(db_session, "Director, IT Operations")

    assert found.passages[0].posting_id == exact.id


async def test_a_one_word_title_does_not_claim_every_question(db_session) -> None:
    company = await _company(db_session, "Acme")
    await _posting(db_session, company, "Engineer", days_ago=0)
    fde = await _posting(db_session, company, "Forward Deployed Engineer", days_ago=3)

    found = await retrieve(db_session, "Forward Deployed Engineer")

    assert found.passages[0].posting_id == fde.id


async def test_a_question_about_applications_still_searches_nothing(db_session) -> None:
    """The title gate needs every word in one title; these share none."""
    company = await _company(db_session, "Acme")
    await _posting(db_session, company, "Engineering Manager")

    for question in ("did the hiring manager reply?", "what needs me?"):
        assert not (await retrieve(db_session, question)).attempted, question


# --- company names -----------------------------------------------------------------


async def test_a_bracketed_alias_names_the_company_both_ways(db_session) -> None:
    wandb = await _company(db_session, "Weights & Biases (CoreWeave)")
    other = await _company(db_session, "Shield AI")
    ours = await _posting(db_session, wandb, "Solutions Architect")
    await _posting(db_session, other, "Staff Engineer", "Model weights and biases.")

    for question in ("jobs at Weights & Biases", "CoreWeave roles"):
        found = await retrieve(db_session, question)
        assert [p.posting_id for p in found.passages] == [ours.id], question


async def test_a_short_name_counts_only_when_it_is_rare(db_session) -> None:
    """ "Mistral" names Mistral AI; "together" must not name Together AI."""
    await _stats(db_session, together=50, teams=40)
    mistral = await _company(db_session, "Mistral AI")
    together = await _company(db_session, "Together AI")
    ours = await _posting(db_session, mistral, "Research Engineer")
    await _posting(db_session, together, "Inference Engineer")

    found = await retrieve(db_session, "anything at Mistral?")
    assert [p.posting_id for p in found.passages] == [ours.id]

    assert not (await retrieve(db_session, "how do teams work together?")).attempted


async def test_a_named_company_with_nothing_open_says_so(
    client: AsyncClient, worker_session, monkeypatch
) -> None:
    """Otherwise "no results" reads as "no match", not "they are not hiring"."""
    import apps.api.routers.chat as chat_module

    company = await _company(worker_session, "Mistral AI")
    await _posting(worker_session, company, "Research Engineer", closed=True)
    await worker_session.commit()

    seen: dict[str, str] = {}

    class Recorder(StubProvider):
        async def complete(self, system, user, *, max_tokens=1024, temperature=0.2) -> str:
            seen["user"] = user
            return "ok"

    monkeypatch.setattr(chat_module.llm_router, "build_provider", lambda name=None: Recorder())

    answered = await client.post("/chat", json={"message": "Mistral AI jobs"})

    assert answered.status_code == 200
    assert "no open postings at Mistral AI" in seen["user"]


# --- an ordinary word is not a company ---------------------------------------------
#
# After the owner's 3,802-company sheet was imported the registry held 3,933
# names, 108 of them a single word found in more than 2% of other companies'
# postings: "Do", "Based", "Fast", "Join", "Future", and "N Robotics" and
# "3 Data", which the tokenizer shortens to `robotics` and `data`. On
# 2026-10-05 "robotics jobs" returned nothing over 103 postings that say
# robotics, because the question was read as "postings at N Robotics".


async def _robotics_corpus(session):
    """A company the tokenizer shortens to `robotics`, and a posting that says it."""
    shortened = await _company(session, "N Robotics")
    employer = await _company(session, "Apptronik")
    posting = await _posting(session, employer, "Controls Engineer", "Humanoid robotics.")
    await _stats(session, robotics=7, humanoid=1)
    return shortened, posting


async def test_a_common_word_is_not_read_as_a_company_with_no_postings(db_session) -> None:
    _, posting = await _robotics_corpus(db_session)

    found = await retrieve(db_session, "robotics jobs")

    assert found.companies == ()
    assert [p.posting_id for p in found.passages] == [posting.id]


async def test_a_very_common_word_names_a_company_only_after_at(db_session) -> None:
    """ "Based" is a company on the sheet, and a word in 71% of postings."""
    based = await _company(db_session, "Based")
    other = await _company(db_session, "Acme")
    theirs = await _posting(db_session, based, "Founding Engineer")
    ours = await _posting(db_session, other, "Platform Engineer", "Based in San Francisco.")
    await _stats(db_session, based=71, founding=1)

    ordinary = await retrieve(db_session, "roles based in San Francisco")
    assert ordinary.companies == ()
    assert ours.id in [p.posting_id for p in ordinary.passages]

    # "founding" gives the keyword scan something to find; a question naming
    # only the company is answered from vectors, which these rows do not have.
    named = await retrieve(db_session, "founding engineer jobs at Based")
    assert named.companies == ("Based",)
    assert [p.posting_id for p in named.passages] == [theirs.id]


async def test_an_employer_others_mention_is_still_named(db_session) -> None:
    """OpenAI is in 7.9% of postings, most of them other companies'."""
    openai = await _company(db_session, "OpenAI")
    other = await _company(db_session, "Acme")
    theirs = await _posting(db_session, openai, "Research Engineer")
    await _posting(db_session, other, "ML Engineer", "Experience with the OpenAI API.")
    await _stats(db_session, openai=8, research=2)

    found = await retrieve(db_session, "OpenAI jobs")

    assert found.companies == ("OpenAI",)
    assert [p.posting_id for p in found.passages] == [theirs.id]


async def test_a_rare_one_word_name_still_names_its_company(db_session) -> None:
    """The case that worked before, with nothing open: it must still say so."""
    await _company(db_session, "Vanta")
    other = await _company(db_session, "Acme")
    await _posting(db_session, other, "Engineer")
    await _stats(db_session, vanta=1, engineer=60)

    found = await retrieve(db_session, "jobs at Vanta")

    assert found.companies == ("Vanta",)
    assert found.passages == ()


# --- words that are a company's name only in the right order and place -------------
#
# Found on 2026-10-10 by tracing why three benchmark questions returned
# nothing at all. Each named no company, was read as naming one with no open
# postings, and so was answered "no open postings at Native AI":
#
#     "... roles at an AI-native multimodal lakehouse company"   -> Native AI
#     "... bare metal for AI-native firms?"                      -> Native AI
#     "... governance teams at fast-growing tech firms"          -> Fast
#
# The first posting was second in the whole corpus by the keyword scan.


async def test_a_two_word_name_is_its_words_in_order(db_session) -> None:
    """ "AI-native" holds both words of Native AI and is not its name."""
    await _company(db_session, "Native AI")
    lance = await _company(db_session, "LanceDB")
    ours = await _posting(
        db_session, lance, "AI Research Engineer", "An AI-native multimodal lakehouse."
    )

    found = await retrieve(
        db_session, "AI Research Engineer roles at an AI-native multimodal lakehouse company"
    )

    assert found.companies == ()
    assert [p.posting_id for p in found.passages] == [ours.id]


async def test_a_two_word_name_said_in_order_still_names_it(db_session) -> None:
    native = await _company(db_session, "Native AI")
    other = await _company(db_session, "Acme")
    theirs = await _posting(db_session, native, "Founding Engineer")
    await _posting(db_session, other, "Engineer", "An AI-native product.")

    found = await retrieve(db_session, "engineer jobs at Native AI")

    assert found.companies == ("Native AI",)
    assert [p.posting_id for p in found.passages] == [theirs.id]


@pytest.mark.parametrize(
    "question",
    [
        "jobs building governance teams at fast-growing tech firms",
        "jobs building governance teams at fast‑growing tech firms",
        "jobs building governance teams at fast growing tech firms",
    ],
)
async def test_a_common_word_after_at_is_a_name_only_where_a_name_would_be(
    db_session, question
) -> None:
    """ "Fast" is a company on the sheet, and "at fast-growing firms" is not it."""
    await _company(db_session, "Fast")
    other = await _company(db_session, "Acme")
    ours = await _posting(db_session, other, "Head of Legal", "Build governance teams.")
    await _stats(db_session, fast=50, governance=2)

    found = await retrieve(db_session, question)

    assert found.companies == (), question
    assert ours.id in [p.posting_id for p in found.passages]


@pytest.mark.parametrize(
    "question",
    [
        "founding engineer jobs at Fast",
        "founding engineer jobs at fast?",
        "founding jobs at Fast in SF",
    ],
)
async def test_the_same_word_where_a_name_would_be_still_names_it(db_session, question) -> None:
    fast = await _company(db_session, "Fast")
    theirs = await _posting(db_session, fast, "Founding Engineer")
    await _stats(db_session, fast=50, founding=1)

    found = await retrieve(db_session, question)

    assert found.companies == ("Fast",), question
    assert theirs.id in [p.posting_id for p in found.passages]


async def test_a_name_after_a_second_at_is_still_read(db_session) -> None:
    """Only the first "at <word>" was looked at, so a question that says the
    word once as a word and once as the name was not about the company."""
    fast = await _company(db_session, "Fast")
    theirs = await _posting(db_session, fast, "Founding Engineer")
    await _stats(db_session, fast=50, founding=1)

    found = await retrieve(db_session, "founding roles at fast growing startups, or at Fast")

    assert found.companies == ("Fast",)
    assert theirs.id in [p.posting_id for p in found.passages]
