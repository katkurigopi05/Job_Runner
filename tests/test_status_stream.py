"""Live status updates — `GET /events/applications`.

The dashboard was `force-dynamic`: every status change needed a manual reload.
These tests cover the two halves that make the stream worth having, and one
that makes it honest.

**The half that is easy to get wrong** is the worker. `state.transition()` has
three callers and two of them — `apps/worker/apply_job.py` and
`packages/inbox/route.py` — run in the worker process, not the API. An
in-process publisher would carry the owner's own clicks and nothing the machine
does, so the stream would be silent for parking at `needs_review`, for a failed
run, and for an OTP request: every event the dashboard exists to show.
`test_a_change_written_by_another_session_arrives` writes through a second
session exactly as the worker does, and is the test that fails if anyone
"simplifies" the watcher into a direct call.

**The honest half** is that nothing here is load-bearing. A dropped stream
costs a refresh, so these also pin that the watcher stops when the last
dashboard closes, and that opening one does not replay history.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select

from apps.api import status_stream
from apps.api.status_stream import stream
from packages.core.enums import ApplicationStatus, FailureReason
from packages.core.models import Application
from packages.core.state import transition

#: Every status the pipeline row renders. The stream sends all of them,
#: including the zeroes — a key that appears only when non-zero is how
#: `/tracker` silently dropped five of seven columns (CLAUDE.md §15).
ALL_STATUSES = {status.value for status in ApplicationStatus}

#: Long enough to be a real poll, short enough that the suite does not wait on
#: it. The shipped interval is a second, which is invisible to a person and
#: very visible in a test.
FAST_POLL_S = 0.02

#: A frame has to arrive within this or the test has failed, not hung.
FRAME_TIMEOUT_S = 10.0

#: How long "nothing arrived" is given to be wrong. Many poll intervals: a
#: replayed history would come back on the first one.
QUIET_WINDOW_S = 0.5


@pytest_asyncio.fixture
async def live_api(committing_sessionmaker, monkeypatch) -> AsyncIterator[AsyncClient]:
    """A real server on a real socket, because `ASGITransport` cannot stream.

    httpx's in-process transport collects `http.response.body` into a list and
    builds the response only after the ASGI app returns (`_transports/asgi.py`).
    An endpoint that never returns therefore never yields a response object at
    all — the `client` fixture every other API test uses hangs on this one at
    `__aenter__`, before a single assertion runs.

    So this fixture runs uvicorn on an ephemeral port inside the test's own
    event loop. Same process, so the patched sessionmaker still applies; real
    sockets, so incremental delivery and client disconnect are the real thing
    rather than a mock of it — and disconnect is what stops the watcher.
    """
    import uvicorn

    import packages.core.db as core_db
    from apps.api.main import app

    monkeypatch.setattr(core_db, "get_sessionmaker", lambda: committing_sessionmaker)

    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", lifespan="off")
    server = uvicorn.Server(config)
    serving = asyncio.create_task(server.serve())
    try:
        for _ in range(500):
            if server.started:
                break
            await asyncio.sleep(0.01)
        assert server.started, "the test server did not come up"
        port = server.servers[0].sockets[0].getsockname()[1]
        async with AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=FRAME_TIMEOUT_S) as ac:
            yield ac
    finally:
        server.should_exit = True
        await serving


@pytest.fixture(autouse=True)
def _fast_poll(monkeypatch):
    monkeypatch.setattr(status_stream, "POLL_INTERVAL_S", FAST_POLL_S)
    monkeypatch.setattr(status_stream, "OVERLAP_S", 2.0)


@pytest.fixture(autouse=True)
def _broker_is_left_clean():
    """No subscriber and no watcher may outlive a test.

    The broker is a module-level singleton, so a leaked watcher would poll
    through every later test in the process and the leak would show up as
    something unrelated failing.
    """
    yield
    assert stream.subscriber_count == 0, "a connection was not cleaned up"
    assert not stream.watching, "the watcher outlived the last subscriber"


class Frames:
    """One reader per response.

    httpx allows a response body to be iterated exactly once — a second
    `aiter_lines()` raises `StreamConsumed` — so the iterator is made here and
    kept, rather than a helper making a new one per call.
    """

    def __init__(self, response) -> None:
        self._lines = response.aiter_lines()

    async def until(self, want: str, *, limit: int = 40) -> dict[str, Any]:
        """The data of the next frame of type `want`. Fails rather than hangs."""

        async def _read() -> dict[str, Any]:
            event: str | None = None
            blanks = 0
            async for line in self._lines:
                if line.startswith("event:"):
                    event = line.split(":", 1)[1].strip()
                elif line.startswith("data:") and event == want:
                    return json.loads(line.split(":", 1)[1].strip())
                elif not line.strip():
                    blanks += 1
                    if blanks > limit:
                        raise AssertionError(f"no {want!r} frame in {limit} frames")
            raise AssertionError(f"stream closed before a {want!r} frame")

        return await asyncio.wait_for(_read(), timeout=FRAME_TIMEOUT_S)


async def _application(sessions, candidate: dict[str, str], url: str) -> uuid.UUID:
    async with sessions() as session:
        application = Application(
            candidate_id=uuid.UUID(candidate["candidate_id"]),
            profile_id=uuid.UUID(candidate["profile_id"]),
            url=url,
            ats="greenhouse",
            status=ApplicationStatus.QUEUED.value,
        )
        session.add(application)
        await session.commit()
        return application.id


async def _move(sessions, application_id: uuid.UUID, to: ApplicationStatus, **kwargs) -> None:
    """A status change made the way the *worker* makes one: its own session."""
    async with sessions() as session:
        application = await session.get(Application, application_id)
        await transition(session, application, to, **kwargs)
        await session.commit()


# --------------------------------------------------------------------------
# The stream itself
# --------------------------------------------------------------------------


async def test_the_stream_opens_with_the_current_counts(
    live_api: AsyncClient, complete_candidate
) -> None:
    """A page that connects and then shows nothing until something happens is
    indistinguishable from a broken stream, and on a quiet afternoon that is
    most of the time."""
    async with live_api.stream("GET", "/events/applications") as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        ready = await Frames(response).until("ready")

    assert set(ready["counts"]) == ALL_STATUSES, "every status, including the zeroes"


async def test_the_response_refuses_to_be_compressed(live_api: AsyncClient) -> None:
    """`no-transform`, and the feature is dead in a browser without it.

    Next's dev proxy compresses proxied responses and a compressor buffers, so
    the browser opened the stream, fired `open`, and then received nothing.
    Every test here passed throughout — they talk to uvicorn directly, where
    there is no proxy to re-encode anything. Measured through the dashboard,
    varying only the header every browser sends:

        curl -N                                         -> 124 bytes, the frame
        curl -N -H 'Accept-Encoding: gzip, deflate, br' ->  10 bytes, a gzip header
        the same, direct to FastAPI                     -> 124 bytes

    This asserts the header rather than the behaviour because the behaviour
    belongs to a proxy that is not in this process. A header nobody checks is
    how it went missing in the first place.
    """
    async with live_api.stream("GET", "/events/applications") as response:
        cache_control = response.headers["cache-control"]
    assert "no-transform" in cache_control, "a proxy may re-encode and buffer the stream"
    assert "no-store" in cache_control


async def test_a_change_written_by_another_session_arrives(
    live_api: AsyncClient, committing_sessionmaker, complete_candidate
) -> None:
    """The worker's transitions are the ones that matter, and they are not here.

    Written through a second session, committed, and read back off the stream —
    which is only possible because the watcher tails `application_events`
    rather than being called by whatever changed the status.
    """
    application_id = await _application(
        committing_sessionmaker, complete_candidate, "https://boards.greenhouse.io/acme/jobs/1"
    )

    async with live_api.stream("GET", "/events/applications") as response:
        frames = Frames(response)
        await frames.until("ready")
        await _move(committing_sessionmaker, application_id, ApplicationStatus.RUNNING)
        change = await frames.until("status")

    assert change["application_id"] == str(application_id)
    assert change["from"] == "queued"
    assert change["to"] == "running"
    assert change["counts"]["running"] == 1
    assert change["counts"]["queued"] == 0


async def test_every_event_carries_the_whole_count_map(
    live_api: AsyncClient, committing_sessionmaker, complete_candidate
) -> None:
    """The client assigns a snapshot rather than applying a delta.

    A dashboard adding `+1 running, -1 queued` drifts the moment one message is
    missed and cannot tell that it has. This is what makes a dropped frame
    survivable, so it is asserted rather than assumed.
    """
    application_id = await _application(
        committing_sessionmaker, complete_candidate, "https://boards.greenhouse.io/acme/jobs/2"
    )

    async with live_api.stream("GET", "/events/applications") as response:
        frames = Frames(response)
        await frames.until("ready")
        await _move(committing_sessionmaker, application_id, ApplicationStatus.RUNNING)
        change = await frames.until("status")

    assert set(change["counts"]) == ALL_STATUSES
    assert sum(change["counts"].values()) == 1


async def test_a_created_application_arrives_as_queued(
    live_api: AsyncClient, client: AsyncClient, complete_candidate
) -> None:
    """`created` is not a transition, and the pipeline row still has to move.

    The create route writes a `created` event and sets `queued` on the row in
    the same transaction. The watcher reads the first and reports the second,
    so this test is what stops that pairing from quietly coming apart.
    """
    async with live_api.stream("GET", "/events/applications") as response:
        frames = Frames(response)
        await frames.until("ready")
        created = await client.post(
            "/applications",
            json={**complete_candidate, "url": "https://boards.greenhouse.io/acme/jobs/3"},
        )
        assert created.status_code == 201, created.text
        change = await frames.until("status")

    assert change["kind"] == "created"
    assert change["to"] == ApplicationStatus.QUEUED.value
    assert change["counts"]["queued"] == 1


async def test_opening_a_stream_does_not_replay_history(
    live_api: AsyncClient, committing_sessionmaker, complete_candidate
) -> None:
    """Otherwise a dashboard opened on a year of applications replays all of them."""
    application_id = await _application(
        committing_sessionmaker, complete_candidate, "https://boards.greenhouse.io/acme/jobs/4"
    )
    await _move(committing_sessionmaker, application_id, ApplicationStatus.RUNNING)
    await _move(
        committing_sessionmaker,
        application_id,
        ApplicationStatus.FAILED,
        failure_reason=FailureReason.SITE_ERROR,
    )

    async with live_api.stream("GET", "/events/applications") as response:
        frames = Frames(response)
        ready = await frames.until("ready")
        # The counts are current — history is *reported*, just not replayed as
        # news — and the only frames after it are keep-alives.
        assert ready["counts"]["failed"] == 1
        # Nothing arriving is the assertion, so it is timed rather than
        # counted: the window is many poll intervals wide, and a replay would
        # land in the first one.
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(frames.until("status"), timeout=QUIET_WINDOW_S)


async def test_the_watcher_stops_when_the_last_dashboard_closes(
    live_api: AsyncClient, complete_candidate
) -> None:
    """Nothing polls while nobody is looking — the shipped cost of this feature
    for an owner who never opens the dashboard is zero."""
    async with live_api.stream("GET", "/events/applications") as response:
        await Frames(response).until("ready")
        assert stream.watching, "a connected dashboard should be watched"

    # The generator's `finally` runs as the response closes, but the close
    # itself is what schedules it: yield to the loop before reading the state.
    for _ in range(50):
        if not stream.watching and stream.subscriber_count == 0:
            break
        await asyncio.sleep(0.01)
    assert stream.subscriber_count == 0
    assert not stream.watching


async def test_two_dashboards_share_one_watcher(
    live_api: AsyncClient, committing_sessionmaker, complete_candidate
) -> None:
    """Fan-out is per client; the poll is not. Two open tabs are one query."""
    application_id = await _application(
        committing_sessionmaker, complete_candidate, "https://boards.greenhouse.io/acme/jobs/5"
    )

    async with live_api.stream("GET", "/events/applications") as first:
        first_frames = Frames(first)
        await first_frames.until("ready")
        async with live_api.stream("GET", "/events/applications") as second:
            second_frames = Frames(second)
            await second_frames.until("ready")
            assert stream.subscriber_count == 2
            await _move(committing_sessionmaker, application_id, ApplicationStatus.RUNNING)
            assert (await first_frames.until("status"))["to"] == "running"
            assert (await second_frames.until("status"))["to"] == "running"


# --------------------------------------------------------------------------
# The ordering that keeps the stream honest
# --------------------------------------------------------------------------


def test_the_stream_is_only_ever_woken_after_a_commit() -> None:
    """A stream told about a change inside the transaction announces one that
    may still roll back — the defect §15 records the doorbell learning.

    Source-level because it is an *ordering*, and no runtime assertion sees the
    difference on a transaction that happens to commit.
    """
    from pathlib import Path

    source = Path("apps/api/routers/applications.py").read_text(encoding="utf-8")
    lines = source.splitlines()
    calls = [i for i, line in enumerate(lines) if line.strip() == "_announce()"]
    assert calls, "no route announces a status change"
    for index in calls:
        previous = lines[index - 1].strip()
        assert previous == "await session.commit()", (
            f"_announce() on line {index + 1} follows {previous!r}, not a commit"
        )


def test_waking_the_stream_carries_no_payload() -> None:
    """`wake()` is latency, not delivery.

    The watcher reads the committed row itself, so a missed wake costs a second
    and a wrong one is impossible. That is what makes it safe to call from a
    handler that must not fail.
    """
    import inspect

    signature = inspect.signature(stream.wake)
    assert not signature.parameters, "wake() must not accept an event to publish"


async def test_a_status_the_enum_does_not_know_still_reaches_the_client(
    live_api: AsyncClient, committing_sessionmaker, complete_candidate
) -> None:
    """A row in a status no longer in the enum is a migration bug, and hiding it
    in the counts is how it would stay one."""
    async with committing_sessionmaker() as session:
        session.add(
            Application(
                candidate_id=uuid.UUID(complete_candidate["candidate_id"]),
                profile_id=uuid.UUID(complete_candidate["profile_id"]),
                url="https://boards.greenhouse.io/acme/jobs/6",
                ats="greenhouse",
                status="queued",
            )
        )
        await session.commit()

    async with live_api.stream("GET", "/events/applications") as response:
        ready = await Frames(response).until("ready")

    assert ready["counts"]["queued"] == 1
    async with committing_sessionmaker() as session:
        stored = (await session.scalars(select(Application))).all()
    assert len(stored) == 1
