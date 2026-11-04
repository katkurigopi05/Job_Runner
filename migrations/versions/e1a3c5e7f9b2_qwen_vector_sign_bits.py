"""qwen vector sign bits

A generated `bit(2560)` column holding each Qwen chunk vector's sign bits, for
a 1-bit shortlist before exact re-ranking. Postgres computes it from
`embedding`, so it can never disagree with the vector. Adding it rewrites the
table once (about 1.3 GB on the owner's database).

Revision ID: e1a3c5e7f9b2
Revises: d8f0a2c4e6b8
Create Date: 2026-11-03 15:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import BIT

revision: str = "e1a3c5e7f9b2"
down_revision: str | Sequence[str] | None = "d8f0a2c4e6b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "posting_chunk_vectors",
        sa.Column(
            "embedding_bits",
            BIT(2560),
            sa.Computed("binary_quantize(embedding)::bit(2560)", persisted=True),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("posting_chunk_vectors", "embedding_bits")
