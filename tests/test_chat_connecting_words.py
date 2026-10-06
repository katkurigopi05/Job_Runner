"""A word that joins a role to its subject is not something to search for.

The owner asked "any jobs role based on ai" on 2026-10-06 and was shown one
posting, "Enterprise Sales Executive, AI Solutions - US-Based", out of 222 open
ones with AI in the title. "any roles with AI" found all 222. The difference
was "based": `tokenize` drops "with" and "on" and keeps "based", so the title
search asked for a title holding both "based" and "ai".

Measured on the owner's 10,589 open postings that day:

    word        in titles    in descriptions
    based       0.18%        70.0%
    focused     0.02%        24.7%
    require     0.00%        23.2%
    use         0.00%        58.9%

Rare in titles, so as a required title word each one empties the result. And
the ones under 30% of descriptions (§14's cut-off) were being searched for
there: "roles focused on security" dropped "security" as too common (36%) and
returned postings that say "focused".

§14 already has the rule for the asker's verbs ("show me", "mention"): framing
words are listed, because rarity cannot be trusted to remove them. These are
the connectives the same list was missing.
"""

from __future__ import annotations

import pytest

from packages.matching import retrieve as R
from packages.matching.retrieve import retrieve
from tests.test_chat_title_words import _common, _company, _posting


@pytest.fixture(autouse=True)
def _no_reranker(monkeypatch):
    monkeypatch.setattr(R, "get_reranker", lambda: None)


async def _ai_titles(session, *common: str):
    await _common(session, "ai", *common)
    company = await _company(session)
    return {
        "pm": await _posting(session, company, "Product Manager, AI Platform", days_ago=0),
        "swe": await _posting(session, company, "Software Engineer, AI Infrastructure", days_ago=1),
        "ai": await _posting(session, company, "Staff AI Engineer", days_ago=2),
        "sales": await _posting(
            session, company, "Enterprise Sales Executive, AI Solutions - US-Based", days_ago=3
        ),
        "chef": await _posting(
            session, company, "Chef", "A kitchen focused on AI. We use it and require it."
        ),
    }


def _listed(found) -> set:
    return {p.posting_id for p in found.passages} | {m.posting_id for m in found.more}


async def test_the_owners_question_finds_every_ai_title(db_session) -> None:
    """ "based" is in 70% of the owner's descriptions, as it is here."""
    posts = await _ai_titles(db_session, "based")

    found = await retrieve(db_session, "any jobs role based on ai")

    assert _listed(found) == {posts[k].id for k in ("pm", "swe", "ai", "sales")}
    assert found.title_words == ("ai",)
    assert found.title_total == 4


@pytest.mark.parametrize(
    "question",
    [
        "roles focused on AI",
        "roles focusing on AI",
        "any roles that require AI",
        "roles requiring AI",
        "roles that use AI",
        "AI-driven roles",
        "AI-centric roles",
        "roles oriented around AI",
        "roles within AI",
        "roles specific to AI",
        "roles relevant to AI",
        "roles such as AI",
        "roles that include AI",
    ],
)
async def test_a_connecting_word_leaves_the_subject_to_be_found(db_session, question) -> None:
    """Without statistics for it, each of these was a term to search descriptions for."""
    posts = await _ai_titles(db_session)

    found = await retrieve(db_session, question)

    assert found.title_words == ("AI",), question
    assert _listed(found) == {posts[k].id for k in ("pm", "swe", "ai", "sales")}
    assert posts["chef"].id not in _listed(found), "it only says the connecting words"


async def test_a_connecting_word_is_not_searched_for_in_descriptions(db_session) -> None:
    """ "roles focused on security" was answered with postings that say "focused"."""
    await _common(db_session, "security")
    company = await _company(db_session)
    wanted = await _posting(db_session, company, "Security Engineer", "We protect the platform.")
    await _posting(db_session, company, "Chef", "A kitchen focused on seasonal menus.")

    found = await retrieve(db_session, "roles focused on security")

    assert [p.posting_id for p in found.passages] == [wanted.id]


async def test_a_rare_subject_is_still_what_is_searched_for(db_session) -> None:
    """Keywords decide relevance (§14); only the connective stops being one."""
    await _common(db_session, "ai")
    company = await _company(db_session)
    kafka = await _posting(db_session, company, "Engineer", "We run Kafka.")
    await _posting(db_session, company, "Recruiter", "A role requiring patience.")

    found = await retrieve(db_session, "roles requiring Kafka")

    assert [p.posting_id for p in found.passages] == [kafka.id]
