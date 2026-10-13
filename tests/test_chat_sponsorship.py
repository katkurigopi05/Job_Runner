"""A question about sponsorship is answered from what postings say about it.

On 2026-10-13 the owner's database was asked "jobs that sponsor H-1B" and the
five postings shown all stated that they do **not** sponsor. "Which AI
engineer jobs offer visa sponsorship?" showed two that refuse it and three
that say nothing. The search matches words (§14), and the postings that say
"sponsor" and "H-1B" most are the ones saying no: of the open postings in the
owner's area, 334 state they do not sponsor and 15 state that they do.

`eligibility.read_posting` has read that verdict out of a posting since §15,
and the feed has been able to filter on it since §18. The assistant's search
never asked it. Now a question that asks which jobs sponsor is answered with
the postings that state they do, and told how many state they do not.

The rules are §16's and §18's, one layer on:

- **Unknown is never a pass.** A posting that says nothing is not listed as
  one that sponsors, and nor is one whose statement cannot be resolved.
- **The words that ask are not searched for.** They find refusals, and a
  "Sponsorship Sales Manager".
- **The owner's own answer is still §2.2.** "Do I need sponsorship?" is
  refused before any search, as it was.
"""

from __future__ import annotations

from httpx import AsyncClient

from apps.api.protected_questions import is_refused
from packages.matching.retrieve import asks_which_sponsor, retrieve
from tests.test_chat_grounding import _answers
from tests.test_chat_skill_gaps import _owner, _target

OFFERS = "Requirements: Python and SQL. We offer visa sponsorship for this role."
REFUSES = "Requirements: Python and SQL. We are unable to sponsor visas for this position."
SILENT = "Requirements: Python and SQL. Great benefits and a friendly team."
UNCLEAR = "Requirements: Python. Candidates with or without sponsorship needs may apply."


async def _corpus(session) -> dict[str, str]:
    profile, company = await _owner(session)
    made = {
        "data offers": ("Data Engineer", OFFERS),
        "data refuses": ("Senior Data Engineer", REFUSES),
        "data silent": ("Data Engineer, Platform", SILENT),
        "data unclear": ("Data Engineer, Growth", UNCLEAR),
        "ml offers": ("Machine Learning Engineer", OFFERS + " You will train models with PyTorch."),
        "sales": ("Sponsorship Sales Manager", "Sell event sponsorship packages to partners."),
    }
    urls = {}
    for key, (title, body) in made.items():
        urls[key] = (await _target(session, profile, company, title, body, {})).url
    await session.commit()
    return urls


def shown(found) -> set[str]:
    return {passage.url for passage in found.passages} | {entry.url for entry in found.more}


# --------------------------------------------------------------------------
# What is asked
# --------------------------------------------------------------------------


def test_a_question_about_which_jobs_sponsor_is_recognised() -> None:
    assert asks_which_sponsor("Which jobs offer visa sponsorship?")
    assert asks_which_sponsor("jobs that sponsor H-1B")
    assert asks_which_sponsor("Show me data engineer roles that sponsor visas")
    assert asks_which_sponsor("any AI engineer roles with H1B sponsorship")


def test_other_questions_are_not() -> None:
    assert not asks_which_sponsor("Which jobs use Kafka?")
    # The opposite question is a different one, and is left to the search as it was.
    assert not asks_which_sponsor("Which jobs do not offer sponsorship?")
    assert not asks_which_sponsor("jobs without visa sponsorship")
    # A job about sponsorships is not a question about visas.
    assert not asks_which_sponsor("Sponsorship Sales Manager jobs")


def test_the_owners_own_answer_is_still_refused_first() -> None:
    assert is_refused("Do I need sponsorship?")
    assert not is_refused("Which jobs offer visa sponsorship?")


# --------------------------------------------------------------------------
# What is shown
# --------------------------------------------------------------------------


async def test_only_postings_that_state_they_sponsor_are_listed(worker_session) -> None:
    urls = await _corpus(worker_session)

    found = await retrieve(worker_session, "Which jobs offer visa sponsorship?")

    assert shown(found) == {urls["data offers"], urls["ml offers"]}


async def test_a_posting_that_refuses_is_never_listed(worker_session) -> None:
    """The five shown for "jobs that sponsor H-1B" were all of this kind."""
    urls = await _corpus(worker_session)

    found = await retrieve(worker_session, "jobs that sponsor H-1B")

    assert urls["data refuses"] not in shown(found)
    assert shown(found) == {urls["data offers"], urls["ml offers"]}


async def test_silence_and_an_unresolved_statement_are_not_an_offer(worker_session) -> None:
    urls = await _corpus(worker_session)

    found = await retrieve(worker_session, "Which jobs offer visa sponsorship?")

    assert urls["data silent"] not in shown(found)
    assert urls["data unclear"] not in shown(found)


async def test_the_word_in_a_title_is_not_an_offer(worker_session) -> None:
    urls = await _corpus(worker_session)

    found = await retrieve(worker_session, "Which jobs offer sponsorship?")

    assert urls["sales"] not in shown(found)


async def test_a_role_named_with_it_narrows_to_that_role(worker_session) -> None:
    urls = await _corpus(worker_session)

    found = await retrieve(worker_session, "Show me data engineer roles that sponsor visas")

    assert shown(found) == {urls["data offers"]}
    assert found.sponsorship is not None
    # Counted over that role's postings: one offers, one refuses, two do not say.
    assert (found.sponsorship.offered, found.sponsorship.refused) == (1, 1)
    assert found.sponsorship.unstated == 2


async def test_another_word_asked_for_narrows_it_too(worker_session) -> None:
    urls = await _corpus(worker_session)

    found = await retrieve(worker_session, "jobs using PyTorch that offer visa sponsorship")

    assert shown(found) == {urls["ml offers"]}


async def test_the_sentence_that_says_so_is_the_excerpt(worker_session) -> None:
    await _corpus(worker_session)

    found = await retrieve(worker_session, "Which jobs offer visa sponsorship?")

    assert found.passages
    assert all("sponsorship" in passage.excerpt.lower() for passage in found.passages)


async def test_none_offering_is_an_answer_with_its_counts(worker_session) -> None:
    profile, company = await _owner(worker_session)
    await _target(worker_session, profile, company, "Data Engineer", REFUSES, {})
    await _target(worker_session, profile, company, "Data Engineer, Platform", SILENT, {})
    await worker_session.commit()

    found = await retrieve(worker_session, "Which data engineer jobs offer visa sponsorship?")

    assert found.attempted and not found.passages
    assert found.sponsorship is not None
    assert (found.sponsorship.offered, found.sponsorship.refused, found.sponsorship.unstated) == (
        0,
        1,
        1,
    )


async def test_the_opposite_question_is_searched_as_before(worker_session) -> None:
    await _corpus(worker_session)

    found = await retrieve(worker_session, "Which jobs do not offer sponsorship?")

    assert found.sponsorship is None


# --------------------------------------------------------------------------
# What the model is told
# --------------------------------------------------------------------------


async def test_the_context_says_how_many_offer_refuse_and_do_not_say(
    client: AsyncClient, worker_session, monkeypatch
) -> None:
    await _corpus(worker_session)
    seen = _answers(monkeypatch, "Two do [P1] [P2].")

    body = (
        await client.post(
            "/chat", json={"message": "Show me data engineer roles that sponsor visas"}
        )
    ).json()

    context = seen["user"]
    assert "sponsorship:" in context
    assert "1 states that it sponsors" in context
    assert "1 states that it does not" in context
    assert "2 do not say" in context
    assert "Senior Data Engineer" not in context  # the one that refuses is not handed over
    assert [source["title"] for source in body["sources"]] == ["Data Engineer"]
