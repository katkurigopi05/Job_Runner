"""Add evidence ordering after the already-published Studio revision.

Existing evidence remains unchanged and receives position zero. The column
check also accepts development databases that applied an interim b7 draft
containing position, retaining their ordering values.

Revision ID: c8e0f2a4b6d9
Revises: b7d9f1a3c5e8
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c8e0f2a4b6d9"
down_revision: str | Sequence[str] | None = "b7d9f1a3c5e8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    for table in ("internships", "certifications"):
        if "position" not in {column["name"] for column in inspector.get_columns(table)}:
            op.add_column(
                table,
                sa.Column(
                    "position", sa.SmallInteger(), server_default=sa.text("0"), nullable=False
                ),
            )


def downgrade() -> None:
    # Only ordering introduced by this revision is removed. Evidence rows,
    # their confirmation flags, and the original tables are preserved.
    for table in ("internships", "certifications"):
        op.drop_column(table, "position")
