"""Queue tasks record when they finished; `cancelled` is a status.

Revision ID: f2c8d4e6a9b1
Revises: e4b7c2d9a1f3
Create Date: 2026-10-19

`finished_at` is left NULL on tasks finished before it existed rather than
guessed: pruning ages those by `created_at`, and the crawl status falls back to
`locked_at`. `status` is a plain string column, so `cancelled` needs no change.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f2c8d4e6a9b1"
down_revision: str | None = "e4b7c2d9a1f3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("queue_tasks", sa.Column("finished_at", sa.DateTime(timezone=True)))
    op.create_index(
        "ix_queue_tasks_status_finished", "queue_tasks", ["status", "finished_at"], unique=False
    )


def downgrade() -> None:
    # A cancelled task has no meaning to the code before this revision, which
    # would read it as neither pending nor finished. Recorded as failed.
    op.execute(
        "UPDATE queue_tasks SET status = 'failed', "
        "last_error = coalesce(last_error, 'cancelled') WHERE status = 'cancelled'"
    )
    op.drop_index("ix_queue_tasks_status_finished", table_name="queue_tasks")
    op.drop_column("queue_tasks", "finished_at")
