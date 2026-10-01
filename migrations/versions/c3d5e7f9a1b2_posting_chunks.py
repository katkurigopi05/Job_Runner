"""posting chunks

Fixed 500-character windows of each posting's description, with a float16
vector each, so search reads the whole posting rather than the first 512
tokens bge-small can see. New table only; no existing row changes.

Revision ID: c3d5e7f9a1b2
Revises: a1b3c5d7e9f2
Create Date: 2026-10-01 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import HALFVEC
from sqlalchemy.dialects import postgresql

revision: str = "c3d5e7f9a1b2"
down_revision: str | Sequence[str] | None = "a1b3c5d7e9f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "posting_chunks",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("posting_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("start", sa.Integer(), nullable=False),
        sa.Column("length", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=True),
        sa.Column("embedding_model", sa.String(length=64), nullable=False),
        sa.Column("embedding", HALFVEC(384), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["posting_id"], ["postings.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("posting_id", "ordinal", name="uq_posting_chunks_posting_ordinal"),
    )
    op.create_index("ix_posting_chunks_posting_id", "posting_chunks", ["posting_id"])


def downgrade() -> None:
    op.drop_index("ix_posting_chunks_posting_id", table_name="posting_chunks")
    op.drop_table("posting_chunks")
