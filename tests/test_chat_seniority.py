"""A level named in a question is read from titles, not searched for as a word.

Asked on the owner's database on 2026-10-13:

    entry level software engineer jobs   three postings, all "Senior …"
    junior backend roles                 five senior and staff postings
    internships in data science          nothing, with 196 intern titles open

"senior" and "staff" had worked, because titles say them. The others were
searched for as words in descriptions, where "entry" and "junior" turn up in
senior postings, and "internships" is in no title at all ("Intern" is).

A level is now read the way the feed reads it (`filters.detect_seniority`,
one ladder for both), from the title. What that leaves to decide is the half
of the corpus whose titles name no level, and the owner asked for both
answers to be built:

- **left out and counted**, the default. Unknown is not a pass (§16), and
  the context says how many were left out;
- **listed after** the ones that say it, with `CHAT_ALLOW_UNKNOWN_SENIORITY`.

A title that names *another* level is never listed under either.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from packages.core.config import get_settings
from packages.matching.level import asked_level
from packages.matching.retrieve import retrieve
from tests.test_chat_grounding import _answers
from tests.test_chat_skill_gaps import _owner, _target

BODY = "Requirements: SQL and Python. You will work with senior stakeholders and junior staff."

DATA_TITLES = (
    "Senior Data Engineer",
    "Staff Data Engineer",
    "Data Engineer",
    "Junior Data Engineer",
    "Data Engineer Intern",
    "Principal Data Engineer",
)


async def _data_engineers(session) -> None:
    profile, company = await _owner(session)
    for title in DATA_TITLES:
        await _target(session, profile, company, title, BODY, {})
    await session.commit()


def titles(found) -> list[str]:
    return [p.title for p in found.passages] + [entry.title for entry in found.more]


@pytest.fixture
def unknown_listed():
    settings = get_settings()
    before = settings.chat_allow_unknown_seniority
    settings.chat_allow_unknown_seniority = True
    yield
    settings.chat_allow_unknown_seniority = before


# --------------------------------------------------------------------------
# What is asked
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "question, levels",
    [
        ("senior data engineer jobs", {"senior"}),
        ("Sr. backend roles", {"senior"}),
        ("staff machine learning engineer roles", {"senior"}),
        ("entry level software engineer jobs", {"junior"}),
        ("entry-level roles in data", {"junior"}),
        ("junior backend roles", {"junior"}),
        ("new grad software engineer roles", {"junior"}),
        ("internships in data science", {"intern"}),
        ("data engineer intern jobs", {"intern"}),
        ("principal engineer openings", {"principal"}),
        ("junior or mid-level data roles", {"junior", "mid"}),
    ],
)
def test_a_level_in_a_question_is_read(question: str, levels: set[str]) -> None:
    asked = asked_level(question)

    assert asked is not None and set(asked.levels) == levels


@pytest.mark.parametrize(
    "question",
    [
        "Which jobs use Kafka?",
        # A level in the ladder, and a job in a question: these are titles.
        "solutions architect jobs",
        "director of engineering jobs",
        "associate product manager roles",
        "tech lead roles",
        # The opposite question is left to the search as it was.
        "data engineer jobs that are not senior",
        "non-senior backend roles",
    ],
)
def test_other_questions_name_no_level(question: str) -> None:
    assert asked_level(question) is None


# --------------------------------------------------------------------------
# Left out and counted: the default
# --------------------------------------------------------------------------


async def test_only_titles_at_the_level_are_listed(worker_session) -> None:
    await _data_engineers(worker_session)

    found = await retrieve(worker_session, "senior data engineer jobs")

    assert sorted(titles(found)) == ["Senior Data Engineer", "Staff Data Engineer"]
    assert found.level is not None
    assert (found.level.at_level, found.level.unknown, found.level.other) == (2, 1, 3)
    assert not found.level.unknown_listed


async def test_a_word_in_the_description_is_not_a_level(worker_session) -> None:
    """Every posting here says "senior stakeholders" and "junior staff"."""
    await _data_engineers(worker_session)

    found = await retrieve(worker_session, "junior data engineer jobs")

    assert titles(found) == ["Junior Data Engineer"]


async def test_entry_level_is_the_junior_rung(worker_session) -> None:
    await _data_engineers(worker_session)

    found = await retrieve(worker_session, "entry level data engineer jobs")

    assert titles(found) == ["Junior Data Engineer"]


async def test_internships_are_found_by_the_title_that_says_intern(worker_session) -> None:
    """ "internships" is in no title. It was the whole search, and found nothing."""
    await _data_engineers(worker_session)

    found = await retrieve(worker_session, "data engineer internships")

    assert found.attempted
    assert titles(found) == ["Data Engineer Intern"]


async def test_the_word_that_was_typed_goes_first_in_its_rung(worker_session) -> None:
    """Staff and Senior are one rung of the ladder. Asked for staff, the
    staff title is not behind a senior one for being newer or older."""
    await _data_engineers(worker_session)

    found = await retrieve(worker_session, "staff data engineer jobs")

    assert titles(found)[0] == "Staff Data Engineer"
    assert sorted(titles(found)) == ["Senior Data Engineer", "Staff Data Engineer"]


async def test_a_level_narrows_what_a_keyword_found(worker_session) -> None:
    profile, company = await _owner(worker_session)
    for title in ("Junior Platform Engineer", "Senior Platform Engineer", "Platform Engineer"):
        await _target(
            worker_session, profile, company, title, "Requirements: Kafka in production.", {}
        )
    await worker_session.commit()

    found = await retrieve(worker_session, "junior roles using Kafka")

    assert titles(found) == ["Junior Platform Engineer"]
    assert found.level is not None and found.level.other == 1


async def test_no_level_asked_changes_nothing(worker_session) -> None:
    await _data_engineers(worker_session)

    found = await retrieve(worker_session, "data engineer jobs")

    assert found.level is None
    assert sorted(titles(found)) == sorted(DATA_TITLES)


# --------------------------------------------------------------------------
# Listed after: the other answer
# --------------------------------------------------------------------------


async def test_titles_naming_no_level_can_be_listed_after(worker_session, unknown_listed) -> None:
    await _data_engineers(worker_session)

    found = await retrieve(worker_session, "senior data engineer jobs")

    listed = titles(found)
    assert sorted(listed[:2]) == ["Senior Data Engineer", "Staff Data Engineer"]
    assert listed[2:] == ["Data Engineer"]
    assert found.level is not None and found.level.unknown_listed


async def test_another_level_is_never_listed_either_way(worker_session, unknown_listed) -> None:
    await _data_engineers(worker_session)

    found = await retrieve(worker_session, "senior data engineer jobs")

    assert not {"Junior Data Engineer", "Data Engineer Intern", "Principal Data Engineer"} & set(
        titles(found)
    )


# --------------------------------------------------------------------------
# What the model is told
# --------------------------------------------------------------------------


async def test_the_context_says_what_was_left_out(
    client: AsyncClient, worker_session, monkeypatch
) -> None:
    await _data_engineers(worker_session)
    seen = _answers(monkeypatch, "Two [P1] [P2].")

    await client.post("/chat", json={"message": "senior data engineer jobs"})

    from apps.api.routers import chat
    from packages.core.schemas import ChatRequest

    ready = await chat.prepare(ChatRequest(message="senior data engineer jobs"), worker_session)
    context = ready.prompt
    assert seen == {}
    assert "level asked for: senior" in context
    assert "2 at that level by title" in context
    assert "1 naming no level" in context
    assert "3 naming another level" in context
    assert "Junior Data Engineer" not in context
    assert "not listed" in context


async def test_the_context_says_when_they_are_listed_after(
    client: AsyncClient, worker_session, monkeypatch, unknown_listed
) -> None:
    await _data_engineers(worker_session)
    seen = _answers(monkeypatch, "Two [P1] [P2].")

    await client.post("/chat", json={"message": "senior data engineer jobs"})

    from apps.api.routers import chat
    from packages.core.schemas import ChatRequest

    ready = await chat.prepare(ChatRequest(message="senior data engineer jobs"), worker_session)
    assert "listed after" in ready.prompt
    assert "do not call one senior" in ready.prompt
    assert seen == {}


# --------------------------------------------------------------------------
# Found by asking the owner's database, after the tests above passed
# --------------------------------------------------------------------------


async def test_a_question_that_is_only_a_level_lists_that_level(worker_session) -> None:
    """ "internships" names a level and nothing else. With the level set
    aside there was no word left, and that was read as a question naming
    nothing: not searched."""
    await _data_engineers(worker_session)

    found = await retrieve(worker_session, "internships")

    assert found.attempted and not found.unnamed
    assert titles(found) == ["Data Engineer Intern"]


async def test_the_cut_keeps_the_level_asked_ahead_of_no_level(
    worker_session, unknown_listed
) -> None:
    """Thirty are kept of what a keyword found. With titles naming no level
    let in, the one junior posting was thirty-first by keyword and was cut,
    so "junior backend roles" listed thirty postings and none was junior."""
    profile, company = await _owner(worker_session)
    # The others say it in their titles as well, which the keyword score
    # counts twice, so this one is last of the thirty-six whatever the order
    # of ties.
    await _target(
        worker_session, profile, company, "Junior Platform Engineer", "Requires Kafka.", {}
    )
    for number in range(35):
        await _target(
            worker_session,
            profile,
            company,
            f"Kafka Platform Engineer, Team {number}",
            "Requires Kafka.",
            {},
        )
    await worker_session.commit()

    found = await retrieve(worker_session, "junior roles using Kafka")

    assert titles(found)[0] == "Junior Platform Engineer"


# --------------------------------------------------------------------------
# Found by `make eval-retrieval`, which moved five known targets out of reach
# --------------------------------------------------------------------------


def test_a_question_that_names_a_title_with_a_level_word_in_it_is_left_alone() -> None:
    """ "Senior Director, GTM Enablement" is at the principal rung of the
    ladder, because "director" is. A question asking for "senior director"
    jobs named that title, and was answered with the senior rung, which the
    posting is not on. Where a question says one of the words that are a
    level in a title and a job in a question, it is naming a title."""
    assert asked_level("Find senior director GTM enablement jobs") is None
    assert asked_level("senior solutions architect roles") is None
    assert asked_level("junior associate roles") is None


def test_entry_or_mid_level_is_both_rungs() -> None:
    asked = asked_level("Are there entry or mid level financial communications jobs?")

    assert asked is not None and set(asked.levels) == {"junior", "mid"}
    # "entry" alone is not a level: "data entry jobs" are jobs entering data.
    assert asked_level("data entry jobs") is None


async def test_sr_is_senior_and_is_not_cut_behind_titles_that_spell_it_out(
    worker_session,
) -> None:
    """Three targets were lost this way. "Sr. Manager of Contracts" is at the
    senior rung, and was sorted behind every title with the word "Senior" in
    it before the thirty were cut, whatever else each said.

    Here the "Sr." title is the best match for the word asked for (it is in
    its title, which the keyword score counts twice), so it is first unless
    the level sort moves it."""
    profile, company = await _owner(worker_session)
    await _target(
        worker_session, profile, company, "Sr. Kafka Platform Manager", "Requires Kafka.", {}
    )
    for number in range(35):
        await _target(
            worker_session,
            profile,
            company,
            f"Senior Platform Engineer, Team {number}",
            "Requires Kafka.",
            {},
        )
    await worker_session.commit()

    found = await retrieve(worker_session, "senior roles using Kafka")

    assert titles(found)[0] == "Sr. Kafka Platform Manager"
