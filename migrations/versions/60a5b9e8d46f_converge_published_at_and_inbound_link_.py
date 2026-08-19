"""Converge two migration heads.

Two branches added a column at the same time -- `postings.published_at` for
freshness tracking, and inbound-message link provenance -- and both were
written against the same parent, so alembic saw two heads and refused to
upgrade. Nothing to do but join them; neither touches the other's table.

Revision ID: 60a5b9e8d46f
Revises: 070f85762037, 69d7a145f083
Create Date: 2026-08-18 23:23:36.501825

"""

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "60a5b9e8d46f"
down_revision: str | Sequence[str] | None = ("070f85762037", "69d7a145f083")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
