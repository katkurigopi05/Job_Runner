"""Posting.title and Posting.location become Text.

The first crawl against the real database aborted on this. A Greenhouse
posting at SumUp lists every US state the role covers — 561 characters of
location against a `varchar(300)` column. Postgres did not truncate it;
asyncpg raised `StringDataRightTruncationError`, the flush failed, and the
whole cycle died, losing 108 other companies' postings to one row.

Both columns hold text an employer types into a form with no length limit.
There is no correct maximum, so there is no maximum. `url` was already `Text`
for the same reason.

Widening is not a rewrite in Postgres — `varchar(n)` to `text` is a metadata
change, so this is fast on any table size and does not take an exclusive lock
for long.

Revision ID: 7c1e4a2b9d30
Revises: 60a5b9e8d46f
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "7c1e4a2b9d30"
down_revision = "60a5b9e8d46f"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("postings", "title", type_=sa.Text(), existing_nullable=True)
    op.alter_column("postings", "location", type_=sa.Text(), existing_nullable=True)


def downgrade() -> None:
    # Narrowing can fail on rows that are already too long — which is the whole
    # reason this migration exists. Truncate rather than error, because a
    # downgrade that cannot run is not a downgrade.
    op.execute("UPDATE postings SET title = left(title, 500) WHERE length(title) > 500")
    op.execute("UPDATE postings SET location = left(location, 300) WHERE length(location) > 300")
    op.alter_column("postings", "title", type_=sa.String(500), existing_nullable=True)
    op.alter_column("postings", "location", type_=sa.String(300), existing_nullable=True)
