"""saved search preferences

Named feed filters, kept out of `profiles` on purpose — CLAUDE.md §1 separates
what the owner wants to see from what goes onto an application. A new table;
nothing existing changes.

Revision ID: d8e3f0b2c5a7
Revises: c7d2e9a1b4f6
Create Date: 2026-09-14 11:05:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "d8e3f0b2c5a7"
down_revision: str | Sequence[str] | None = "c7d2e9a1b4f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "search_preferences",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column(
            "scope", sa.String(length=64), server_default=sa.text("'global'"), nullable=False
        ),
        sa.Column(
            "name", sa.String(length=100), server_default=sa.text("'default'"), nullable=False
        ),
        sa.Column(
            "filters_json",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("scope", "name", name="uq_search_preferences_scope_name"),
    )


def downgrade() -> None:
    op.drop_table("search_preferences")
