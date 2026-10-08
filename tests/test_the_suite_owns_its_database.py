"""The suite's only database is the test database.

A fixture hands most code its session. Some code takes one for itself from
`packages.core.db`, which is bound to whatever `DATABASE_URL` names, and on a
developer's machine that is their real data. Two tests ran such code with no
fixture in the way: they reserved rate-limit slots in the owner's live
`crawler_host_budgets` on every run of the suite.

It showed as a test that could not fail. With the limiter's floor halved in
code, four fetchers were still two seconds apart, because the row they read
was the live one and already held two seconds.

`tests/conftest.py` now names the test database as the configured one before
anything reads the settings. These hold it there.
"""

from __future__ import annotations

from sqlalchemy.engine import make_url

from tests.conftest import TEST_DATABASE_URL


def test_the_configured_database_is_the_test_database() -> None:
    from packages.core.config import get_settings

    assert get_settings().async_database_url == TEST_DATABASE_URL


def test_the_engine_code_reaches_for_is_bound_to_it() -> None:
    """Made, not connected to: an engine opens nothing until it is used."""
    import packages.core.db as core_db

    assert core_db.get_engine().url.database == make_url(TEST_DATABASE_URL).database


async def test_a_limiter_built_the_ordinary_way_reserves_in_the_test_database(engine) -> None:
    """The case that leaked, with nothing patched: only the setting above
    stands between this and the owner's table."""
    import sqlalchemy as sa

    from packages.crawler.fetch import build_fetcher

    host = "leak-check.example"
    async with engine.begin() as conn:
        await conn.execute(sa.text("TRUNCATE crawler_host_budgets"))

    fetcher = build_fetcher()
    try:
        await fetcher.rate_limiter.acquire(host)
    finally:
        await fetcher.aclose()

    async with engine.begin() as conn:
        reserved = (
            await conn.execute(
                sa.text("SELECT count(*) FROM crawler_host_budgets WHERE host = :host"),
                {"host": host},
            )
        ).scalar()
        await conn.execute(sa.text("TRUNCATE crawler_host_budgets"))
    assert reserved == 1, "the reservation went to some other database"
