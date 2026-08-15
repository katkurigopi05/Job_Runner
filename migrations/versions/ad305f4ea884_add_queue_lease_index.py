"""add queue lease index

Revision ID: ad305f4ea884
Revises: 01a136f0838f
Create Date: 2026-08-15 03:21:50.212318

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "ad305f4ea884"
down_revision: str | Sequence[str] | None = "01a136f0838f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # The reclaim half of a claim looks for expired leases; without this it
    # is a sequential scan of every running task.
    op.create_index(
        "ix_queue_tasks_status_lease", "queue_tasks", ["status", "lease_expires_at"], unique=False
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_queue_tasks_status_lease", table_name="queue_tasks")
