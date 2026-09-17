"""Live application-status updates, fanned out to connected dashboards.

`apps/web/src/app/page.tsx` is `force-dynamic`: every status change needed a
manual reload. This is the push side of that — an in-process broker holding one
`asyncio.Queue` per connected client, and a watcher that tells it what changed.

## Why the watcher reads the database instead of being called directly

The obvious wiring is "every place that changes a status publishes an event".
It does not work here, and the reason is worth stating because the version that
does not work looks correct in testing.

There are three call sites for `state.transition()`:

    apps/api/routers/applications.py   this process — approve, reject, OTP, manual submit
    apps/worker/apply_job.py           the worker process
    packages/inbox/route.py            the worker process

`make api` and `make worker` are separate processes (Makefile). An in-process
publisher therefore sees the owner's own clicks and **nothing the machine
does** — so the stream would be silent for exactly the events the dashboard
exists to show: an application parking at `needs_review`, a run failing, an OTP
request arriving. Every one of those is written by the worker.

So the shared substrate is the one both processes already write to: the
append-only `application_events` log, which `transition()` fills in the same
transaction as the status change (§6), plus the `created` row the API writes
beside a new application. The watcher tails those. That means:

- The worker needs no change at all, and cannot forget to publish.
- The stream can never disagree with the database, because the row *is* the
  event. A publish that happened and a commit that rolled back is not a state
  this can reach.
- One publication path, so an API-originated change cannot arrive twice.

Postgres `LISTEN/NOTIFY` would cut the latency to near zero and is not an
external service either. It is not used because it needs a dedicated raw
connection and a reconnect loop for a single local reader, and a one-second
tick on one user's dashboard is not a problem anybody has. Redis and Kafka are
refused outright — CLAUDE.md §11, localhost only.

## The cursor is deliberately sloppy

`ApplicationEvent.at` is `func.now()`, which in Postgres is the **transaction**
clock, not the statement clock — CLAUDE.md §15 records the same fact breaking a
notification test. Two consequences for a cursor:

- Events written in one transaction share a timestamp, so `>` would skip
  siblings.
- A long transaction commits *after* a short one that started later, so its
  events appear with an older `at` than rows already read, and a strict
  high-water mark would step over them for ever.

The fix is to re-read a small overlap window every tick and discard ids already
seen. Cheap, and it fails toward sending a duplicate rather than dropping a
transition — which is the right direction, because every event carries the
authoritative counts and a duplicate changes nothing on screen.

## Nothing here is load-bearing for correctness

If the stream dies, the dashboard falls back to what it has always done: the
server render on next navigation. The watcher runs only while somebody is
connected, so an idle dashboard costs one process doing nothing.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import func, select

from packages.core import db as core_db
from packages.core.enums import ApplicationStatus, EventType
from packages.core.models import Application, ApplicationEvent

log = structlog.get_logger(__name__)

__all__ = ["StatusStream", "current_counts", "stream", "sse_frame"]

#: How often the watcher looks for new transitions. A person cannot tell 1s
#: from instant, and this is one dashboard on one machine.
POLL_INTERVAL_S = 1.0

#: How far back each poll re-reads. Covers the transaction-clock skew described
#: above: a transaction that takes longer than this to commit could still have
#: an event stepped over, which is why the fallback is a server render and not
#: this stream.
OVERLAP_S = 5.0

#: Ids kept to suppress the duplicates the overlap window produces. Bounded so
#: a long-lived connection cannot grow it without limit; larger than any
#: plausible number of transitions inside one overlap window.
SEEN_LIMIT = 512

#: A comment frame every this often, so an idle connection is not mistaken for
#: a dead one by the browser or anything between.
KEEPALIVE_S = 15.0

#: Events a slow client may fall behind by before the oldest are dropped.
#: Dropping is safe *because* every event carries the full counts: a client
#: that misses three and reads the fourth is exactly as correct as one that
#: read all four. It loses the detail of which application moved, not the
#: numbers on the screen.
QUEUE_MAXSIZE = 32


#: Event types that move an application between the dashboard's columns.
#: `created` is in here because a new application lands in `queued` and the
#: pipeline row has to show it — `transition` alone would leave the count stale
#: until the worker picked the task up. `notified` and `note` change no status
#: and are deliberately absent.
_STATUS_EVENTS = (EventType.TRANSITION.value, EventType.CREATED.value)


@dataclass(frozen=True)
class StatusChange:
    """One transition, with the counts as they stood when it was read.

    The counts ride along rather than being left for the client to derive.
    A client applying deltas drifts the moment one message is missed, and it
    cannot tell that it has drifted; a client assigning a snapshot cannot.
    """

    application_id: str
    kind: str
    frm: str | None
    to: str
    at: str
    counts: dict[str, int]

    def as_dict(self) -> dict[str, Any]:
        return {
            "application_id": self.application_id,
            "kind": self.kind,
            "from": self.frm,
            "to": self.to,
            "at": self.at,
            "counts": self.counts,
        }


def sse_frame(event: str, data: dict[str, Any]) -> str:
    """One server-sent-event frame. No library: this is four lines of format."""
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


async def current_counts(session: Any) -> dict[str, int]:
    """How many applications sit in each status, right now.

    Every status is present, including the zeroes. A key that appears only when
    non-zero is how `/tracker` silently dropped five of seven outcomes (§15) —
    the client renders what it is given, so give it the whole shape.
    """
    counts = {status.value: 0 for status in ApplicationStatus}
    rows = await session.execute(
        select(Application.status, func.count()).group_by(Application.status)
    )
    for status, total in rows:
        if status in counts:
            counts[status] = int(total)
        else:  # pragma: no cover - a status not in the enum is a migration bug
            counts[status] = int(total)
    return counts


class StatusStream:
    """Fan-out to connected dashboards, with one watcher behind all of them.

    Subscribing starts the watcher; the last unsubscribe stops it. Nothing
    polls while nobody is looking, which keeps the shipped cost of this feature
    at zero for anyone who never opens the dashboard.
    """

    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue[StatusChange]] = set()
        self._watcher: asyncio.Task[None] | None = None
        self._wake = asyncio.Event()

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    @property
    def watching(self) -> bool:
        return self._watcher is not None and not self._watcher.done()

    def subscribe(self) -> asyncio.Queue[StatusChange]:
        queue: asyncio.Queue[StatusChange] = asyncio.Queue(maxsize=QUEUE_MAXSIZE)
        self._subscribers.add(queue)
        if self._watcher is None or self._watcher.done():
            self._watcher = asyncio.create_task(self._watch())
        return queue

    def unsubscribe(self, queue: asyncio.Queue[StatusChange]) -> None:
        self._subscribers.discard(queue)
        if not self._subscribers and self._watcher is not None:
            self._watcher.cancel()
            self._watcher = None

    def wake(self) -> None:
        """Look now rather than at the next tick.

        Called after a commit by the routes in this process — the change is
        already durable, so the watcher will find it. This is latency, not
        delivery: removing every call to it costs a second, not an event.
        """
        self._wake.set()

    def _publish(self, change: StatusChange) -> None:
        for queue in self._subscribers:
            try:
                queue.put_nowait(change)
            except asyncio.QueueFull:
                # Drop the oldest, keep the newest: see QUEUE_MAXSIZE.
                with contextlib.suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
                with contextlib.suppress(asyncio.QueueFull):
                    queue.put_nowait(change)

    async def _watch(self) -> None:
        """Tail `application_events` and publish what is new.

        Wrapped whole: a watcher that dies takes every connected dashboard's
        updates with it, and the failure would show as "the page stopped
        refreshing" with nothing logged.
        """
        seen: set[str] = set()
        since = datetime.now(UTC) - timedelta(seconds=OVERLAP_S)
        # Everything already in the log is history, not news. Without this a
        # dashboard opened on a database with a year of applications would
        # replay all of them on connect.
        seen.update(await self._ids_before(since))

        while True:
            try:
                since = await self._tick(since, seen)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - the watcher must survive
                log.warning("status_stream_poll_failed", error=type(exc).__name__)
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=POLL_INTERVAL_S)
            except TimeoutError:
                pass
            finally:
                self._wake.clear()

    async def _ids_before(self, since: datetime) -> set[str]:
        """Transition ids already in the window at startup, so they are not news."""
        try:
            async with core_db.get_sessionmaker()() as session:
                rows = await session.execute(
                    select(ApplicationEvent.id).where(
                        ApplicationEvent.type.in_(_STATUS_EVENTS),
                        ApplicationEvent.at >= since,
                    )
                )
                return {str(row[0]) for row in rows}
        except Exception as exc:  # noqa: BLE001 - an empty set replays at worst
            log.warning("status_stream_priming_failed", error=type(exc).__name__)
            return set()

    async def _tick(self, since: datetime, seen: set[str]) -> datetime:
        """One poll. Returns the next window start."""
        async with core_db.get_sessionmaker()() as session:
            rows = (
                await session.execute(
                    select(
                        ApplicationEvent.id,
                        ApplicationEvent.application_id,
                        ApplicationEvent.type,
                        ApplicationEvent.payload_json,
                        ApplicationEvent.at,
                    )
                    .where(
                        ApplicationEvent.type.in_(_STATUS_EVENTS),
                        ApplicationEvent.at >= since,
                    )
                    .order_by(ApplicationEvent.at)
                )
            ).all()

            fresh = [row for row in rows if str(row[0]) not in seen]
            if not fresh:
                return datetime.now(UTC) - timedelta(seconds=OVERLAP_S)

            # One counts query for the whole batch: the client assigns a
            # snapshot, so every event in a batch carrying the same final
            # numbers is correct and cheaper than one query each.
            counts = await current_counts(session)

        for event_id, application_id, kind, payload, at in fresh:
            seen.add(str(event_id))
            body = payload or {}
            # A `created` event carries the url and the ATS, not a status: the
            # route sets `queued` on the row in the same transaction that
            # writes it, which `test_a_created_application_arrives_as_queued`
            # pins so this does not quietly become a lie.
            to = (
                ApplicationStatus.QUEUED.value
                if kind == EventType.CREATED.value
                else str(body.get("to") or "")
            )
            self._publish(
                StatusChange(
                    application_id=str(application_id),
                    kind=str(kind),
                    frm=body.get("from"),
                    to=to,
                    at=at.isoformat(),
                    counts=counts,
                )
            )

        if len(seen) > SEEN_LIMIT:
            # Keep the window's worth, drop the rest. Anything discarded is
            # older than the overlap and cannot be re-read.
            seen.difference_update(list(seen)[: len(seen) - SEEN_LIMIT])

        return datetime.now(UTC) - timedelta(seconds=OVERLAP_S)


#: One broker per API process. Module-level for the same reason the sessionmaker
#: is: the route needs the same instance every request, and there is one app.
stream = StatusStream()
