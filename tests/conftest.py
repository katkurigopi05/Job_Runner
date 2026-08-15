"""Shared fixtures.

Tests that touch the database use a real Postgres (the schema leans on JSONB,
UUID, and pgvector, so SQLite is not a substitute). When no database is
reachable those tests skip rather than fail, so `pytest` is green on a fresh
checkout before `docker compose up`.

Two isolation strategies live here, and the difference matters:

- `db_session` wraps one connection in a transaction that is always rolled
  back. Fast, and what unit tests use.
- `client` / `worker_session` commit for real, because the API and the worker
  are separate sessions that must see each other's writes. Those tests clean
  up by truncating.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from packages.core.models import Application, Base, Candidate, Profile, User

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://jobrunner:jobrunner@localhost:5432/jobrunner_test",
)

#: Truncated between committing tests, children first is handled by CASCADE.
_ALL_TABLES = (
    "application_events",
    "inbound_messages",
    "applications",
    "matches",
    "projects",
    "queue_tasks",
    "profiles",
    "resumes",
    "postings",
    "candidates",
    "companies",
    "users",
)


#: Set by `make gate-0`. Turns "no database, skip" into a hard failure — a
#: gate that passes because 56 tests silently skipped is worse than no gate.
REQUIRE_DB = os.environ.get("REQUIRE_DB") == "1"


@pytest_asyncio.fixture(scope="session")
async def engine():
    eng = create_async_engine(TEST_DATABASE_URL)
    try:
        async with eng.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    except Exception as exc:  # noqa: BLE001 - any connection failure means skip
        await eng.dispose()
        message = (
            f"no database at {TEST_DATABASE_URL}: {exc}\n"
            "Start one with `make up`, or point TEST_DATABASE_URL elsewhere."
        )
        if REQUIRE_DB:
            pytest.fail(message, pytrace=False)
        pytest.skip(message)
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


# --------------------------------------------------------------------------
# Committing fixtures — for tests that span the API and the worker
# --------------------------------------------------------------------------


@pytest_asyncio.fixture
async def committing_sessionmaker(engine) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Sessions that really commit, with a truncate afterwards."""
    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield maker
    finally:
        async with engine.begin() as conn:
            await conn.execute(text(f"TRUNCATE {', '.join(_ALL_TABLES)} RESTART IDENTITY CASCADE"))


@pytest_asyncio.fixture
async def worker_session(committing_sessionmaker) -> AsyncIterator[AsyncSession]:
    async with committing_sessionmaker() as session:
        yield session


@pytest.fixture(autouse=True)
def _tmp_storage(tmp_path):
    """Every test writes to its own storage root, never the repo's."""
    from packages.core.storage import LocalStorage, set_storage

    set_storage(LocalStorage(tmp_path / "storage"))
    yield
    set_storage(None)


@pytest_asyncio.fixture
async def client(committing_sessionmaker, monkeypatch) -> AsyncIterator[AsyncClient]:
    """HTTP client bound to the real app, talking to the test database."""
    import packages.core.db as core_db
    from apps.api.main import app

    monkeypatch.setattr(core_db, "get_sessionmaker", lambda: committing_sessionmaker)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


#: Minimal parseable résumé for fixtures.
_SAMPLE_RESUME = b"""Ada Lovelace
ada@example.com | +1 (555) 555-0100

Summary
Backend engineer.

Experience
Staff Engineer, Analytical Engines Ltd

Skills
Python, PostgreSQL
"""


async def _attach_resume(client: AsyncClient, candidate_id: str, profile_id: str) -> None:
    """Upload a résumé and make it the profile's base.

    Required since the completeness gate learned that every ATS form has a
    mandatory résumé field.
    """
    import io

    created = await client.post(
        "/resumes",
        data={"candidate_id": candidate_id},
        files={"file": ("resume.txt", io.BytesIO(_SAMPLE_RESUME), "text/plain")},
    )
    assert created.status_code == 201, created.text
    linked = await client.post(
        f"/resumes/{created.json()['id']}/set-base", params={"profile_id": profile_id}
    )
    assert linked.status_code == 200, linked.text


@pytest_asyncio.fixture
async def bare_candidate(client: AsyncClient) -> dict[str, str]:
    """Profile fields all filled, but no résumé attached.

    For tests about the résumé requirement itself.
    """
    suffix = uuid.uuid4().hex[:8]
    cand = await client.post(
        "/candidates", json={"name": "Bare Owner", "email": f"bare-{suffix}@example.com"}
    )
    assert cand.status_code == 201, cand.text
    prof = await client.post(
        "/profiles",
        json={
            "candidate_id": cand.json()["id"],
            "label": "bare",
            "phone": "+1-555-0100",
            "location": "Austin, TX",
            "work_auth": "US citizen",
            "needs_sponsorship": False,
        },
    )
    assert prof.status_code == 201, prof.text
    return {"candidate_id": cand.json()["id"], "profile_id": prof.json()["id"]}


@pytest_asyncio.fixture
async def complete_candidate(client: AsyncClient) -> dict[str, str]:
    """A candidate + profile that passes the completeness gate."""
    suffix = uuid.uuid4().hex[:8]
    cand = await client.post(
        "/candidates",
        json={"name": "Test Owner", "email": f"owner-{suffix}@example.com"},
    )
    assert cand.status_code == 201, cand.text
    candidate_id = cand.json()["id"]

    prof = await client.post(
        "/profiles",
        json={
            "candidate_id": candidate_id,
            "label": "backend",
            "phone": "+1-555-0100",
            "location": "Austin, TX",
            "work_auth": "US citizen",
            "needs_sponsorship": False,
        },
    )
    assert prof.status_code == 201, prof.text
    profile_id = prof.json()["id"]
    await _attach_resume(client, candidate_id, profile_id)
    return {"candidate_id": candidate_id, "profile_id": profile_id}


@pytest_asyncio.fixture
async def auto_submit_candidate(client: AsyncClient) -> dict[str, str]:
    """A complete candidate whose profile has opted in to auto-submit.

    Both halves of the gate must be on before anything submits: the global
    AUTO_SUBMIT and this per-profile flag. CLAUDE.md §2.3.
    """
    suffix = uuid.uuid4().hex[:8]
    cand = await client.post(
        "/candidates",
        json={"name": "Auto Owner", "email": f"auto-{suffix}@example.com"},
    )
    assert cand.status_code == 201, cand.text
    candidate_id = cand.json()["id"]

    prof = await client.post(
        "/profiles",
        json={
            "candidate_id": candidate_id,
            "label": "auto",
            "phone": "+1-555-0100",
            "location": "Austin, TX",
            "work_auth": "US citizen",
            "needs_sponsorship": False,
            "auto_submit": True,
        },
    )
    assert prof.status_code == 201, prof.text
    profile_id = prof.json()["id"]
    await _attach_resume(client, candidate_id, profile_id)
    return {"candidate_id": candidate_id, "profile_id": profile_id}
