"""posting chunk vectors

A second, larger vector per chunk: Qwen3-Embedding-4B's 2,560 dimensions, made
on a Colab TPU and imported with `make import-chunk-vectors`. New table only;
no existing row changes, and deleting a chunk deletes its vectors.

Revision ID: d8f0a2c4e6b8
Revises: c4e6a8b0d2f4
Create Date: 2026-11-02 18:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import HALFVEC
from sqlalchemy.dialects import postgresql

revision: str = "d8f0a2c4e6b8"
down_revision: str | Sequence[str] | None = "c4e6a8b0d2f4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "posting_chunk_vectors",
        sa.Column("chunk_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("embedding_model", sa.String(length=64), nullable=False),
        sa.Column("embedding", HALFVEC(2560), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["chunk_id"], ["posting_chunks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("chunk_id", "embedding_model"),
    )


def downgrade() -> None:
    op.drop_table("posting_chunk_vectors")
