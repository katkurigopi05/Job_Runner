"""add profile target seniority

Revision ID: b1c4e7a92f30
Revises: a1f6c30b27e4
Create Date: 2026-08-31 07:10:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b1c4e7a92f30"
down_revision: str | Sequence[str] | None = "a1f6c30b27e4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Nullable, with no default and no backfill.

    NULL means "do not filter on level", which is exactly what every existing
    row did before this column existed — `filters.seniority_ok` passes
    everything for a None target. So the migration changes no behaviour, which
    is the point: the rung is the owner's to state (CLAUDE.md §1), and guessing
    it from a résumé would narrow the feed on the strength of an inference
    nobody made.
    """
    op.add_column("profiles", sa.Column("target_seniority", sa.String(length=20), nullable=True))


def downgrade() -> None:
    op.drop_column("profiles", "target_seniority")
