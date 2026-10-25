"""What a repository's dependency files declare, and the résumé entry it backs.

Revision ID: b7d9f1a3c5e2
Revises: a6c8e0f2b4d7
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b7d9f1a3c5e2"
down_revision: str | None = "a6c8e0f2b4d7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "projects",
        sa.Column(
            "dependency_technologies",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column("projects", sa.Column("dependencies_pushed_at", sa.DateTime(timezone=True)))
    op.add_column("projects", sa.Column("resume_entry", sa.Text()))


def downgrade() -> None:
    # The owner's links are lost; the dependency facts are read again by a sync.
    op.drop_column("projects", "resume_entry")
    op.drop_column("projects", "dependencies_pushed_at")
    op.drop_column("projects", "dependency_technologies")
