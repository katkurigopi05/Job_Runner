"""Retrieval judgments: the owner's grades of chat search results.

Revision ID: d3a9f6c1b742
Revises: c4e8a1b7d290
Create Date: 2026-10-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "d3a9f6c1b742"
down_revision: str | None = "c4e8a1b7d290"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "retrieval_judgments",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("set_version", sa.String(length=32), nullable=False),
        sa.Column("question_key", sa.String(length=64), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("stratum", sa.String(length=32), nullable=False),
        sa.Column("held_out", sa.Boolean(), nullable=False),
        sa.Column("posting_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("pool_order", sa.Integer(), nullable=False),
        sa.Column("surfaced_by", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=True),
        sa.Column("relevance", sa.Integer(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("graded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "relevance IS NULL OR relevance BETWEEN 0 AND 2",
            name="ck_retrieval_judgments_relevance_scale",
        ),
        sa.ForeignKeyConstraint(["posting_id"], ["postings.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "set_version",
            "question_key",
            "posting_id",
            name="uq_retrieval_judgments_question_posting",
        ),
    )
    op.create_index(
        "ix_retrieval_judgments_posting", "retrieval_judgments", ["posting_id"], unique=False
    )
    op.create_index(
        "ix_retrieval_judgments_set_question",
        "retrieval_judgments",
        ["set_version", "question_key"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_retrieval_judgments_set_question", table_name="retrieval_judgments")
    op.drop_index("ix_retrieval_judgments_posting", table_name="retrieval_judgments")
    op.drop_table("retrieval_judgments")
