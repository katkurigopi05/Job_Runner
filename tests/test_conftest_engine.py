from __future__ import annotations


async def test_committing_engine_uses_pre_ping(engine) -> None:
    assert engine.sync_engine.pool._pre_ping is True
