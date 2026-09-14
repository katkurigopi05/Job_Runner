"""Task defaults, and the one task the system creates on its own.

A routed reply that says "interview" or "assessment" already moves the
application's outcome. What it did not do was leave the owner anything to act
on — no deadline field, no preparation list. So a routed interview or
assessment reply creates one task, with a default checklist and no due date:
the date is in the recruiter's message, and reading it out of prose would be a
guess about a deadline, which is the worst place to guess.

Created only for an exact alias route (the same rule `inbox/route.py` applies
to outcomes), and at most one open task per application and kind, so a thread
of replies does not stack duplicates.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models import Application
from packages.core.models_tracking import ApplicationTask

DEFAULT_CHECKLISTS: dict[str, tuple[str, ...]] = {
    "interview": (
        "Confirm the date, time and time zone",
        "Re-read the posting and the résumé that was sent",
        "Prepare a story for each stated requirement",
        "Write down questions to ask them",
        "Test the call or video setup",
    ),
    "assessment": (
        "Note the deadline and any time limit",
        "Check which languages and tools are allowed",
        "Set up the environment before starting",
        "Submit before the deadline",
    ),
    "follow_up": (
        "Draft the follow-up yourself — nothing here sends it",
        "Send it from your own mailbox",
    ),
    "prep": ("List what to prepare",),
    "other": (),
}


def checklist(kind: str, items: list[str] | None = None) -> list[dict[str, Any]]:
    """A stored checklist: the given items, or the default for the kind."""
    texts = items if items is not None else list(DEFAULT_CHECKLISTS.get(kind, ()))
    return [{"text": text.strip(), "done": False} for text in texts if text.strip()]


async def ensure_task_for_outcome(
    session: AsyncSession, application: Application, outcome: str
) -> ApplicationTask | None:
    """One open interview or assessment task after a routed reply. Does not commit."""
    if outcome not in ("interview", "assessment"):
        return None
    existing = await session.scalar(
        select(ApplicationTask).where(
            ApplicationTask.application_id == application.id,
            ApplicationTask.kind == outcome,
            ApplicationTask.completed_at.is_(None),
        )
    )
    if existing is not None:
        return None
    task = ApplicationTask(
        application_id=application.id,
        kind=outcome,
        title=(
            "Interview — confirm the time from the recruiter's reply"
            if outcome == "interview"
            else "Assessment — note the deadline from the recruiter's reply"
        ),
        checklist_json=checklist(outcome),
        source="inbox",
    )
    session.add(task)
    await session.flush()
    return task


__all__ = ["DEFAULT_CHECKLISTS", "checklist", "ensure_task_for_outcome"]
