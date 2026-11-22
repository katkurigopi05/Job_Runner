"""Seed one credential-free synthetic application and route its interview invitation."""

from __future__ import annotations

import asyncio

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.db import get_sessionmaker
from packages.core.enums import Classification
from packages.core.models import (
    Application,
    Candidate,
    Company,
    Posting,
    Profile,
    Project,
    Resume,
    User,
)
from packages.inbox.alias import alias_for
from packages.inbox.classify import ClassificationResult
from packages.inbox.route import InboundEmail, route_message
from packages.tailor.parse import parse_text
from scripts.seed_demo import demo_id

BODY = (
    "We'd like to invite you to interview for the new-grad Applied AI Engineer role.\n"
    "Round 1: video interview on 2026-12-03 at 14:00 UTC for 45 minutes.\n"
    "Topics: Python, model evaluation"
)
DESCRIPTION = (
    "Requirements:\n"
    "- Experience with Python.\n"
    "- Explain model evaluation and write regression tests.\n"
    "- Experience operating Kubernetes in production."
)


async def seed(session: AsyncSession, *, commit: bool = True) -> Application:
    await session.execute(text("SELECT pg_advisory_xact_lock(731942117)"))

    async def add(model, record_key, **values):
        identifier = demo_id("interview:" + record_key)
        row = await session.get(model, identifier)
        if row is None:
            row = model(id=identifier, **values)
            session.add(row)
            await session.flush()
        return row

    user = await add(User, "owner", email="interview-owner@example.com")
    candidate = await add(
        Candidate,
        "candidate",
        user_id=user.id,
        name="Sam Interview Fixture",
        email="interview-candidate@example.com",
    )
    parsed = parse_text(
        "Sam Interview Fixture\nExperience\n"
        "Built Python tests for a course-data pipeline.\n"
        "Projects\nImplemented a small model evaluation harness.\n"
        "Education\nCoursework in machine learning and statistics."
    ).model_dump(mode="json")
    resume = await add(
        Resume,
        "resume",
        candidate_id=candidate.id,
        version=2,
        parsed_json=parsed,
        storage_ref="demo/interview/resume.txt",
    )
    profile = await add(
        Profile,
        "profile",
        candidate_id=candidate.id,
        label="Synthetic interview demo",
        base_resume_id=resume.id,
    )
    company = await add(
        Company,
        "company",
        name="Interview Demo Works",
        domain="interview-demo.example.com",
    )
    posting = await add(
        Posting,
        "posting",
        company_id=company.id,
        url="https://interview-demo.example.com/jobs/applied-ai",
        title="New-grad Applied AI Engineer",
        location="Austin, TX",
        description_raw=DESCRIPTION,
    )
    app = await add(
        Application,
        "application",
        candidate_id=candidate.id,
        profile_id=profile.id,
        posting_id=posting.id,
        url=posting.url,
        status="submitted",
        receipt_json={"interview_fixture": True},
        submission_sources={
            "resume": {"id": str(resume.id), "version": resume.version, "parsed": parsed},
            "posting": {
                "id": str(posting.id),
                "version": 1,
                "description": DESCRIPTION,
                "company": company.name,
                "company_domain": company.domain,
                "role": posting.title,
                "location": posting.location,
            },
        },
    )
    await add(
        Project,
        "project",
        candidate_id=candidate.id,
        source="github",
        external_id="interview-fixture-project",
        name="Eval Cards",
        full_name="interview-fixture/eval-cards",
        url="https://github.com/interview-fixture/eval-cards",
        language="Python",
        description="Implemented Python regression tests for an offline model evaluation harness.",
        dependency_technologies=["pytest"],
    )
    await route_message(
        session,
        InboundEmail(
            message_id="<interview-fixture-1@example.com>",
            from_addr="recruiter@example.com",
            to_addr=alias_for(candidate.email, app.id),
            subject="Interview invitation: Round 1",
            body=BODY,
        ),
        result=ClassificationResult(classification=Classification.INTERVIEW),
    )
    if commit:
        await session.commit()
    return app


async def main() -> None:
    async with get_sessionmaker()() as session:
        app = await seed(session)
    print("Synthetic interview invite routed. Open Tracker → Prepare for interview.")
    print(f"/applications/{app.id}/interview")


if __name__ == "__main__":
    asyncio.run(main())
