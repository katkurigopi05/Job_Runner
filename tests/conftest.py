"""Shared fixtures.

Tests that touch the database use a real Postgres (the schema leans on JSONB,
UUID, and pgvector, so SQLite is not a substitute). When no database is
reachable those tests skip rather than fail, so `pytest` is green on a fresh
checkout before `docker compose up`.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from packages.core.models import Application, Base, Candidate, Profile, User

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://jobrunner:jobrunner@localhost:5432/jobrunner_test",
)


@pytest_asyncio.fixture(scope="session")
async def engine():
    eng = create_async_engine(TEST_DATABASE_URL)
    try:
        async with eng.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    except Exception as exc:  # noqa: BLE001 - any connection failure means skip
        await eng.dispose()
        pytest.skip(f"no database at {TEST_DATABASE_URL}: {exc}")
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def db_session(engine) -> AsyncIterator[AsyncSession]:
    """A session wrapped in a transaction that is always rolled back.

    Tests never see each other's rows and the database stays clean.
    """
    async with engine.connect() as conn:
        trans = await conn.begin()
        session = async_sessionmaker(bind=conn, expire_on_commit=False)()
        try:
            yield session
        finally:
            await session.close()
            await trans.rollback()


@pytest_asyncio.fixture
async def application(db_session: AsyncSession) -> Application:
    """A freshly queued application, with the rows it depends on."""
    suffix = uuid.uuid4().hex[:8]

    user = User(email=f"owner-{suffix}@example.com")
    db_session.add(user)
    await db_session.flush()

    candidate = Candidate(user_id=user.id, name="Test Owner", email=f"owner-{suffix}@example.com")
    db_session.add(candidate)
    await db_session.flush()

    profile = Profile(candidate_id=candidate.id, label="default")
    db_session.add(profile)
    await db_session.flush()

    app = Application(
        candidate_id=candidate.id,
        profile_id=profile.id,
        url=f"https://boards.greenhouse.io/acme/jobs/{suffix}",
        ats="greenhouse",
        status="queued",
    )
    db_session.add(app)
    await db_session.flush()
    return app
