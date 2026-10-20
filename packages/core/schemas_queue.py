"""API shapes for the task queue (`/queue`)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class QueueTaskOut(BaseModel):
    """One task. The payload carries ids only, never a résumé or a page."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    kind: str
    status: str
    attempts: int
    payload: dict[str, Any] = Field(validation_alias="payload_json")
    run_after: datetime
    #: When its last attempt was claimed; with `finished_at`, how long it took.
    locked_at: datetime | None = None
    finished_at: datetime | None = None
    last_error: str | None = None
    created_at: datetime


class CancelIn(BaseModel):
    reason: str = Field(default="cancelled by the owner", max_length=200)


class CancelKindIn(CancelIn):
    kind: str = Field(min_length=1, max_length=50)


class CancelKindOut(BaseModel):
    kind: str
    cancelled: int


class KindStatsOut(BaseModel):
    """`queue.KindStats`: counts now, and what finished in the window."""

    model_config = ConfigDict(from_attributes=True)

    kind: str
    pending: int
    running: int
    done: int
    failed: int
    cancelled: int
    median_seconds: float | None = None
    p95_seconds: float | None = None
    oldest_waiting_seconds: float | None = None
    last_error: str | None = None
