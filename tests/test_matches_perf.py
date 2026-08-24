"""What `GET /matches` is allowed to pull out of Postgres.

The rows behind this route are fetched unbounded on purpose — the seniority
and remoteness filters read posting *text*, so they cannot run in SQL, and the
limit is applied after filtering so a narrow search still returns a full page.
That decision is sound and documented in the handler.

Its consequence is not obvious: every column on `Posting` is then loaded for
every match in the table, not for the page. `description_embedding` is 384
floats, `MatchOut` has no field for it, and no filter reads it — so the route
was transferring the entire corpus's vectors on every request in order to
throw them away. Against 1,853 matches that measured 183ms eager against
119ms deferred.

This test pins the column out of the query rather than asserting a duration.
A timing assertion on a route whose cost scales with the table would be flaky
on a small fixture and meaningless on a large one; "the bytes are not
requested" is the thing that actually holds.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import event

pytestmark = pytest.mark.asyncio


@pytest.fixture
def executed_sql(engine):
    """Every statement the route emits, captured off the sync engine."""
    seen: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany):
        seen.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", record)
    yield seen
    event.remove(engine.sync_engine, "before_cursor_execute", record)


async def test_the_feed_never_selects_the_embedding(
    client: AsyncClient, executed_sql: list[str]
) -> None:
    """The regression. `select(Match, Posting)` loads every mapped column
    unless told otherwise, and nothing here reads the vector."""
    assert (await client.get("/matches")).status_code == 200

    selects = [s for s in executed_sql if "description_embedding" in s]
    assert not selects, (
        "GET /matches is loading description_embedding again — "
        f"{len(selects)} statement(s) reference it"
    )


async def test_the_text_the_filters_read_is_still_loaded(
    client: AsyncClient, executed_sql: list[str]
) -> None:
    """The other half, and the reason this is a `defer` and not a column list.

    `filters.py` reads `description_raw` for the clearance and seniority
    checks. Deferring it too would make the feed silently stop filtering —
    a much worse bug than the one being fixed, and an easy one to introduce
    while trimming columns.
    """
    assert (await client.get("/matches")).status_code == 200

    assert any("description_raw" in s for s in executed_sql), (
        "description_raw is no longer loaded; the seniority and clearance "
        "filters read it and would silently pass everything"
    )
