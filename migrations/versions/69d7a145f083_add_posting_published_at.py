"""add posting published_at

Revision ID: 69d7a145f083
Revises: 8623998a43a9
Create Date: 2026-08-18 00:32:03.569614

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "69d7a145f083"
down_revision: str | Sequence[str] | None = "8623998a43a9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # When the source says the posting went up, as distinct from first_seen_at,
    # which is when we noticed. Without both, "are we late?" is unanswerable —
    # and lag is the number that says whether poll_interval_s is set sensibly.
    op.add_column(
        "postings",
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_postings_published_at", "postings", [sa.text("published_at DESC")])


def downgrade() -> None:
    op.drop_index("ix_postings_published_at", table_name="postings")
    op.drop_column("postings", "published_at")
