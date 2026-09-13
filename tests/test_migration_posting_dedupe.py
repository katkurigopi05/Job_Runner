"""The `(company_id, external_id)` migration must not eat the owner's labels.

`c33a361f9611` adds a unique constraint to a table that may already hold
duplicates, so it collapses them first. That is the only deduplication in this
repository that runs against real data with no review step, and two of the four
tables pointing at `postings` cascade — `posting_labels` among them.

CLAUDE.md §15 says the matching benchmark is waiting on exactly that table:
grades the owner sat down and wrote, which no rerun can regenerate. A `DELETE`
of a duplicate row would take them along, report success, and leave nothing in
the log to read afterwards. So the collapse is tested end to end, on a throwaway
database, through Alembic itself rather than through a copy of its SQL.

The rest of the suite builds its schema with `Base.metadata.create_all`, which
never runs a migration at all. Without this file the dedupe ships unexecuted.
"""

from __future__ import annotations

import os
import subprocess
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine

from tests.conftest import REQUIRE_DB, TEST_DATABASE_URL

#: The revision immediately before the one under test. The fixture database is
#: brought up to here, filled with the duplicates a real registry could hold,
#: and only then upgraded across the boundary.
BEFORE = "afb1de709ce0"
UNDER_TEST = "c33a361f9611"

REPO_ROOT = Path(__file__).resolve().parent.parent


def _url_for(url: str, database: str) -> str:
    """The same server, pointed at another database.

    Stays on asyncpg. The synchronous drivers would read more naturally for a
    setup script, but psycopg2 is not a dependency of this project and a test
    is not a good enough reason to make it one.
    """
    parts = urlsplit(url)._replace(path=f"/{database}")
    return urlunsplit(parts)


@pytest_asyncio.fixture
async def scratch_database():
    """A real, empty database that is dropped however the test ends.

    Named per-run so a crashed previous run cannot collide with this one.
    """
    name = f"jr_migration_{uuid.uuid4().hex[:12]}"
    # CREATE DATABASE cannot run inside a transaction block.
    admin = create_async_engine(
        _url_for(TEST_DATABASE_URL, "postgres"), isolation_level="AUTOCOMMIT"
    )
    try:
        async with admin.connect() as conn:
            await conn.execute(sa.text(f'CREATE DATABASE "{name}"'))
    except Exception as exc:  # noqa: BLE001 - no database means skip, as elsewhere
        await admin.dispose()
        message = f"no database at {TEST_DATABASE_URL}: {exc}"
        if REQUIRE_DB:
            pytest.fail(message, pytrace=False)
        pytest.skip(message)

    try:
        yield _url_for(TEST_DATABASE_URL, name)
    finally:
        async with admin.connect() as conn:
            # FORCE: the Alembic subprocess may have left a connection behind,
            # and a leaked scratch database is worse than a slow teardown.
            await conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        await admin.dispose()


def _alembic(target: str, database_url: str) -> None:
    """Run one Alembic upgrade against `database_url`, failing loudly."""
    environment = {
        **os.environ,
        # env.py reads this through Settings. A real environment variable
        # outranks `.env`, so this cannot be redirected at the owner's own
        # database by a file sitting in the repo.
        "DATABASE_URL": database_url,
    }
    result = subprocess.run(
        [str(REPO_ROOT / ".venv" / "bin" / "alembic"), "upgrade", target],
        cwd=REPO_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=300,
    )
    if result.returncode != 0:
        pytest.fail(f"alembic upgrade {target} failed:\n{result.stdout}\n{result.stderr}")


async def _seed_duplicates(engine) -> dict[str, uuid.UUID]:
    """One company, one posting recorded twice, and references to both.

    The two postings share `(company_id, external_id)`, which is what the new
    constraint forbids. Between them they cover every reference shape the
    collapse has to handle:

    - a label on the *loser* only, which has to move;
    - a label on *both* for the same profile, where the loser's is the same
      judgement twice and is the only row allowed to disappear;
    - a match and an application on the loser, which have to move;
    - a fresher body on the loser, which has to end up on the survivor.
    """
    ids = {key: uuid.uuid4() for key in ("user", "candidate", "profile", "other", "keep", "lose")}

    async with engine.begin() as conn:
        await conn.execute(
            sa.text("INSERT INTO users (id, email) VALUES (:id, :email)"),
            {"id": ids["user"], "email": "owner@example.com"},
        )
        await conn.execute(
            sa.text(
                "INSERT INTO candidates (id, user_id, name, email, email_mode) "
                "VALUES (:id, :user_id, 'Owner', 'owner@example.com', 'self')"
            ),
            {"id": ids["candidate"], "user_id": ids["user"]},
        )
        for key, label in (("profile", "primary"), ("other", "secondary")):
            await conn.execute(
                sa.text(
                    "INSERT INTO profiles (id, candidate_id, label) "
                    "VALUES (:id, :candidate_id, :label)"
                ),
                {"id": ids[key], "candidate_id": ids["candidate"], "label": label},
            )

        company = uuid.uuid4()
        await conn.execute(
            sa.text(
                "INSERT INTO companies (id, name, ats_type, careers_url) VALUES "
                "(:id, 'Acme', 'greenhouse', "
                "'https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=true')"
            ),
            {"id": company},
        )
        ids["company"] = company

        # The survivor is the older row; the duplicate carries the newer body.
        #
        # Absolute timestamps rather than `now() - :offset`: Postgres infers a
        # bare parameter there as a timestamptz, and `now() - timestamptz` is
        # an interval, not a point in time.
        now = datetime.now(UTC)
        for key, title, seen in (
            ("keep", "Old Title", now - timedelta(days=2)),
            ("lose", "New Title", now - timedelta(hours=1)),
        ):
            await conn.execute(
                sa.text(
                    "INSERT INTO postings "
                    "(id, company_id, ats_type, external_id, url, title, first_seen_at) "
                    "VALUES (:id, :company, 'greenhouse', 'REQ-1', :url, :title, :seen)"
                ),
                {
                    "id": ids[key],
                    "company": company,
                    "url": f"https://example.com/{key}",
                    "title": title,
                    "seen": seen,
                },
            )

        # A grade on each row from the same profile: the collision case.
        for key in ("keep", "lose"):
            await conn.execute(
                sa.text(
                    "INSERT INTO posting_labels (id, profile_id, posting_id, relevance) "
                    "VALUES (:id, :profile, :posting, 2)"
                ),
                {"id": uuid.uuid4(), "profile": ids["profile"], "posting": ids[key]},
            )
        # A grade from a second profile, on the loser only: the move case.
        await conn.execute(
            sa.text(
                "INSERT INTO posting_labels (id, profile_id, posting_id, relevance) "
                "VALUES (:id, :profile, :posting, 3)"
            ),
            {"id": uuid.uuid4(), "profile": ids["other"], "posting": ids["lose"]},
        )
        await conn.execute(
            sa.text(
                "INSERT INTO matches (id, profile_id, posting_id, score) "
                "VALUES (:id, :profile, :posting, 0.9)"
            ),
            {"id": uuid.uuid4(), "profile": ids["profile"], "posting": ids["lose"]},
        )
        await conn.execute(
            sa.text(
                "INSERT INTO applications (id, candidate_id, profile_id, posting_id, url, status) "
                "VALUES (:id, :candidate, :profile, :posting, "
                "'https://example.com/apply', 'queued')"
            ),
            {
                "id": uuid.uuid4(),
                "candidate": ids["candidate"],
                "profile": ids["profile"],
                "posting": ids["lose"],
            },
        )
    return ids


async def test_duplicate_postings_collapse_without_losing_labels(scratch_database):
    _alembic(BEFORE, scratch_database)
    engine = create_async_engine(scratch_database)
    try:
        ids = await _seed_duplicates(engine)
        _alembic(UNDER_TEST, scratch_database)

        async with engine.connect() as conn:
            surviving = (
                await conn.execute(
                    sa.text("SELECT id, title FROM postings WHERE company_id = :c"),
                    {"c": ids["company"]},
                )
            ).all()
            assert len(surviving) == 1, "the duplicate should be gone"
            assert surviving[0].id == ids["keep"], "the older row is the survivor"
            # The duplicate was the more recently crawled of the two, so its
            # body is the current one and has to come across.
            assert surviving[0].title == "New Title"

            labels = (
                await conn.execute(
                    sa.text("SELECT profile_id, posting_id, relevance FROM posting_labels")
                )
            ).all()
            # Three went in; the two on one profile were the same judgement
            # recorded twice, so exactly one of those may be dropped.
            assert len(labels) == 2, f"a hand-written grade was lost: {labels}"
            assert {row.profile_id for row in labels} == {ids["profile"], ids["other"]}
            assert all(row.posting_id == ids["keep"] for row in labels)

            match_targets = (
                (await conn.execute(sa.text("SELECT posting_id FROM matches"))).scalars().all()
            )
            assert match_targets == [ids["keep"]]

            app_targets = (
                (await conn.execute(sa.text("SELECT posting_id FROM applications"))).scalars().all()
            )
            assert app_targets == [ids["keep"]], "an application must not be orphaned"
    finally:
        await engine.dispose()


async def test_slug_and_last_seen_at_are_backfilled(scratch_database):
    """The two backfills, checked on a row that predates the column."""
    _alembic(BEFORE, scratch_database)
    engine = create_async_engine(scratch_database)
    try:
        ids = await _seed_duplicates(engine)
        _alembic(UNDER_TEST, scratch_database)

        async with engine.connect() as conn:
            slug = (
                await conn.execute(
                    sa.text("SELECT slug FROM companies WHERE id = :c"), {"c": ids["company"]}
                )
            ).scalar_one()
            # Read off the API URL, so `v1` must not win over the real slug.
            assert slug == "acme"

            row = (
                await conn.execute(
                    sa.text("SELECT first_seen_at, last_seen_at FROM postings WHERE id = :p"),
                    {"p": ids["keep"]},
                )
            ).one()
            assert row.last_seen_at is not None, (
                "a pre-existing posting must not read as never seen"
            )
            assert row.last_seen_at == row.first_seen_at
    finally:
        await engine.dispose()
