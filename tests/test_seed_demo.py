"""The reviewer fixture is repeatable and uses real review/tracker contracts."""

from sqlalchemy import func, select

from packages.core.models import Application, Candidate, Match, Profile, Project, QueueTask
from packages.core.storage import get_storage
from scripts.seed_demo import demo_id, seed


async def test_seed_demo_is_idempotent_and_preserves_review_decisions(db_session):
    first = await seed(db_session)
    candidate = await db_session.get(Candidate, demo_id("candidate"))
    assert candidate.name == "Jordan Chen"
    candidate.name = "Reviewer edited fixture"
    second = await seed(db_session)
    assert first["created"] > 0 and second["created"] == 0
    assert candidate.name == "Reviewer edited fixture"
    assert await db_session.scalar(select(func.count()).select_from(Match)) == 10
    assert await db_session.scalar(select(func.count()).select_from(Project)) == 3
    assert await db_session.scalar(select(func.count()).select_from(QueueTask)) == 0
    applications = (await db_session.scalars(select(Application))).all()
    assert len(applications) == 5
    assert {a.outcome for a in applications if a.status == "submitted"} == {
        "interview",
        "assessment",
        "awaiting",
        "rejected",
    }
    waiting = next(a for a in applications if a.status == "needs_review")
    assert waiting.review_json["resume_diff"]["changed"] == 1
    assert waiting.review_json["unanswered"][0]["required"] is True
    assert waiting.tailored_resume_id
    profile = await db_session.get(Profile, waiting.profile_id)
    assert profile.auto_submit is False
    assert all(a.url.startswith("https://demo.invalid/") for a in applications)
    assert get_storage().get("demo/base.pdf").startswith(b"%PDF")
