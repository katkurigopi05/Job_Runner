"""add inbound message link provenance

Revision ID: 070f85762037
Revises: 8623998a43a9
Create Date: 2026-08-19 00:35:04.201240

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "070f85762037"
down_revision: str | Sequence[str] | None = "8623998a43a9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "inbound_messages",
        sa.Column(
            "link_method",
            sa.String(length=20),
            server_default=sa.text("'unlinked'"),
            nullable=False,
        ),
    )
    op.add_column("inbound_messages", sa.Column("link_confidence", sa.Float(), nullable=True))

    # Every message stored before this migration reached its application
    # through the alias, because that was the only path there was. Leaving
    # them on the "unlinked" default would misdescribe history and, worse,
    # make exact links look like guesses.
    op.execute("UPDATE inbound_messages SET link_method = 'alias' WHERE application_id IS NOT NULL")


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("inbound_messages", "link_confidence")
    op.drop_column("inbound_messages", "link_method")
