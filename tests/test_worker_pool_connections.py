"""A pool of claimants needs a pool of database connections to match.

`make workers n=20` was asked for on 2026-10-12, to get through 3,900
discovery tasks that five claimants were finishing nineteen a minute. A
claimant holds one connection for as long as its task runs, a minute at a
time while it waits on a host, and takes a second for what runs beside the
task: the rate limiter's reservation, the request meter, a heartbeat.
SQLAlchemy's own pool stops at fifteen. Twenty claimants would have held
every connection and then each waited thirty seconds on the others for one
more, so the pool is a setting now and a count it cannot hold is refused.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from apps.worker.run import CONNECTIONS_PER_CLAIMANT, pool_shortfall
from packages.core import db as core_db
from packages.core.config import Settings, get_settings

REPO = Path(__file__).resolve().parents[1]


def test_the_engine_is_built_with_the_configured_pool(monkeypatch) -> None:
    configured = get_settings().model_copy(update={"db_pool_size": 40, "db_max_overflow": 7})
    monkeypatch.setattr(core_db, "get_settings", lambda: configured)

    # `__wrapped__`: a fresh engine, not the cached one the suite is using.
    engine = core_db.get_engine.__wrapped__()

    assert engine.pool.size() == 40
    assert engine.pool._max_overflow == 7  # no public reader for the limit


def test_the_shipped_pool_is_sqlalchemys_own() -> None:
    """Five and ten more: a process that is not a worker pool is unchanged."""
    fields = Settings.model_fields

    assert (fields["db_pool_size"].default, fields["db_max_overflow"].default) == (5, 10)


def test_the_claimants_the_owner_ran_before_still_fit_the_shipped_pool() -> None:
    assert pool_shortfall(5, pool_size=5, max_overflow=10) is None
    assert pool_shortfall(7, pool_size=5, max_overflow=10) is None


def test_twenty_claimants_are_refused_on_the_shipped_pool() -> None:
    """Refused before anything is claimed, and told which setting to raise."""
    refusal = pool_shortfall(20, pool_size=5, max_overflow=10)

    assert refusal is not None
    assert "DB_POOL_SIZE" in refusal
    assert "40" in refusal and "15" in refusal, "what it needs and what it has"


def test_twenty_claimants_fit_a_pool_sized_for_them() -> None:
    assert pool_shortfall(20, pool_size=20 * CONNECTIONS_PER_CLAIMANT, max_overflow=10) is None


def test_make_workers_sizes_the_pool_to_the_count() -> None:
    """The command the owner types is the one that has to work."""
    printed = subprocess.run(
        ["make", "-n", "workers", "n=20"], cwd=REPO, capture_output=True, text=True, check=True
    ).stdout

    assert f"DB_POOL_SIZE={20 * CONNECTIONS_PER_CLAIMANT} " in printed
    assert "--workers 20" in printed
