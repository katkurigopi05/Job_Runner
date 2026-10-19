"""Scoring the assistant's search against the owner's grades (Phase 5).

The arithmetic first, on rankings written by hand, then the whole path on
two search modes that differ only in the search area, so no model is loaded.
"""

from __future__ import annotations

import sys
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from packages.core.config import get_settings
from packages.core.models import Company, Posting
from packages.core.models_judgments import RetrievalJudgment
from packages.matching import judged, judged_scores
from packages.matching.judged_scores import score_ranking

A, B, C, D = (uuid.UUID(int=n) for n in range(1, 5))
NOW = datetime.now(UTC)


# --- the arithmetic ------------------------------------------------------------------


def test_a_perfect_answer_scores_one_where_one_is_possible() -> None:
    got = score_ranking([A, B], {A: 2, B: 1}, depth=8)

    assert got["reciprocal_rank"] == 1.0
    assert got["recall_at_5"] == got["recall_at_depth"] == 1.0
    assert got["ndcg_at_depth"] == pytest.approx(1.0)
    assert got["precision_at_5"] == pytest.approx(2 / 5), "over five, however many came back"


def test_a_result_nobody_graded_counts_as_not_relevant() -> None:
    got = score_ranking([C, A], {A: 2}, depth=8)

    assert got["reciprocal_rank"] == 0.5
    assert got["precision_at_5"] == pytest.approx(1 / 5)


def test_the_grade_itself_orders_ndcg() -> None:
    better = score_ranking([A, B], {A: 2, B: 1}, depth=8)["ndcg_at_depth"]
    worse = score_ranking([B, A], {A: 2, B: 1}, depth=8)["ndcg_at_depth"]

    assert better is not None and worse is not None
    assert better > worse


def test_a_relevant_posting_past_the_depth_is_a_miss() -> None:
    got = score_ranking([C, D, A], {A: 2}, depth=2)

    assert got["recall_at_depth"] == 0.0 and got["reciprocal_rank"] == 0.0


def test_a_question_with_nothing_relevant_is_not_scored_at_all() -> None:
    """0 or 1 would each move the mean for no reason."""
    assert set(score_ranking([A], {A: 0}, depth=8).values()) == {None}


# --- the whole path ------------------------------------------------------------------


AREA = judged.Mode(name="area", settings={})
EVERYWHERE = judged.Mode(name="everywhere", settings={"search_us_only": False})
KAFKA = judged.Question(key="kafka", question="kafka jobs", stratum="skill", expect="answer")
ZIG = judged.Question(key="zig", question="roles using Zig?", stratum="absent", expect="none")
CHAT = judged.Question(
    key="reply", question="did the hiring manager reply?", stratum="talk", expect="not_searched"
)
QUESTIONS = judged.QuestionSet(
    version="test-v1", depth=8, modes=(AREA, EVERYWHERE), questions=(KAFKA, ZIG, CHAT)
)


@pytest.fixture(autouse=True)
def standing_area(monkeypatch):
    monkeypatch.setenv("SEARCH_US_ONLY", "true")
    monkeypatch.setenv("SEARCH_REMOTE_OUTSIDE_CALIFORNIA", "true")
    get_settings.cache_clear()
    yield
    monkeypatch.undo()
    get_settings.cache_clear()


async def _graded(session, grades: dict[str, int]) -> dict[str, Posting]:
    company = Company(name="Acme", ats_type="greenhouse")
    session.add(company)
    await session.flush()
    made = {}
    for n, (name, location) in enumerate(
        (("here", "San Francisco, CA"), ("abroad", "UK - London"), ("closed", "San Jose, CA"))
    ):
        made[name] = Posting(
            company_id=company.id,
            external_id=name,
            url=f"https://x.test/{name}",
            title="Engineer",
            location=location,
            description_raw="Kafka pipelines.",
            content_hash="h1",
            first_seen_at=NOW - timedelta(minutes=n),
        )
    session.add_all(made.values())
    await session.flush()
    pool = await judged.build_pool(session, QUESTIONS)
    await judged.write_pool(session, QUESTIONS, pool)
    for name, grade in grades.items():
        row = await session.scalar(
            select(RetrievalJudgment).where(RetrievalJudgment.posting_id == made[name].id)
        )
        row.relevance = grade
    await session.flush()
    return made


def _one(scores, mode: str, key: str) -> judged_scores.QuestionScore:
    return next(s for s in scores if s.mode == mode and s.key == key)


async def test_each_mode_is_scored_against_the_same_grades(db_session) -> None:
    await _graded(db_session, {"here": 2, "abroad": 0, "closed": 1})

    scores = await judged_scores.score(db_session, QUESTIONS)

    area, everywhere = _one(scores, "area", "kafka"), _one(scores, "everywhere", "kafka")
    assert area.reciprocal_rank == everywhere.reciprocal_rank == 1.0
    assert area.relevant == everywhere.relevant == 2
    assert everywhere.returned == 3 and everywhere.judged == 3


async def test_a_relevant_posting_that_closed_is_gone_not_missed(db_session) -> None:
    made = await _graded(db_session, {"here": 2, "abroad": 0, "closed": 1})
    made["closed"].closed_at = NOW
    await db_session.flush()

    scores = await judged_scores.score(db_session, QUESTIONS)

    kafka = _one(scores, "area", "kafka")
    assert (kafka.relevant, kafka.gone) == (1, 1)
    assert kafka.recall_at_depth == 1.0


async def test_a_question_expecting_nothing_is_right_when_nothing_comes_back(
    db_session,
) -> None:
    await _graded(db_session, {"here": 2, "abroad": 0, "closed": 1})

    scores = await judged_scores.score(db_session, QUESTIONS)

    assert _one(scores, "area", "zig").correct is True
    assert _one(scores, "area", "reply").correct is True
    assert _one(scores, "area", "reply").attempted is False


async def test_the_summary_keeps_the_splits_apart(db_session, monkeypatch) -> None:
    await _graded(db_session, {"here": 2, "abroad": 0, "closed": 1})
    scores = await judged_scores.score(db_session, QUESTIONS)

    dev = judged_scores.summarise(scores, "dev")
    held = judged_scores.summarise(scores, "held_out")
    everything = judged_scores.summarise(scores, "all")

    every_key = {s.key for s in scores}
    assert {r.mode for r in everything} == {"area", "everywhere"}
    for rows, held_out in ((dev, False), (held, True)):
        keys = {s.key for s in scores if s.held_out is held_out}
        for row in rows:
            assert row.abstention_asked == sum(1 for k in keys if k in {"zig", "reply"})
    assert sum(r.abstention_asked for r in dev + held if r.mode == "area") == 2
    assert every_key == {"kafka", "zig", "reply"}
    area = next(r for r in everything if r.mode == "area")
    assert area.means["reciprocal_rank"] == 1.0
    assert area.abstained_right == 2
    assert area.coverage == 1.0


async def test_no_baseline_is_frozen_until_every_posting_is_graded(
    committing_sessionmaker, worker_session, monkeypatch, capsys
) -> None:
    from scripts import score_judgments

    await _graded(worker_session, {"here": 2})
    await worker_session.commit()
    monkeypatch.setattr(score_judgments, "get_sessionmaker", lambda: committing_sessionmaker)
    monkeypatch.setattr(score_judgments.judged, "load", lambda _path: QUESTIONS)
    monkeypatch.setattr(sys, "argv", ["score_judgments", "--freeze"])

    assert await score_judgments.main() == 2
    assert "1 of 3 graded" in capsys.readouterr().out


async def test_a_frozen_baseline_says_who_gave_its_grades(
    committing_sessionmaker, worker_session, monkeypatch, tmp_path, capsys
) -> None:
    """The owner asked Claude to grade most of the first set. A baseline
    that did not say so would pass Claude's judgement off as the owner's."""
    import json

    from scripts import score_judgments

    await _graded(worker_session, {"here": 2, "abroad": 0, "closed": 1})
    claude_graded = await worker_session.scalar(
        select(RetrievalJudgment).where(RetrievalJudgment.relevance == 0)
    )
    claude_graded.note = "claude: a posting abroad"
    await worker_session.commit()
    monkeypatch.setenv("STORAGE_ROOT", str(tmp_path))
    get_settings.cache_clear()
    monkeypatch.setattr(score_judgments, "get_sessionmaker", lambda: committing_sessionmaker)
    monkeypatch.setattr(score_judgments.judged, "load", lambda _path: QUESTIONS)
    monkeypatch.setattr(score_judgments.judged, "DEFAULT_SET", tmp_path / "set.json")
    (tmp_path / "set.json").write_text("{}")
    monkeypatch.setattr(sys, "argv", ["score_judgments", "--freeze"])

    assert await score_judgments.main() == 0

    assert "2 by the owner, 1 by Claude" in capsys.readouterr().out
    (written,) = (tmp_path / "evaluation").glob("test-v1-baseline-*.json")
    record = json.loads(written.read_text())
    assert record["graded_by"] == {"owner": 2, "claude": 1}
    assert record["thresholds_approved"]["on"] == "2026-10-19"
