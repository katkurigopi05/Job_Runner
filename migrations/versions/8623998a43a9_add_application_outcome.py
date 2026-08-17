"""add application outcome

Revision ID: 8623998a43a9
Revises: f31201519b23
Create Date: 2026-08-17 04:27:08.753758

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "8623998a43a9"
down_revision: str | Sequence[str] | None = "f31201519b23"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("applications", sa.Column("outcome", sa.String(length=30), nullable=True))
    op.add_column(
        "applications", sa.Column("outcome_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("applications", "outcome_at")
    op.drop_column("applications", "outcome")
