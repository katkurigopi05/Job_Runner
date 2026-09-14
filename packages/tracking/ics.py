"""Calendar export (RFC 5545) for tasks with a due date.

A file the owner imports into their own calendar. Nothing is published or
subscribed to: the API serves it to loopback only, like every other route.
Only the task's own fields go in — title, time, location, checklist, and the
application link — never a contact's email or phone.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime, timedelta

from packages.core.models_tracking import ApplicationTask

#: Default length of an event with only a due time.
EVENT_MINUTES = 60


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def _stamp(moment: datetime) -> str:
    utc = moment if moment.tzinfo else moment.replace(tzinfo=UTC)
    return utc.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def _fold(line: str) -> list[str]:
    """Lines longer than 75 octets continue on the next line, indented one space."""
    encoded = line.encode()
    if len(encoded) <= 75:
        return [line]
    parts: list[str] = []
    current = b""
    for char in line:
        piece = char.encode()
        if len(current) + len(piece) > (75 if not parts else 74):
            parts.append(current.decode())
            current = b""
        current += piece
    parts.append(current.decode())
    return [parts[0], *(" " + part for part in parts[1:])]


def render(
    tasks: Iterable[tuple[ApplicationTask, str | None]], *, now: datetime | None = None
) -> str:
    """A VCALENDAR of every task with a due date. `(task, application_url)` pairs."""
    stamp = _stamp(now or datetime.now(UTC))
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Jobrunner//Application tasks//EN",
        "CALSCALE:GREGORIAN",
    ]
    for task, url in tasks:
        if task.due_at is None:
            continue
        description = [
            f"{'[x]' if item.get('done') else '[ ]'} {item['text']}"
            for item in task.checklist_json or []
        ]
        if url:
            description.append(f"Application: {url}")
        event = [
            "BEGIN:VEVENT",
            f"UID:{task.id}@jobrunner.local",
            f"DTSTAMP:{stamp}",
            f"DTSTART:{_stamp(task.due_at)}",
            f"DTEND:{_stamp(task.due_at + timedelta(minutes=EVENT_MINUTES))}",
            f"SUMMARY:{_escape(task.title)}",
        ]
        if task.location:
            event.append(f"LOCATION:{_escape(task.location)}")
        if description:
            event.append(f"DESCRIPTION:{_escape(chr(10).join(description))}")
        if task.reminder_at is not None and task.reminder_at <= task.due_at:
            minutes = int((task.due_at - task.reminder_at).total_seconds() // 60)
            event += [
                "BEGIN:VALARM",
                "ACTION:DISPLAY",
                f"DESCRIPTION:{_escape(task.title)}",
                f"TRIGGER:-PT{minutes}M",
                "END:VALARM",
            ]
        event.append("END:VEVENT")
        lines.extend(event)
    lines.append("END:VCALENDAR")
    return "\r\n".join(folded for line in lines for folded in _fold(line)) + "\r\n"


__all__ = ["render"]
