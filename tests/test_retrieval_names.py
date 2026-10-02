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
