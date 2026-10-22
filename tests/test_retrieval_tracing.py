"""Retrieval telemetry must be local, text-free and behavior preserving."""

import asyncio
import json

import pytest

from packages.core.trace import correlated
from packages.matching import retrieval_trace as trace


@pytest.fixture
def events(monkeypatch):
    recorded = []
    monkeypatch.setattr(trace.log, "info", lambda event, **fields: recorded.append((event, fields)))
    return recorded


async def test_stages_emit_one_correlated_record_without_inputs_or_results(events):
    @trace.stage("keyword")
    async def keyword(private):
        return [private]

    @trace.traced
    async def search(private):
        return await keyword(private)

    with correlated(trace_id="opaque-trace", task_id="opaque-task"):
        assert await search("PRIVATE_SENTINEL") == ["PRIVATE_SENTINEL"]
    assert len(events) == 1
    event, fields = events[0]
    assert event == "retrieval_completed"
    assert fields["trace_id"] == "opaque-trace"
    assert fields["task_id"] == "opaque-task"
    assert fields["success"] is True
    assert fields["stages"][0]["stage"] == "keyword"
    assert fields["stages"][0]["items"] == 1
    assert fields["stages"][0]["elapsed_ms"] >= 0
    assert "PRIVATE_SENTINEL" not in json.dumps(fields)


async def test_exception_text_never_logged_and_next_request_has_no_old_stages(events):
    @trace.stage("keyword")
    async def broken():
        raise ValueError("PRIVATE_SENTINEL")

    @trace.traced
    async def search():
        return await broken()

    with pytest.raises(ValueError, match="PRIVATE_SENTINEL"):
        await search()

    @trace.traced
    async def empty():
        return 42

    assert await empty() == 42
    assert events[0][1]["success"] is False
    assert events[0][1]["stages"][0]["success"] is False
    assert events[1][1]["stages"] == []
    assert "PRIVATE_SENTINEL" not in json.dumps(events)


async def test_concurrent_requests_keep_their_stages_separate(events):
    @trace.stage("keyword")
    async def keyword():
        await asyncio.sleep(0)

    @trace.traced
    async def search():
        await keyword()

    await asyncio.gather(search(), search())
    assert len({fields["retrieval_id"] for _, fields in events}) == 2
    assert [len(fields["stages"]) for _, fields in events] == [1, 1]


async def test_cancelled_request_resets_context_and_logging_failure_cannot_break_search(
    events, monkeypatch
):
    @trace.traced
    async def cancelled():
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await cancelled()
    assert events[0][1]["success"] is False

    def broken_logger(*args, **kwargs):
        raise RuntimeError("log unavailable")

    monkeypatch.setattr(trace.log, "info", broken_logger)

    @trace.traced
    async def search():
        return "ok"

    assert await search() == "ok"


async def test_actual_retrieval_result_and_order_unchanged(db_session, events):
    from packages.matching.retrieve import retrieve

    # Both normal and traced entry points must agree, including no-search routes.
    for question in ("hello", "jobs using kafka", "right to work jobs"):
        expected = await retrieve.__wrapped__(db_session, question)
        actual = await retrieve(db_session, question)
        assert actual == expected
    assert len(events) == 3
