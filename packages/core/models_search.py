"""Saved searches — what the owner wants to see, kept apart from who they are.

CLAUDE.md §1: filters are the owner's input, not a reading of their profile.
`Profile` fields are typed onto real applications (§2.2); a saved search
changes only the feed. Storing "at least $150k" or "I hold a bachelor's" on
the profile would put a search preference one edit away from a form answer,
so it lives here, in its own table, and nothing in the apply path reads it.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, String, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from packages.core.models import Base


class SearchPreference(Base):
    """One named, saved set of feed filters."""

    __tablename__ = "search_preferences"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    #: `global`, or `profile:<uuid>` for a search tied to one profile's feed.
    #: A string rather than a nullable foreign key so "the global default" is
    #: a unique key Postgres enforces, which a NULL in a unique index is not.
    scope: Mapped[str] = mapped_column(String(64), nullable=False, server_default=text("'global'"))
    name: Mapped[str] = mapped_column(String(100), nullable=False, server_default=text("'default'"))
    #: Query parameters exactly as `/matches` accepts them.
    filters_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (UniqueConstraint("scope", "name", name="uq_search_preferences_scope_name"),)


__all__ = ["SearchPreference"]
