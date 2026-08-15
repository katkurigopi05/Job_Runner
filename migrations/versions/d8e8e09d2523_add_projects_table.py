"""add projects table

Revision ID: d8e8e09d2523
Revises: ad305f4ea884
Create Date: 2026-08-15 03:40:22.616890

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "d8e8e09d2523"
down_revision: str | Sequence[str] | None = "ad305f4ea884"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "projects",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("candidate_id", sa.UUID(), nullable=False),
        sa.Column(
            "source", sa.String(length=30), server_default=sa.text("'github'"), nullable=False
        ),
        sa.Column("external_id", sa.String(length=100), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("full_name", sa.String(length=300), nullable=True),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("homepage", sa.Text(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("language", sa.String(length=50), nullable=True),
        sa.Column(
            "topics_json",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("stars", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("forks", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("is_fork", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("is_archived", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("is_private", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("pushed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("include", sa.Boolean(), nullable=True),
        sa.Column("pinned", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "synced_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["candidate_id"], ["candidates.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("candidate_id", "source", "external_id", name="uq_projects_source_id"),
    )
    op.create_index("ix_projects_candidate", "projects", ["candidate_id"], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_projects_candidate", table_name="projects")
    op.drop_table("projects")
