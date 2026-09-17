"""Server-sent events: what changed, without asking the page to reload.

`GET /events/applications` streams one frame per application status change,
each carrying the counts as they stood when it was read. The dashboard
(`apps/web/src/app/page.tsx`) was `force-dynamic` — correct, and it meant the
owner learned an application had parked at `needs_review` by pressing refresh.

The mechanics are in `apps/api/status_stream.py`, including the part that is
not obvious: the events come from the append-only log rather than from the code
that changes a status, because two of the three callers of `transition()` run
in the worker process.

Three properties this route has to keep:

- **It never becomes the source of truth.** A dropped stream costs the owner a
  refresh, never a wrong number. The client falls back to the server render,
  which is what the page did before any of this existed.
- **It holds no database session.** A connection parked for the lifetime of an
  open browser tab is one the rest of the app cannot have, and this one would
  be idle for all of it. The watcher opens a session per poll and closes it.
- **It is localhost-only like everything else.** No CORS, no auth — the
  dashboard reaches it through the Next rewrite (`/api/events/applications`),
  so the peer FastAPI sees is the Next server on loopback (CLAUDE.md §3).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from apps.api.status_stream import KEEPALIVE_S, current_counts, sse_frame, stream
from packages.core import db as core_db

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/events", tags=["events"])

SSE_MEDIA_TYPE = "text/event-stream"

#: `no-store` because a cached event stream is a contradiction, and
#: `X-Accel-Buffering: no` because a proxy that buffers turns a live stream
#: into a very slow poll — the Next rewrite is a proxy.
#:
#: **`no-transform` is the one that was missing, and its absence killed the
#: feature in a browser while every test passed.** Next's dev proxy compresses
#: proxied responses, and a compressor buffers: the browser opened the stream,
#: fired `open`, and then received nothing for as long as it was left running.
#: Measured against the same endpoint through the same proxy, varying only the
#: header every browser sends:
#:
#:     curl -N                                        -> 124 bytes, the ready frame
#:     curl -N -H 'Accept-Encoding: gzip, deflate, br'-> 10 bytes, a gzip header
#:     the same, direct to FastAPI (no proxy)         -> 124 bytes
#:
#: `no-transform` is the standards-compliant way to say "do not re-encode this"
#: (RFC 9111 §5.2.2.6). It belongs on the response rather than in the dashboard
#: config because it is a property of *this* body, not of the Next server:
#: turning compression off globally would slow every other page to fix one
#: route.
SSE_HEADERS = {
    "Cache-Control": "no-store, no-transform",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


async def _read_counts() -> dict[str, int]:
    """The current counts, on a session of its own.

    Split out so the whole checkout-query-return cycle is one awaitable the
    caller can shield. A `shield` around only the query would still leave the
    connection's return exposed, which is the part that breaks.
    """
    async with core_db.get_sessionmaker()() as session:
        return await current_counts(session)


async def _frames(request: Request) -> AsyncIterator[str]:
    """One connected dashboard's view of the stream.

    Opens with the current counts rather than waiting for the first change: a
    page that connects and then shows nothing until something happens is
    indistinguishable from a broken stream, and on a quiet afternoon that is
    most of the time.
    """
    queue = stream.subscribe()
    try:
        try:
            # Shielded, and this is not caution for its own sake.
            #
            # Starlette's `BaseHTTPMiddleware` cancels the downstream task when
            # the client disconnects, and a browser disconnects from a stream
            # whenever a tab closes or a page navigates — routinely, and at a
            # moment nobody chooses. Unshielded, that cancel can land *inside*
            # the session's teardown: SQLAlchemy is part-way through returning
            # the connection to the pool, `do_terminate` is interrupted, and
            # the next thing to check that connection out gets
            # `InterfaceError: connection is closed` — a failure that names
            # neither this route nor the client that left.
            #
            # It is not theoretical. It took CI down on the commit that added
            # this stream: a truncate in an unrelated fixture, two tests after
            # a test whose only crime was closing the response as soon as it
            # had read the headers. `asyncio.shield` lets the read and the
            # connection's return finish even when the caller has gone; the
            # frame it produces is simply never delivered, which is correct —
            # nobody is listening.
            counts = await asyncio.shield(_read_counts())
            yield sse_frame("ready", {"counts": counts})
        except Exception as exc:  # noqa: BLE001 - a live stream still beats none
            # The database being down is what /health is for. Say the stream is
            # open, send no counts, and let the next real change carry them.
            log.warning("status_stream_initial_counts_failed", error=type(exc).__name__)
            yield sse_frame("ready", {"counts": None})

        while True:
            if await request.is_disconnected():
                return
            try:
                change = await asyncio.wait_for(queue.get(), timeout=KEEPALIVE_S)
            except TimeoutError:
                # A comment frame. Keeps the connection warm and gives the
                # generator a chance to notice a disconnect on a quiet stream.
                yield ": keep-alive\n\n"
                continue
            yield sse_frame("status", change.as_dict())
    finally:
        # Reached on disconnect, on cancellation, and on error alike. The last
        # unsubscribe stops the watcher, so a closed tab stops the polling.
        stream.unsubscribe(queue)


@router.get("/applications")
async def application_events(request: Request) -> StreamingResponse:
    """Stream application status changes for as long as the client listens."""
    return StreamingResponse(_frames(request), media_type=SSE_MEDIA_TYPE, headers=SSE_HEADERS)
