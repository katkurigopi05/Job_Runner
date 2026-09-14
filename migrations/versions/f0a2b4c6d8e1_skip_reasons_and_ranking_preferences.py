"""skip reasons and explicit ranking preferences

`matches.skip_reason` and `matches.decision_note` record why the owner skipped
a posting; `ranking_preferences` holds bounded, explicit adjustments to the
feed order. `matches.score` — the base score — is not touched, and no
existing decision changes.

Revision ID: f0a2b4c6d8e1
Revises: e9f1a3b5c7d9
Create Date: 2026-09-14 15:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "f0a2b4c6d8e1"
down_revision: str | Sequence[str] | None = "e9f1a3b5c7d9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("matches", sa.Column("skip_reason", sa.String(length=30), nullable=True))
    op.add_column("matches", sa.Column("decision_note", sa.Text(), nullable=True))
    op.create_table(
        "ranking_preferences",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column(
            "scope", sa.String(length=64), server_default=sa.text("'global'"), nullable=False
        ),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("value", sa.String(length=200), nullable=False),
        sa.Column("weight", sa.Float(), nullable=False),
        sa.Column(
            "source", sa.String(length=20), server_default=sa.text("'explicit'"), nullable=False
        ),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "kind IN ('company', 'skill', 'title_term', 'location_term', 'remote')",
            name="ck_ranking_preferences_kind",
        ),
        sa.CheckConstraint("weight BETWEEN -0.3 AND 0.3", name="ck_ranking_preferences_weight"),
        sa.CheckConstraint(
            "source IN ('explicit', 'suggestion')", name="ck_ranking_preferences_source"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "scope", "kind", "value", name="uq_ranking_preferences_scope_kind_value"
        ),
    )


def downgrade() -> None:
    op.drop_table("ranking_preferences")
    op.drop_column("matches", "decision_note")
    op.drop_column("matches", "skip_reason")
