"""Explicit ranking preferences — the owner's own adjustments to the order.

`Match.score` is the base score and is never rewritten. A preference adds or
subtracts a bounded amount for postings it names, at read time, and every
application of one is shown on the card. Nothing here is learned: a learned
reranker would need held-out owner labels to justify itself, and
`packages/matching/personalize.py` refuses to claim one exists.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Float, String, Text, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from packages.core.models import Base


class RankingPreference(Base):
    __tablename__ = "ranking_preferences"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    #: `global` or `profile:<uuid>`, like saved searches.
    scope: Mapped[str] = mapped_column(String(64), nullable=False, server_default=text("'global'"))
    #: `company`, `skill`, `title_term`, `location_term` or `remote`.
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    #: A company name, a skill vocabulary key, a word, or `remote` / `onsite`.
    value: Mapped[str] = mapped_column(String(200), nullable=False)
    #: Added to the base score where it applies. Bounded so no one preference
    #: can outrank the fit it is adjusting.
    weight: Mapped[float] = mapped_column(Float, nullable=False)
    #: `explicit`, or `suggestion` when accepted from the skip-reason suggestions.
    source: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=text("'explicit'")
    )
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("scope", "kind", "value", name="uq_ranking_preferences_scope_kind_value"),
        CheckConstraint(
            "kind IN ('company', 'skill', 'title_term', 'location_term', 'remote')",
            name="ck_ranking_preferences_kind",
        ),
        CheckConstraint("weight BETWEEN -0.3 AND 0.3", name="ck_ranking_preferences_weight"),
        CheckConstraint(
            "source IN ('explicit', 'suggestion')", name="ck_ranking_preferences_source"
        ),
    )


__all__ = ["RankingPreference"]
