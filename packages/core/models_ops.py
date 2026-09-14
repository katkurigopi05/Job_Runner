"""Operational tables: how the local installation itself is doing.

Kept out of `models.py`, which is past the size this repo allows, and
registered on the same `Base.metadata` by the import at the bottom of that
module — so Alembic and the test schema see these tables without either
having to know this file exists.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, String, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from packages.core.models import Base


class WorkerHeartbeat(Base):
    """The last time each queue worker said it was alive, and what it was doing.

    Nothing recorded this. The crawl status could say work was *queued with
    nobody holding it*, but a worker that had not claimed anything for an hour
    looked the same whether it was idle or dead — and the owner's first
    recovery question is "is the worker running", which no screen could answer.

    One row per worker id, upserted. It is a liveness signal, not a log: the
    history of what ran lives on `queue_tasks`.
    """

    __tablename__ = "worker_heartbeats"

    worker_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    hostname: Mapped[str] = mapped_column(String(255), nullable=False)
    pid: Mapped[int] = mapped_column(Integer, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    #: What the worker was running at its last beat; NULL when idle.
    current_task_kind: Mapped[str | None] = mapped_column(String(50))
    current_task_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    tasks_completed: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    #: Exception class name only. A message can carry page content or a URL
    #: with a token in it, and this is shown on a dashboard.
    last_error_kind: Mapped[str | None] = mapped_column(String(100))
    #: Set on a clean shutdown, so "stopped" and "crashed" read differently.
    stopped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (Index("ix_worker_heartbeats_last_seen_at", "last_seen_at"),)


__all__ = ["WorkerHeartbeat"]
