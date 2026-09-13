"""One pytest process per test database, and it says so when refused.

The session fixture in `conftest.py` drops and recreates the whole schema on
startup. That is the right thing for a suite that owns its database and a
destructive thing for one that does not: a second run starting while the first
is mid-test deletes rows the first has committed, and the failure lands in
whichever test happened to be running — naming an unrelated feature, and gone
again by the time anyone looks.

It happened, twice, in `test_worker_host_blocking`. These tests hold the guard
that makes it say so instead.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import text

from tests.conftest import SCHEMA_LOCK_KEY


def _engine_fixture_source() -> str:
    """The body of `conftest.engine`, read from the file.

    `inspect.getsource` cannot reach it: the fixture decorator replaces the
    function with pytest_asyncio's wrapper.
    """
    source = Path("tests/conftest.py").read_text(encoding="utf-8")
    body = source.split("async def engine():", 1)[1]
    return body.split("\n@", 1)[0]


def _code_of(source: str) -> str:
    """The same text with comment lines dropped.

    Prose about the rule is not the rule — and "exactly" contains "xact",
    which is how this helper came to exist.
    """
    return "\n".join(line for line in source.splitlines() if not line.lstrip().startswith("#"))


async def test_the_database_is_claimed_for_the_whole_session(engine) -> None:
    """A second connection cannot take the lock while the suite is running.

    This is the property a second `pytest` would hit, asserted from inside the
    run that holds it: the claim is live right now, not merely taken once at
    startup and released.
    """
    async with engine.connect() as conn:
        held = await conn.scalar(
            text("SELECT pg_try_advisory_lock(:key)"), {"key": SCHEMA_LOCK_KEY}
        )
        if held:
            # Never reached unless the guard is gone. Release it rather than
            # leaving a stray lock behind to confuse the next run.
            await conn.scalar(text("SELECT pg_advisory_unlock(:key)"), {"key": SCHEMA_LOCK_KEY})
        await conn.commit()
    assert held is False, (
        "the session fixture is not holding the database claim — a second "
        "pytest run against this database would drop the schema under it"
    )


async def test_the_claim_survives_a_commit(engine) -> None:
    """The lock is session-level, and the fixture commits after taking it.

    It has to: the drop that follows waits on table locks, so the claiming
    connection must not sit idle in a transaction. A transaction-level lock
    (`pg_advisory_xact_lock`) would be released by that commit and the guard
    would be decoration. The test above passing *after* the fixture committed
    is the proof; this one names why it matters so the swap is a visible
    change rather than a silent one.
    """
    claim = _code_of(_engine_fixture_source())
    assert "pg_try_advisory_lock" in claim
    assert "pg_advisory_xact_lock" not in claim, (
        "a transaction-scoped lock does not survive the commit below"
    )
    assert "await claim.commit()" in claim


def test_a_refused_claim_fails_rather_than_skips() -> None:
    """REQUIRE_DB must not decide this one.

    An absent database is a reason to skip — `pytest` is green on a fresh
    checkout before `docker compose up`, which is deliberate. A database
    already in use is a mistake with a remedy, and skipping the suite would
    hide it exactly as well as the collision it prevents did.
    """
    # Comments are stripped: the one in there explains *why* it is not a skip,
    # and reading the prose as the behaviour would make the test unwritable.
    code = _code_of(_engine_fixture_source().split("if not held:", 1)[1].split("\n    try:", 1)[0])
    assert "pytest.fail(_CONCURRENT_RUN" in code
    assert "skip" not in code
    assert "REQUIRE_DB" not in code
