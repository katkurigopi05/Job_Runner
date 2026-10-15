"""The suite drops and recreates the schema of whatever database it is given.

Nothing compared that database with the live one. `TEST_DATABASE_URL` comes
from the environment or `.env`, and one typo (`…5433/jobrunner` for
`…5433/jobrunner_test`) would have dropped every application, the submission
ledger and the registry. The advisory lock in `conftest.py` only stops two
pytest runs from sharing a database; it says nothing about which one it is.

Two things stand in the way now, and either alone refuses:

- **The live database by name.** Host, port and database compared with
  `DATABASE_URL` as the app would read it, whatever the driver and whichever
  name the loopback goes by. Checked before anything connects.
- **A mark the suite does not write.** The database's comment must read
  `jobrunner:test`. Set once, by a person or by `scripts/init-db.sql`; a mark
  the fixture wrote for itself would prove nothing.
"""

from __future__ import annotations

import inspect
import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from tests import conftest
from tests.conftest import TEST_DATABASE_MARKER, TEST_DATABASE_URL, same_database, unmarked

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize(
    ("left", "right", "same"),
    [
        (
            "postgresql://a:b@localhost:5433/jobrunner",
            "postgresql+asyncpg://c:d@127.0.0.1:5433/jobrunner",
            True,
        ),
        ("postgresql://a:b@localhost/jobrunner", "postgresql://a:b@[::1]:5432/jobrunner", True),
        (
            "postgresql://a:b@LOCALHOST:5433/jobrunner",
            "postgresql://a:b@localhost:5433/jobrunner",
            True,
        ),
        (
            "postgresql://a:b@localhost:5433/jobrunner",
            "postgresql://a:b@localhost:5433/jobrunner_test",
            False,
        ),
        (
            "postgresql://a:b@localhost:5433/jobrunner",
            "postgresql://a:b@localhost:5432/jobrunner",
            False,
        ),
        (
            "postgresql://a:b@db.lan:5433/jobrunner",
            "postgresql://a:b@localhost:5433/jobrunner",
            False,
        ),
    ],
)
def test_one_database_is_recognised_whatever_the_driver_or_loopback_name(
    left: str, right: str, same: bool
) -> None:
    assert same_database(left, right) is same


def test_a_run_aimed_at_the_live_database_stops_before_anything_connects() -> None:
    """Run as the mistake would be made: both variables naming one database.

    Port 1 has nothing listening, and collection connects to nothing, so a
    guard that failed here could not reach a database either.
    """
    url = "postgresql+asyncpg://someone:not-a-real-secret@127.0.0.1:1/jobrunner_probe"
    env = {**os.environ, "DATABASE_URL": url, "TEST_DATABASE_URL": url}

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            "-p",
            "no:cacheprovider",
            "tests/test_queue.py",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    output = result.stdout + result.stderr

    assert result.returncode != 0, output
    assert "127.0.0.1:1/jobrunner_probe" in output
    assert "not-a-real-secret" not in output
    assert "someone" not in output


async def test_a_database_without_the_mark_is_refused() -> None:
    """The maintenance database is on every server and carries no mark."""
    other = make_url(TEST_DATABASE_URL).set(database="postgres")
    engine = create_async_engine(other)
    try:
        async with engine.connect() as conn:
            problem = await unmarked(conn)
    finally:
        await engine.dispose()

    assert problem is not None
    assert problem.startswith(f"{other.host}:{other.port or 5432}/postgres ")
    assert f"COMMENT ON DATABASE postgres IS '{TEST_DATABASE_MARKER}'" in problem
    # The credentials as the URL carries them. The password alone can be a
    # word every database name here contains, and so can the user.
    assert f"{other.username}:{other.password}@" not in problem
    assert "@" not in problem


async def test_the_database_this_run_owns_carries_the_mark(engine) -> None:
    async with engine.connect() as conn:
        assert await unmarked(conn) is None


def test_the_mark_is_read_before_the_schema_is_dropped() -> None:
    """Read from the source, as the status stream's commit ordering is:
    no runtime assertion can tell a check that ran first from one that ran
    after the damage."""
    source = inspect.getsource(conftest.engine)

    assert "unmarked(" in source
    assert source.index("unmarked(") < source.index("drop_all")


@pytest.mark.parametrize("path", ["scripts/init-db.sql", ".github/workflows/ci.yml"])
def test_every_place_that_creates_the_test_database_marks_it(path: str) -> None:
    assert f"COMMENT ON DATABASE jobrunner_test IS '{TEST_DATABASE_MARKER}'" in (
        ROOT / path
    ).read_text(encoding="utf-8")
