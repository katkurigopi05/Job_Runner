"""Lower per-host delays stored at the old 60 s floor to 5 s (§2.6, 2026-11-02).

The owner set our own delay to 5 to 60 s a host, 5 by default.
`crawler_host_budgets.delay_seconds` only ratchets up: a reservation takes the
greater of the stored delay and the one it asks for. So lowering the floor in
code changed nothing for a host already stored at 60 s, and 6,080 were.

A row above 60 is a site's own Crawl-delay and is left as it is, and so is a
shared API at its 2 s floor. A Crawl-delay of exactly 60 is lowered here and
written back the next time that site's robots.txt is read.

Revision ID: c4e6a8b0d2f4
Revises: b7d9f1a3c5e2
"""

from collections.abc import Sequence

from alembic import op

revision: str = "c4e6a8b0d2f4"
down_revision: str | None = "b7d9f1a3c5e2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("UPDATE crawler_host_budgets SET delay_seconds = 5 WHERE delay_seconds = 60")


def downgrade() -> None:
    # Back to the 60 s floor: every host at the new floor returns to the old one.
    op.execute("UPDATE crawler_host_budgets SET delay_seconds = 60 WHERE delay_seconds = 5")
