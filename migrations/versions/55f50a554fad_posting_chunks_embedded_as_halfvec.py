"""posting chunks embedded as halfvec

bge-small truncates at 512 tokens and every one of the twelve real crawled
postings is longer, so one vector per posting embedded a median 37% of the
text — and on 4 of 12 the requirements section was entirely outside it. This
table holds the rest. See `packages/matching/chunking.py`.

Additive: `postings.description_embedding` is untouched, because it is what
the feed ranks on and replacing it would re-rank as a side effect of a storage
change.

`halfvec` rather than `vector`, measured at 92,000 rows: half the storage
(135 MB -> 68 MB), identical top-10 in identical order on 40 of 40 queries,
largest distance difference 5.46e-05.

Revision ID: 55f50a554fad
Revises: a1b3c5d7e9f2
Create Date: 2026-10-01 09:38:41.541639

"""

from collections.abc import Sequence

import pgvector.sqlalchemy
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "55f50a554fad"
down_revision: str | Sequence[str] | None = "a1b3c5d7e9f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "posting_chunks",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("posting_id", sa.UUID(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("start_char", sa.Integer(), nullable=False),
        sa.Column("end_char", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("embedding", pgvector.sqlalchemy.HALFVEC(dim=384), nullable=True),
        sa.Column("embedding_model", sa.String(length=64), nullable=True),
        sa.Column("embedding_revision", sa.Integer(), nullable=True),
        sa.CheckConstraint("end_char > start_char", name="ck_posting_chunks_span"),
        sa.ForeignKeyConstraint(["posting_id"], ["postings.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("posting_id", "ordinal", name="uq_posting_chunks_posting_ordinal"),
    )
    op.create_index("ix_posting_chunks_posting_id", "posting_chunks", ["posting_id"], unique=False)

    # No vector index. CLAUDE.md §5 claims an ivfflat on
    # postings.description_embedding and no migration has ever created one, so
    # adding a second unverified claim here would repeat that. An ANN index on
    # this table is a separate, measured decision: pgvector filters *after*
    # index traversal, and a filtered ivfflat query measured returning fewer
    # than 5 rows on 28 of 30 queries. A partial index per embedding space is
    # what that needs.


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_posting_chunks_posting_id", table_name="posting_chunks")
    op.drop_table("posting_chunks")
