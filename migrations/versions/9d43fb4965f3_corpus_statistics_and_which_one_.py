"""corpus statistics, and which one produced a vector

Revision ID: 9d43fb4965f3
Revises: 9e2a7c4d31bf
Create Date: 2026-08-22 03:04:16.438701

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "9d43fb4965f3"
down_revision: str | Sequence[str] | None = "9e2a7c4d31bf"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the stats table and the stamp that says which produced a vector.

    Existing vectors are deliberately left un-stamped. NULL is honest: they
    were written before anything recorded the model, so nobody knows which
    produced them. The first scoring pass re-embeds them, which costs one
    pass and is the only way to be sure the feed is comparing like with like.
    """
    """Upgrade schema."""
    op.create_table(
        "corpus_stats",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("total_documents", sa.Integer(), nullable=False),
        sa.Column("counts_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("revision"),
    )
    op.add_column("postings", sa.Column("embedding_model", sa.String(length=64), nullable=True))
    op.add_column("postings", sa.Column("embedding_revision", sa.Integer(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("postings", "embedding_revision")
    op.drop_column("postings", "embedding_model")
    op.drop_table("corpus_stats")
