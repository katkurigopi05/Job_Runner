"""Session-scoped Qwen evidence vectors."""

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import HALFVEC

revision = "68e2a1b945d0"
down_revision = "71ad2ce490f6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "interview_vectors",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "plan_id",
            sa.Uuid(),
            sa.ForeignKey("interview_plans.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("source_id", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(40), nullable=False),
        sa.Column("model", sa.String(64), nullable=False),
        sa.Column("embedding", HALFVEC(2560), nullable=False),
        sa.UniqueConstraint("plan_id", "source_id", "model", name="uq_interview_vector_source"),
    )
    op.create_index("ix_interview_vectors_plan_id", "interview_vectors", ["plan_id"])


def downgrade() -> None:
    op.drop_table("interview_vectors")
