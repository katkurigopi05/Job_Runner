"""applied_postings_ledger

Durable submit fence, saved BEFORE contacting an employer.
Prevents duplicate application submissions across worker retries.

Revision ID: 14ebc80a6ecb
Revises: e5f7a9b1c2d4
Create Date: 2026-10-13 23:47:20.384823

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "14ebc80a6ecb"
down_revision: str | Sequence[str] | None = "e5f7a9b1c2d4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "applied_postings",
        sa.Column("application_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("candidate_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("posting_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("job_key", sa.String(length=64), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("state", sa.String(length=20), nullable=False),
        sa.Column("source", sa.String(length=20), nullable=False),
        sa.Column(
            "attempted_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["application_id"], ["applications.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["candidate_id"], ["candidates.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["posting_id"], ["postings.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("application_id"),
        sa.UniqueConstraint("candidate_id", "job_key", name="uq_applied_postings_candidate_job"),
        sa.CheckConstraint(
            "state IN ('attempting', 'uncertain', 'submitted', 'released')",
            name="ck_applied_postings_state",
        ),
    )
    op.create_index("ix_applied_postings_posting_id", "applied_postings", ["posting_id"])


def downgrade() -> None:
    if op.get_bind().scalar(sa.text("SELECT EXISTS (SELECT 1 FROM applied_postings)")):
        raise RuntimeError(
            "refusing to drop nonempty submission ledger; preserve/export submission "
            "history and explicitly plan rollback before removing duplicate protection"
        )
    op.drop_index("ix_applied_postings_posting_id", table_name="applied_postings")
    op.drop_table("applied_postings")
