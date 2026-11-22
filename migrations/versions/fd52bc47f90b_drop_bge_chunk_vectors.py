"""drop bge chunk vectors

Chunks are searched by Qwen3-Embedding-4B alone (`posting_chunk_vectors`), so
each chunk's bge-small vector, its model stamp and their HNSW index go. The
chunk rows stay: they are the offsets every Qwen vector belongs to, and the
cascade from `posting_chunks` would take those vectors with them.

On the owner's database (2026-11-22) the index was 460 MB and the vectors
307 MB. Dropping the index frees its space at once; a dropped column's space
comes back only when the table is rewritten, so run `VACUUM FULL posting_chunks`
once afterwards (it locks the table for as long as the copy takes).

The downgrade cannot bring the vectors back. It restores the columns, nullable,
and stamps every chunk with bge-small's name so the old chunker reads them as
current: a missing stamp would make it cut every posting again, and the cut
deletes the Qwen vectors. Old code then finds no bge-small chunk vector to rank
by; bge-small's one vector per posting is untouched.

Revision ID: fd52bc47f90b
Revises: 68e2a1b945d0
Create Date: 2026-11-22 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import HALFVEC

revision: str = "fd52bc47f90b"
down_revision: str | Sequence[str] | None = "68e2a1b945d0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: `SentenceTransformerEmbedder.name`, as the rows were stamped. Written out so
#: the migration does not change when that class does.
BGE_STAMP = "sentence-transformers"


def upgrade() -> None:
    op.drop_index("ix_posting_chunks_embedding_hnsw", table_name="posting_chunks")
    op.drop_column("posting_chunks", "embedding")
    op.drop_column("posting_chunks", "embedding_model")


def downgrade() -> None:
    op.add_column(
        "posting_chunks",
        sa.Column("embedding_model", sa.String(length=64), nullable=True),
    )
    op.add_column("posting_chunks", sa.Column("embedding", HALFVEC(384), nullable=True))
    op.execute(
        sa.text("UPDATE posting_chunks SET embedding_model = :stamp").bindparams(stamp=BGE_STAMP)
    )
    op.create_index(
        "ix_posting_chunks_embedding_hnsw",
        "posting_chunks",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "halfvec_cosine_ops"},
        postgresql_with={"m": 16, "ef_construction": 200},
    )
