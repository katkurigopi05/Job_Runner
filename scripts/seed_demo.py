"""Seed synthetic reviewer data, atomically and idempotently.

Run after migrations: python -m scripts.seed_demo. No network LLM, crawler,
GitHub request or worker task is started. Stable UUIDs isolate demo records;
reruns preserve reviewer edits and application decisions.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import uuid
from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.db import get_engine, get_sessionmaker
from packages.core.enums import ApplicationStatus
from packages.core.models import (
    Application,
    Candidate,
    Company,
    Match,
    Posting,
    Profile,
    Project,
    Resume,
    User,
)
from packages.core.state import transition
from packages.core.storage import get_storage
from packages.matching.embed import LexicalEmbedder
from packages.tailor.parse import parse_text

NAMESPACE = uuid.UUID("d9d8f1e0-e870-4d4b-9680-693e3ba29c63")


def demo_id(label: str) -> uuid.UUID:
    return uuid.uuid5(NAMESPACE, label)


RESUME = """Jordan Chen
jordan@example.com | +1 (202) 555-0142 | San Francisco, CA
Summary
Software engineer building Python APIs, grounded AI assistants, and cloud infrastructure.
Experience
Software Engineering Intern | Harbor Labs | Jun 2025 - Aug 2025
Built FastAPI endpoints backed by PostgreSQL for an internal inventory dashboard.
Added pytest integration checks and GitHub Actions workflows for API releases.
Cloud Engineering Intern | Beacon Systems | Jun 2024 - Aug 2024
Provisioned AWS development environments using Terraform and Docker.
Instrumented service health checks with OpenTelemetry for incident triage.
Projects
Agent Workbench
Built a Python retrieval assistant with source citations and evaluation fixtures.
Cloud Launchpad
Created Terraform modules and GitHub Actions pipelines for container services.
API Contract Kit
Built TypeScript contract tests for a Next.js dashboard and FastAPI backend.
Skills
Python, TypeScript, SQL, FastAPI, PostgreSQL, Redis, Docker, AWS, Terraform, pytest
Education
B.S. Computer Science | Example State University | Aug 2022 - May 2026
Certifications
AWS Certified Cloud Practitioner | 2025
HashiCorp Certified: Terraform Associate | 2025
"""
ORIGINAL = "Built FastAPI endpoints backed by PostgreSQL for an internal inventory dashboard."
TAILORED = "Built PostgreSQL-backed FastAPI endpoints for an internal inventory dashboard."
JOBS = [
    ("Harbor Labs", "Software Engineer, Backend", "Python FastAPI PostgreSQL pytest API services"),
    (
        "Beacon Systems",
        "AI Agent Engineer",
        "Python retrieval RAG agent tools evaluation source citations",
    ),
    (
        "Summit Cloud",
        "Cloud Infrastructure Engineer",
        "AWS Terraform Docker observability GitHub Actions",
    ),
    (
        "Cedar Analytics",
        "Forward Deployed Engineer",
        "Python SQL customer discovery integrations API delivery",
    ),
    (
        "Orbit Tools",
        "Developer Tools Engineer",
        "TypeScript contract tests Next.js developer experience",
    ),
    ("Atlas Compute", "Platform Engineer", "Docker PostgreSQL Terraform incident response"),
    (
        "Lighthouse AI",
        "AI Evaluation Engineer",
        "Python pytest retrieval evaluation grounded answers",
    ),
    (
        "Meadow Networks",
        "Software Engineer, Full Stack",
        "TypeScript Next.js FastAPI PostgreSQL dashboards",
    ),
    ("Bridge Data", "Solutions Engineer", "SQL Python cloud integrations technical discovery"),
    (
        "Granite Infra",
        "Site Reliability Engineer",
        "AWS Docker OpenTelemetry service health automation",
    ),
]


async def seed(session: AsyncSession) -> dict[str, int]:
    await session.execute(text("SELECT pg_advisory_xact_lock(731942109)"))
    stats = {"created": 0, "existing": 0}

    async def add(model, record_key: str, **values):
        found = await session.get(model, demo_id(record_key))
        if found is not None:
            stats["existing"] += 1
            return found
        row = model(id=demo_id(record_key), **values)
        session.add(row)
        await session.flush()
        stats["created"] += 1
        return row

    user = await add(User, "user", email="demo-owner@example.com")
    candidate = await add(
        Candidate, "candidate", user_id=user.id, name="Jordan Chen", email="jordan@example.com"
    )
    parsed = parse_text(RESUME)
    tailored = parse_text(RESUME.replace(ORIGINAL, TAILORED))
    storage = get_storage()
    # Valid PDFs use the same renderer as real resumes; no fabricated extension.
    from packages.tailor.assemble import assemble_pdf

    for label, document in [("base", parsed), ("tailored", tailored)]:
        key = f"demo/{label}.pdf"
        if not storage.exists(key):
            storage.put(key, assemble_pdf(document))
    base = await add(
        Resume,
        "resume",
        candidate_id=candidate.id,
        storage_ref="demo/base.pdf",
        parsed_json=parsed.model_dump(mode="json"),
        is_default=True,
    )
    profile = await add(
        Profile,
        "profile",
        candidate_id=candidate.id,
        label="Demo · SWE / AI / Cloud",
        base_resume_id=base.id,
        phone="+1 (202) 555-0142",
        location="San Francisco, CA",
        work_auth="Authorized to work in the United States",
        needs_sponsorship=False,
        auto_submit=False,
        links_json={"github": "https://github.com/example-dev"},
    )
    for i, (name, language, topics) in enumerate(
        [
            ("agent-workbench", "Python", ["retrieval", "evaluation", "fastapi"]),
            ("cloud-launchpad", "HCL", ["terraform", "aws", "docker"]),
            ("api-contract-kit", "TypeScript", ["nextjs", "testing", "api"]),
        ]
    ):
        await add(
            Project,
            f"project-{i}",
            candidate_id=candidate.id,
            source="github",
            external_id=f"demo-{i}",
            name=name,
            full_name=f"example-dev/{name}",
            url=f"https://github.com/example-dev/{name}",
            language=language,
            description="Synthetic demo project; imported metadata fixture, not a live repository.",
            topics_json=topics,
            include=True,
            pinned=i == 0,
            synced_at=datetime.now(UTC),
        )
    embedder = LexicalEmbedder()
    for i, (company_name, title, terms) in enumerate(JOBS):
        company = await add(
            Company,
            f"company-{i}",
            name=f"{company_name} (Demo)",
            domain="demo.invalid",
            careers_url=f"https://demo.invalid/company/{i}",
            ats_type="greenhouse",
        )
        description = (
            f"{title} at {company_name} — synthetic reviewer fixture. "
            f"Build and maintain {terms}. Collaborate with engineers and customers, "
            "write integration tests, document design decisions, and own reliable releases. "
            "Requirements: 0-2 years of experience or equivalent project work. "
            "Location: San Francisco, California, United States; remote within the US. "
            "Salary: $110,000-$150,000 USD per year. This is not a live vacancy."
        )
        posting = await add(
            Posting,
            f"posting-{i}",
            company_id=company.id,
            ats_type="greenhouse",
            external_id=f"demo-{i}",
            url=f"https://demo.invalid/jobs/{i}",
            title=title,
            location="San Francisco, CA, United States (Remote)",
            country_code="US",
            state_code="CA",
            is_remote=True,
            description_raw=description,
            content_hash=hashlib.sha256(description.encode()).hexdigest(),
            description_embedding=embedder.encode([description])[0],
            embedding_model=embedder.name,
            salary_min=110000,
            salary_max=150000,
            salary_currency="USD",
            salary_period="year",
            last_seen_at=datetime.now(UTC),
        )
        await add(
            Match,
            f"match-{i}",
            profile_id=profile.id,
            posting_id=posting.id,
            score=round(0.94 - i * 0.025, 3),
            reasons_json={
                "matched_terms": terms.split()[:4],
                "missing_terms": [],
                "title_similarity": 0.85,
                "body_similarity": 0.9,
                "demo": "Illustrative fixture score, not a measured retrieval result",
            },
        )
        if i > 4:
            continue
        app_id = demo_id(f"application-{i}")
        if await session.get(Application, app_id) is not None:
            continue
        application = await add(
            Application,
            f"application-{i}",
            candidate_id=candidate.id,
            profile_id=profile.id,
            posting_id=posting.id,
            url=posting.url,
            ats="greenhouse",
            status="queued",
        )
        await transition(session, application, ApplicationStatus.RUNNING)
        if i == 0:
            document = await add(
                Resume,
                "tailored-resume",
                candidate_id=candidate.id,
                storage_ref="demo/tailored.pdf",
                parsed_json=tailored.model_dump(mode="json"),
                tailored_for_posting_id=posting.id,
                tailored_by="synthetic-demo",
            )
            application.tailored_resume_id = document.id
            application.review_json = {
                "demo": True,
                "filled": [
                    {"label": "Full name", "value": "Jordan Chen"},
                    {"label": "Email", "value": "jordan@example.com"},
                ],
                "unanswered": [
                    {
                        "key": "customer_story",
                        "question": "Describe a customer problem you helped resolve.",
                        "kind": "textarea",
                        "required": True,
                    }
                ],
                "resume_diff": {
                    "changed": 1,
                    "unchanged": 5,
                    "rejected": 0,
                    "changes": [{"original": ORIGINAL, "tailored": TAILORED, "inline_html": ""}],
                    "unified": f"- {ORIGINAL}\n+ {TAILORED}",
                },
            }
            await transition(session, application, ApplicationStatus.NEEDS_REVIEW)
        else:
            await transition(
                session,
                application,
                ApplicationStatus.SUBMITTED,
                payload={"demo": True, "note": "Synthetic event; nothing was sent"},
            )
            application.outcome = ["interview", "assessment", "awaiting", "rejected"][i - 1]
            application.outcome_at = datetime.now(UTC)
    return stats


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args(argv)
    try:
        async with get_sessionmaker()() as session, session.begin():
            stats = await seed(session)
        print(
            f"Demo ready: {stats['created']} created, "
            f"{stats['existing']} existing records preserved."
        )
        print(
            "Jordan Chen · 10 matches · 1 review · 4 tracked outcomes. "
            "All job URLs use demo.invalid."
        )
        return 0
    finally:
        await get_engine().dispose()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
