"""per host request cost

What each host actually cost us, split into the half we chose and the half we
did not: `waited_seconds` is time inside the rate limiter (§2.6, a rule being
obeyed) and `network_seconds` is time in the request itself.

Counters on the existing per-host row rather than a new table or an in-process
tally. `make workers n=4` is four processes, so an in-process counter reports a
quarter of the truth to each of them and none of them says so.

Server defaults of 0 rather than a backfill, and the distinction matters: an
existing row has genuinely served requests we did not measure, so 0 means "not
counted yet" and the totals are honest from the next request onward rather than
retroactively invented.

Revision ID: 531b51cd6279
Revises: 0d73f6d35422
Create Date: 2026-09-13 06:33:01.518784

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "531b51cd6279"
down_revision: str | Sequence[str] | None = "0d73f6d35422"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the counters. Server-defaulted, never backfilled."""
    op.add_column(
        "crawler_host_budgets",
        sa.Column("requests", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )
    op.add_column(
        "crawler_host_budgets",
        sa.Column("waited_seconds", sa.Float(), server_default=sa.text("0"), nullable=False),
    )
    op.add_column(
        "crawler_host_budgets",
        sa.Column("network_seconds", sa.Float(), server_default=sa.text("0"), nullable=False),
    )


def downgrade() -> None:
    """Drop them. Nothing else holds these totals."""
    op.drop_column("crawler_host_budgets", "network_seconds")
    op.drop_column("crawler_host_budgets", "waited_seconds")
    op.drop_column("crawler_host_budgets", "requests")
