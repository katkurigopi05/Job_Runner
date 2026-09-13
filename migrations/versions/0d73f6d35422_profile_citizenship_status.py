"""profile citizenship status

Current work authorization, separated from future sponsorship need.

`profiles.needs_sponsorship` was answering both questions, and they are not the
same question. A permanent resident needs no sponsorship and still fails a
posting restricted to US citizens; somebody on OPT is authorized today and may
need sponsorship later. With one boolean there was nothing for a citizenship
filter to read, so "US citizens only" was not filtered on at all.

**Deliberately not backfilled.** NULL means *unstated*, and the filter then
flags an explicit restriction instead of excluding on it. The data that could
seed a guess is `work_auth`, a free-text field §2.2 keeps verbatim for forms —
parsing a legal status out of it to decide which jobs the owner ever sees would
be an inference nobody made, on the one subject where being wrong has legal
consequences. An honest gap is better, and the screen says "Unknown — verify
with employer" rather than implying the question was settled.

Existing profile data is untouched: this only adds a nullable column.

Revision ID: 0d73f6d35422
Revises: 53964fd524ac
Create Date: 2026-09-13 00:30:50.045481

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0d73f6d35422"
down_revision: str | Sequence[str] | None = "53964fd524ac"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the column. No backfill — see the module docstring."""
    op.add_column("profiles", sa.Column("citizenship_status", sa.String(length=30), nullable=True))
    # A CHECK rather than a Postgres enum, matching `companies.source_status`:
    # adding a value to an enum type is a migration, and a typo in an unchecked
    # string column would read as "no preference" and silently unfilter the feed.
    op.create_check_constraint(
        "ck_profiles_citizenship_status",
        "profiles",
        "citizenship_status IS NULL OR citizenship_status IN "
        "('us_citizen', 'permanent_resident', 'other_authorized', 'not_authorized')",
    )


def downgrade() -> None:
    """Drop it. Nothing else held this fact, so nothing else has to be restored."""
    op.drop_constraint("ck_profiles_citizenship_status", "profiles", type_="check")
    op.drop_column("profiles", "citizenship_status")
