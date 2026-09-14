"""worker heartbeats

One row per queue worker, upserted every few seconds, so the setup page can
answer "is the worker running" — which nothing could before. The crawl status
knew when work was queued with nobody holding it, but an idle worker and a
dead one were indistinguishable.

Additive only: a new table, no existing row touched.

Revision ID: b3f0c1d2e4a5
Revises: 7a1c94b8e3d2
Create Date: 2026-09-14 09:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "b3f0c1d2e4a5"
down_revision: str | Sequence[str] | None = "7a1c94b8e3d2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "worker_heartbeats",
        sa.Column("worker_id", sa.String(length=200), nullable=False),
        sa.Column("hostname", sa.String(length=255), nullable=False),
        sa.Column("pid", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("current_task_kind", sa.String(length=50), nullable=True),
        sa.Column("current_task_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("tasks_completed", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("last_error_kind", sa.String(length=100), nullable=True),
        sa.Column("stopped_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("worker_id"),
    )
    op.create_index(
        "ix_worker_heartbeats_last_seen_at", "worker_heartbeats", ["last_seen_at"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_worker_heartbeats_last_seen_at", table_name="worker_heartbeats")
    op.drop_table("worker_heartbeats")
