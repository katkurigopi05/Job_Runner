"""Company issues: companies a worker could not read, and the deep dive's findings.

Revision ID: e4b7c2d9a1f3
Revises: d3a9f6c1b742
Create Date: 2026-10-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "e4b7c2d9a1f3"
down_revision: str | None = "d3a9f6c1b742"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "company_issues",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("stage", sa.String(length=16), nullable=False),
        sa.Column("category", sa.String(length=40), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="noted", nullable=False),
        sa.Column("times_noted", sa.Integer(), server_default="1", nullable=False),
        sa.Column(
            "noted_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "last_noted_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("queued_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deep_dived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "findings",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("diagnosis", sa.Text(), nullable=True),
        sa.Column("fix", sa.Text(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_by", sa.String(length=16), nullable=True),
        sa.CheckConstraint("stage IN ('discovery', 'crawl')", name="ck_company_issues_stage"),
        sa.CheckConstraint(
            "status IN ('noted', 'queued', 'fixed', 'needs_owner')",
            name="ck_company_issues_status",
        ),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_company_issues_open",
        "company_issues",
        ["company_id", "stage"],
        unique=True,
        postgresql_where=sa.text("status <> 'fixed'"),
    )
    op.create_index(
        "ix_company_issues_status_noted", "company_issues", ["status", "noted_at"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_company_issues_status_noted", table_name="company_issues")
    op.drop_index("uq_company_issues_open", table_name="company_issues")
    op.drop_table("company_issues")
