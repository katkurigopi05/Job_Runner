"""Small local correlation context for requests, queue tasks, and model calls.

The identifiers contain no user material.  They exist only to join records
that already live on this machine; there is no exporter or hosted tracer.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_trace_id: ContextVar[str | None] = ContextVar("jobrunner_trace_id", default=None)
_task_id: ContextVar[str | None] = ContextVar("jobrunner_task_id", default=None)

TRACE_HEADER = "X-Trace-ID"


def new_trace_id() -> str:
    """Return an opaque trace identifier safe to persist and log."""
    return str(uuid.uuid4())


def normalize_trace_id(value: object) -> str | None:
    """Return a canonical UUID, or None for untrusted/non-identifier input."""
    if not isinstance(value, str):
        return None
    try:
        return str(uuid.UUID(value))
    except ValueError:
        return None


def current_trace_id() -> str | None:
    return _trace_id.get()


def current_task_id() -> str | None:
    return _task_id.get()


@contextmanager
def correlated(*, trace_id: str | None, task_id: str | None = None) -> Iterator[None]:
    """Bind correlation for this async/thread context, then restore its parent."""
    trace_token = _trace_id.set(trace_id)
    task_token = _task_id.set(task_id)
    try:
        yield
    finally:
        _task_id.reset(task_token)
        _trace_id.reset(trace_token)
