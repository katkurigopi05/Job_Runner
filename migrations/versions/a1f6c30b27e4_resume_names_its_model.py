"""resumes.tailored_by

§7 records `answered_by` on the provider so a résumé written by llama3.1 after
the remote allowance ran out can be told apart from one written by Gemini. It
lived on the provider object and died with the run, so the review screen — the
one place the distinction matters, because that is where the owner approves the
document — had no way to show it.

Stored on the résumé rather than on the application: a cache hit and an
overnight batch serve one row to several applications, and the model that wrote
the document is a property of the document.

Revision ID: a1f6c30b27e4
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a1f6c30b27e4"
down_revision = "c8d2f6b34a17"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Nullable with no backfill and no default. Every résumé that exists today
    # was tailored before this column did, and there is no record of which
    # model wrote them — NULL says "unrecorded", which is true. Backfilling a
    # plausible provider name would turn an unknown into a confident wrong
    # answer on a document the owner is about to send to an employer.
    op.add_column("resumes", sa.Column("tailored_by", sa.String(length=120), nullable=True))


def downgrade() -> None:
    op.drop_column("resumes", "tailored_by")
