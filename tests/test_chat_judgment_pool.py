"""The pool of postings the owner grades for the assistant's search (Phase 5).

`packages/matching/judged.py` asks each frozen question in each search mode
and pools what came back. No test here loads a model: two modes that differ
only in the search area return different postings for the same question.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from packages.core.config import get_settings
from packages.core.models import Company, Posting
from packages.core.models_judgments import RetrievalJudgment
from packages.matching import judged
from packages.matching import retrieve as R

NOW = datetime.now(UTC)

AREA = judged.Mode(name="area", settings={})
EVERYWHERE = judged.Mode(name="everywhere", settings={"search_us_only": False})


@pytest.fixture(autouse=True)
def standing_area(monkeypatch):
    monkeypatch.setenv("SEARCH_US_ONLY", "true")
    monkeypatch.setenv("SEARCH_REMOTE_OUTSIDE_CALIFORNIA", "true")
    get_settings.cache_clear()
    yield
    monkeypatch.undo()
    get_settings.cache_clear()


def _set(*questions: judged.Question, depth: int = 8) -> judged.QuestionSet:
    return judged.QuestionSet(
        version="test-v1", depth=depth, modes=(AREA, EVERYWHERE), questions=questions
    )


KAFKA = judged.Question(key="kafka", question="kafka jobs", stratum="skill", expect="answer")
ZIG = judged.Question(key="zig", question="roles using Zig?", stratum="absent", expect="none")


async def _corpus(session) -> dict[str, Posting]:
    company = Company(name="Acme", ats_type="greenhouse")
    session.add(company)
    await session.flush()
    made = {}
    for n, (name, location) in enumerate(
        (("here", "San Francisco, CA"), ("abroad", "UK - London"), ("chef", "San Francisco, CA"))
    ):
        made[name] = Posting(
            company_id=company.id,
            external_id=name,
            url=f"https://x.test/{name}",
            title="Pastry Chef" if name == "chef" else "Engineer",
            location=location,
            description_raw="Croissants." if name == "chef" else "Kafka pipelines.",
            content_hash=f"hash-{name}",
            first_seen_at=NOW - timedelta(minutes=n),
        )
    session.add_all(made.values())
    await session.flush()
    return made


# --- the question set -----------------------------------------------------------------


def test_the_frozen_set_loads_and_holds_some_questions_out() -> None:
    questions = judged.load()

    assert questions.version == "chat-v1"
    assert {mode.name for mode in questions.modes} == {"shipped", "semantic", "cross_encoder"}
    held = [q for q in questions.questions if q.held_out]
    assert 0 < len(held) < len(questions.questions)
    assert {q.expect for q in questions.questions} == {"answer", "none", "not_searched"}


def test_held_out_is_decided_by_the_key_alone() -> None:
    assert [judged.is_held_out(f"q{n}") for n in range(50)] == [
        judged.is_held_out(f"q{n}") for n in range(50)
    ]


def _write(tmp_path, **overrides) -> object:
    raw = {
        "version": "t",
        "depth": 5,
        "modes": {"shipped": {}},
        "questions": [{"key": "a", "stratum": "skill", "expect": "answer", "question": "kafka"}],
    }
    raw.update(overrides)
    path = tmp_path / "set.json"
    path.write_text(json.dumps(raw))
    return path


def test_a_mode_with_a_misspelt_switch_is_refused(tmp_path) -> None:
    """A typo would run the shipped search and report it as the mode."""
    path = _write(tmp_path, modes={"semantic": {"chat_semantic_candidate": True}})

    with pytest.raises(ValueError, match="unknown settings"):
        judged.load(path)


def test_a_question_expecting_something_unknown_is_refused(tmp_path) -> None:
    questions = [{"key": "a", "stratum": "s", "expect": "maybe", "question": "q"}]

    with pytest.raises(ValueError, match="expects"):
        judged.load(_write(tmp_path, questions=questions))


def test_two_questions_with_one_key_are_refused(tmp_path) -> None:
    one = {"key": "a", "stratum": "s", "expect": "answer", "question": "q"}

    with pytest.raises(ValueError, match="unique"):
        judged.load(_write(tmp_path, questions=[one, one]))


# --- searching as a mode -------------------------------------------------------------


async def test_a_mode_searches_with_its_switches_and_puts_them_back(db_session) -> None:
    made = await _corpus(db_session)
    before = (vars(R)["get_settings"], vars(R)["get_reranker"])

    with judged.searching_as(EVERYWHERE):
        everywhere = await judged.ask(db_session, "kafka jobs", 8)
    area = await judged.ask(db_session, "kafka jobs", 8)

    assert set(everywhere.ranked) == {made["here"].id, made["abroad"].id}
    assert area.ranked == (made["here"].id,)
    assert (vars(R)["get_settings"], vars(R)["get_reranker"]) == before


def test_the_switches_are_put_back_when_the_search_fails() -> None:
    before = vars(R)["get_settings"]

    with pytest.raises(RuntimeError), judged.searching_as(EVERYWHERE):
        raise RuntimeError("search failed")

    assert vars(R)["get_settings"] is before


def test_a_mode_naming_a_model_that_will_not_load_is_refused(monkeypatch) -> None:
    """Run without it, the mode would be the shipped search under its name."""
    monkeypatch.setattr(judged, "load_reranker", lambda name: None)
    broken = judged.Mode(name="broken", settings={"chat_rerank_model": "no/such-model"})

    with pytest.raises(RuntimeError, match="could not be loaded"), judged.searching_as(broken):
        pass


# --- the pool ------------------------------------------------------------------------


async def test_the_pool_is_every_mode_together_with_each_rank(db_session) -> None:
    made = await _corpus(db_session)

    pool = await judged.build_pool(db_session, _set(KAFKA))

    assert pool.surfaced == {
        ("kafka", made["here"].id): {"area": 1, "everywhere": 1},
        ("kafka", made["abroad"].id): {"everywhere": 2},
    }


async def test_a_question_expecting_nothing_pools_nothing(db_session) -> None:
    await _corpus(db_session)

    pool = await judged.build_pool(db_session, _set(ZIG))

    assert pool.surfaced == {}
    assert pool.asked[("zig", "area")].ranked == ()


async def test_writing_adds_rows_to_grade_with_no_grade(db_session) -> None:
    made = await _corpus(db_session)
    questions = _set(KAFKA)

    added = await judged.write_pool(
        db_session, questions, await judged.build_pool(db_session, questions)
    )

    rows = (await db_session.scalars(select(RetrievalJudgment))).all()
    assert added == 2 and len(rows) == 2
    here = next(row for row in rows if row.posting_id == made["here"].id)
    assert here.relevance is None
    assert (here.question, here.stratum, here.content_hash) == ("kafka jobs", "skill", "hash-here")
    assert here.held_out is KAFKA.held_out
    assert here.pool_order == judged.pool_order("test-v1", "kafka", made["here"].id)


async def test_writing_again_never_touches_a_grade(db_session) -> None:
    made = await _corpus(db_session)
    questions = _set(KAFKA)
    pool = await judged.build_pool(db_session, questions)
    await judged.write_pool(db_session, questions, pool)
    graded = await db_session.scalar(
        select(RetrievalJudgment).where(RetrievalJudgment.posting_id == made["here"].id)
    )
    graded.relevance = 2
    graded.surfaced_by = {"area": 1, "everywhere": 1}
    await db_session.flush()

    added = await judged.write_pool(db_session, questions, pool)

    await db_session.refresh(graded)
    assert added == 0
    assert graded.relevance == 2


async def test_a_posting_deleted_since_it_was_asked_about_is_not_pooled(db_session) -> None:
    made = await _corpus(db_session)
    questions = _set(KAFKA)
    pool = await judged.build_pool(db_session, questions)
    pool.surfaced[("kafka", uuid.uuid4())] = {"area": 3}

    added = await judged.write_pool(db_session, questions, pool)

    assert added == 2
    assert {row.posting_id for row in await db_session.scalars(select(RetrievalJudgment))} == {
        made["here"].id,
        made["abroad"].id,
    }
