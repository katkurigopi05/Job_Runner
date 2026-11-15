"""Optional query backend: 384-dimensional cosine HNSW index."""

from alembic import op

revision = "d7e9f1a3b5c7"
down_revision = "a3c5e7f9b1d2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_posting_chunks_embedding_hnsw",
        "posting_chunks",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "halfvec_cosine_ops"},
        postgresql_with={"m": 16, "ef_construction": 200},
    )


def downgrade() -> None:
    op.drop_index("ix_posting_chunks_embedding_hnsw", table_name="posting_chunks")
