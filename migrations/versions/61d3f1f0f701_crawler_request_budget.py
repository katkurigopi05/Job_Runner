"""crawler request budget

How many requests the crawl has made in the current window. One row, pinned to
`id = 1`, shared by every worker.

The rate limiter answers "how fast", per host. Nothing answered "how many", at
all — which is why a bounded pilot could cap companies (`limit=20`) and
wall-clock (`timeout 900`) and simply could not cap requests.

Not seeded. The first reservation inserts the row, so an unused budget leaves
no row at all and `state()` reads zero — which is true rather than assumed.

Unlimited by default (`CRAWLER_REQUEST_BUDGET=0`), so this table stays empty
until someone deliberately sets a ceiling for a run they are watching.

Revision ID: 61d3f1f0f701
Revises: 531b51cd6279
Create Date: 2026-09-13 06:43:26.500712

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "61d3f1f0f701"
down_revision: str | Sequence[str] | None = "531b51cd6279"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the single-row budget table."""
    op.create_table(
        "crawler_request_budgets",
        sa.Column("id", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("window_started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("requests", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    """Drop it. The ceiling is a setting; nothing else reads this."""
    op.drop_table("crawler_request_budgets")
