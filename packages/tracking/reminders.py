"""Local reminders for tasks whose reminder time has come.

Delivered through the notification backends the owner already configured
(`NOTIFY_BACKENDS`: log, desktop, webhook to their own URL). Never to an
employer, never an email to anyone: a reminder tells the owner, and the owner
decides what to do.

Rings once. `reminded_at` is set in the same transaction that selected the
task, so a worker that restarts mid-tick does not ring it twice, and a task
completed before its reminder never rings.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models import Application
from packages.core.models_tracking import ApplicationTask

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class Reminder:
    task_id: str
    application_id: str
    kind: str
    title: str
    due_at: str | None
    url: str

    @property
    def body(self) -> str:
        return f"{self.title}" + (f" — due {self.due_at}" if self.due_at else "")

    def as_dict(self) -> dict[str, str | None]:
        """The webhook payload: identifiers and the owner's own task text only."""
        return {
            "task_id": self.task_id,
            "application_id": self.application_id,
            "kind": self.kind,
            "title": "Jobrunner reminder",
            "body": self.body,
            "due_at": self.due_at,
            "url": self.url,
        }


async def _send(reminder: Reminder) -> list[str]:
    import httpx

    from packages.core.config import get_settings
    from packages.core.notify import WEBHOOK_TIMEOUT_S, _configured_backends, _desktop

    delivered: list[str] = []
    log.info("task_reminder", task_id=reminder.task_id, kind=reminder.kind)
    for backend in _configured_backends():
        try:
            if backend == "log":
                delivered.append(backend)
            elif backend == "desktop":
                shown = type("Shown", (), {"title": "Jobrunner reminder", "body": reminder.body})()
                await asyncio.to_thread(_desktop, shown)  # type: ignore[arg-type]
                delivered.append(backend)
            elif backend == "webhook":
                url = get_settings().notify_webhook_url
                if not url:
                    continue
                async with httpx.AsyncClient() as client:
                    response = await client.post(
                        url, json=reminder.as_dict(), timeout=WEBHOOK_TIMEOUT_S
                    )
                    response.raise_for_status()
                delivered.append(backend)
        except Exception as exc:  # noqa: BLE001 - a reminder must not fail the worker
            log.warning("task_reminder_backend_failed", backend=backend, error=type(exc).__name__)
    return delivered


async def ring_due(session: AsyncSession, *, now: datetime | None = None, limit: int = 50) -> int:
    """Deliver every reminder that is due and not yet rung. Commits."""
    current = now or datetime.now(UTC)
    rows = (
        await session.execute(
            select(ApplicationTask, Application.id)
            .join(Application, Application.id == ApplicationTask.application_id)
            .where(
                ApplicationTask.reminder_at.is_not(None),
                ApplicationTask.reminder_at <= current,
                ApplicationTask.reminded_at.is_(None),
                ApplicationTask.completed_at.is_(None),
            )
            .order_by(ApplicationTask.reminder_at)
            .limit(limit)
            .with_for_update(skip_locked=True, of=ApplicationTask)
        )
    ).all()
    reminders = []
    for task, application_id in rows:
        task.reminded_at = current
        reminders.append(
            Reminder(
                task_id=str(task.id),
                application_id=str(application_id),
                kind=task.kind,
                title=task.title,
                due_at=task.due_at.isoformat() if task.due_at else None,
                url=f"http://127.0.0.1:3001/applications/{application_id}",
            )
        )
    # Committed before delivery: a reminder that rang and then failed to record
    # would ring again on every tick.
    await session.commit()
    for reminder in reminders:
        await _send(reminder)
    return len(reminders)


__all__ = ["Reminder", "ring_due"]
