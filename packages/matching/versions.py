"""What changed between two readings of one posting.

The crawler overwrote a posting in place, so a salary cut or a new hard
requirement left no trace: the owner saw today's text and nothing to compare
it with. `PostingVersion` keeps the comparable fields; this turns two of them
into the changes a person would look for.

Only the fields worth deciding on are compared — title, location, pay, the
required and preferred skills, education. A text edit that touches none of
them is still reported, as a text edit, so a version never appears to have
changed nothing.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models_jobs import PostingVersion

SNAPSHOT_FIELDS = (
    "content_hash",
    "title",
    "location",
    "salary_min",
    "salary_max",
    "salary_currency",
    "salary_period",
    "requirements_json",
)


@dataclass(frozen=True)
class Change:
    field: str
    before: Any
    after: Any

    def as_dict(self) -> dict[str, Any]:
        return {"field": self.field, "before": self.before, "after": self.after}


def snapshot(source: Any) -> dict[str, Any]:
    """The comparable fields of a posting row, a version row, or a mapping."""
    if isinstance(source, Mapping):
        return {name: source.get(name) for name in SNAPSHOT_FIELDS}
    return {name: getattr(source, name, None) for name in SNAPSHOT_FIELDS}


def describe_pay(state: Mapping[str, Any]) -> str:
    low, high = state.get("salary_min"), state.get("salary_max")
    if low is None and high is None:
        return "not stated"
    figures = f"{low:,.0f}" if low == high or high is None else f"{low:,.0f}–{high:,.0f}"
    currency = state.get("salary_currency") or "unstated currency"
    period = state.get("salary_period")
    return f"{figures} {currency}" + (f" per {period}" if period else ", period not stated")


def _skills(state: Mapping[str, Any], bucket: str) -> set[str]:
    reading = state.get("requirements_json") or {}
    return {entry["label"] for entry in (reading.get("skills") or {}).get(bucket) or []}


def _education(state: Mapping[str, Any]) -> str:
    education = (state.get("requirements_json") or {}).get("education")
    if not education:
        return "not stated"
    suffix = " or equivalent experience" if education.get("equivalent_experience") else ""
    return f"{education['requirement']} {education['level'].replace('_', ' ')}{suffix}"


def diff(before: Mapping[str, Any], after: Mapping[str, Any]) -> list[Change]:
    """The changes from `before` to `after`, in the order a person reads a posting."""
    changes: list[Change] = []
    for name in ("title", "location"):
        if (before.get(name) or None) != (after.get(name) or None):
            changes.append(Change(name, before.get(name), after.get(name)))

    if describe_pay(before) != describe_pay(after):
        changes.append(Change("pay", describe_pay(before), describe_pay(after)))

    # A reading from before extraction existed says nothing either way; a
    # missing reading is not "all skills removed".
    if before.get("requirements_json") is not None and after.get("requirements_json") is not None:
        for bucket in ("required", "preferred"):
            old, new = _skills(before, bucket), _skills(after, bucket)
            if old != new:
                changes.append(
                    Change(
                        f"{bucket}_skills",
                        sorted(old - new),
                        sorted(new - old),
                    )
                )
        if _education(before) != _education(after):
            changes.append(Change("education", _education(before), _education(after)))

    if not changes and before.get("content_hash") != after.get("content_hash"):
        changes.append(Change("text", None, None))
    return changes


async def record(
    session: AsyncSession, captures: list[tuple[uuid.UUID, dict[str, Any], datetime]]
) -> int:
    """Append one version per `(posting_id, snapshot, captured_at)`. Does not commit.

    Numbered after the posting's latest stored version, in the order given, so
    a baseline passed before the new reading gets the lower number.
    """
    if not captures:
        return 0
    ids = list({posting_id for posting_id, _, _ in captures})
    latest: dict[uuid.UUID, int] = {
        posting_id: version
        for posting_id, version in (
            await session.execute(
                select(PostingVersion.posting_id, func.max(PostingVersion.version))
                .where(PostingVersion.posting_id.in_(ids))
                .group_by(PostingVersion.posting_id)
            )
        ).all()
    }
    for posting_id, state, captured_at in captures:
        number = latest.get(posting_id, 0) + 1
        latest[posting_id] = number
        session.add(
            PostingVersion(
                posting_id=posting_id,
                version=number,
                captured_at=captured_at,
                **{name: state.get(name) for name in SNAPSHOT_FIELDS},
            )
        )
    await session.flush()
    return len(captures)


async def history(session: AsyncSession, posting_id: uuid.UUID) -> list[PostingVersion]:
    """A posting's versions, oldest first."""
    rows = await session.scalars(
        select(PostingVersion)
        .where(PostingVersion.posting_id == posting_id)
        .order_by(PostingVersion.version)
    )
    return list(rows.all())


__all__ = ["SNAPSHOT_FIELDS", "Change", "describe_pay", "diff", "history", "record", "snapshot"]
