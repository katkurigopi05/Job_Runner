"""Historical applications must be protected when the ledger is introduced."""

import os
import subprocess

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from packages.core.enums import ApplicationStatus
from packages.core.models import Application, AppliedPosting, QueueTask
from packages.core.state import transition
from tests.test_migration_posting_dedupe import (  # noqa: F401
    REPO_ROOT,
    _alembic,
    scratch_database,
)
from tests.test_submission_ledger import _application


async def test_backfill_preserves_history_and_protects_old_attempts(scratch_database):  # noqa: F811
    _alembic("14ebc80a6ecb", scratch_database)
    engine = create_async_engine(scratch_database)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        sent_id = await _application(maker)
        interrupted_id = await _application(maker)
        rolled_back_id = await _application(maker)
        untouched_id = await _application(maker)
        async with maker() as session:
            sent = await session.get(Application, sent_id)
            await transition(session, sent, ApplicationStatus.SUBMITTED)
            duplicate = Application(
                candidate_id=sent.candidate_id,
                profile_id=sent.profile_id,
                url="https://job-boards.greenhouse.io/acme/jobs/123?utm_source=mail",
            )
            session.add(duplicate)
            await session.flush()
            await transition(session, duplicate, ApplicationStatus.RUNNING)
            await transition(session, duplicate, ApplicationStatus.SUBMITTED)
            # Simulate state rolled back by an old worker, while its task
            # claim survived in a separate transaction. The untouched queued
            # application must NOT receive a speculative fence.
            await session.execute(
                text("UPDATE applications SET status = 'queued' WHERE id IN (:old, :new)"),
                {"old": rolled_back_id, "new": untouched_id},
            )
            session.add(
                QueueTask(
                    kind="apply", payload_json={"application_id": str(rolled_back_id)}, attempts=1
                )
            )
            await session.commit()
        _alembic("head", scratch_database)
        _alembic("head", scratch_database)
        async with maker() as session:
            fences = (await session.scalars(select(AppliedPosting))).all()
            assert len(fences) == 3
            assert (await session.get(AppliedPosting, sent_id)).state == "submitted"
            assert (await session.get(AppliedPosting, interrupted_id)).state == "uncertain"
            assert (await session.get(AppliedPosting, rolled_back_id)).state == "uncertain"
            assert await session.get(AppliedPosting, untouched_id) is None
            apps = (await session.scalars(select(Application))).all()
            assert len(apps) == 5
            assert sorted(app.status for app in apps) == [
                "queued",
                "queued",
                "running",
                "submitted",
                "submitted",
            ]
        # A rollback to the preceding code keeps all protection. Going back
        # beyond the table must fail, not erase durable submission evidence.
        result = _downgrade("14ebc80a6ecb", scratch_database)
        assert result.returncode == 0, result.stderr
        result = _downgrade("e5f7a9b1c2d4", scratch_database)
        assert result.returncode != 0
        assert "refusing to drop nonempty submission ledger" in result.stderr
        async with maker() as session:
            assert len((await session.scalars(select(AppliedPosting))).all()) == 3
        _alembic("head", scratch_database)
        async with maker() as session:
            assert len((await session.scalars(select(AppliedPosting))).all()) == 3
    finally:
        await engine.dispose()


def _downgrade(target, database_url):
    return subprocess.run(
        [str(REPO_ROOT / ".venv/bin/alembic"), "downgrade", target],
        cwd=REPO_ROOT,
        env={**os.environ, "DATABASE_URL": database_url},
        capture_output=True,
        text=True,
        timeout=60,
    )
