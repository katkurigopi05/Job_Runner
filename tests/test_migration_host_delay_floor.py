"""`c4e6a8b0d2f4` lowers the hosts stored at the old 60 s floor, and only those.

The stored delay only ratchets up, so without this migration the 5 s floor
would apply to new hosts and never to the 6,080 the owner's crawler already
knew. A site's own Crawl-delay above 60, and a shared API's 2 s, must survive.
Run through Alembic on a throwaway database, as `test_migration_posting_dedupe`
does, because the suite's schema never runs a migration.
"""

from __future__ import annotations

import subprocess

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine

from tests.test_migration_posting_dedupe import REPO_ROOT, scratch_database  # noqa: F401

BEFORE = "b7d9f1a3c5e2"
UNDER_TEST = "c4e6a8b0d2f4"

ROWS = {
    "careers.acme.example": 60.0,  # the old floor
    "boards-api.greenhouse.io": 2.0,  # a shared API at its own floor
    "slow.example": 300.0,  # a site's Crawl-delay
}


def _alembic(command: str, target: str, database_url: str) -> None:
    import os

    result = subprocess.run(
        [str(REPO_ROOT / ".venv" / "bin" / "alembic"), command, target],
        cwd=REPO_ROOT,
        env={**os.environ, "DATABASE_URL": database_url},
        capture_output=True,
        text=True,
        timeout=300,
    )
    if result.returncode != 0:
        pytest.fail(f"alembic {command} {target} failed:\n{result.stdout}\n{result.stderr}")


async def _delays(url: str) -> dict[str, float]:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            rows = await conn.execute(
                sa.text("SELECT host, delay_seconds FROM crawler_host_budgets")
            )
            return {host: float(delay) for host, delay in rows}
    finally:
        await engine.dispose()


async def test_only_hosts_at_the_old_floor_are_lowered_and_it_goes_back(scratch_database):  # noqa: F811
    _alembic("upgrade", BEFORE, scratch_database)
    engine = create_async_engine(scratch_database)
    async with engine.begin() as conn:
        for host, delay in ROWS.items():
            await conn.execute(
                sa.text(
                    "INSERT INTO crawler_host_budgets (host, next_allowed_at, delay_seconds, "
                    "updated_at) VALUES (:host, now(), :delay, now())"
                ),
                {"host": host, "delay": delay},
            )
    await engine.dispose()

    _alembic("upgrade", UNDER_TEST, scratch_database)
    assert await _delays(scratch_database) == {
        "careers.acme.example": 5.0,
        "boards-api.greenhouse.io": 2.0,
        "slow.example": 300.0,
    }

    _alembic("downgrade", BEFORE, scratch_database)
    assert await _delays(scratch_database) == ROWS
