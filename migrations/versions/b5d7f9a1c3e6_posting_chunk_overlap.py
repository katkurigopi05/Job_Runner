"""posting chunk overlap

Each chunk records the overlap it was cut at. Every row so far was cut at 25
characters, DP-800's `OVERLAP = 25` read as characters; it is a percent, so
chunks are cut at 125 from now on, and a row cut at any other overlap is stale.
The default keeps the old rows truthful, and a column with a constant default
is added without rewriting the table.

Revision ID: b5d7f9a1c3e6
Revises: e1a3c5e7f9b2
Create Date: 2026-11-06 00:30:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b5d7f9a1c3e6"
down_revision: str | Sequence[str] | None = "e1a3c5e7f9b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "posting_chunks",
        sa.Column("overlap", sa.SmallInteger(), nullable=False, server_default=sa.text("25")),
    )


def downgrade() -> None:
    op.drop_column("posting_chunks", "overlap")
