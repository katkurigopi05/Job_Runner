"""resumes.tailored_for_posting_id

A tailored résumé recorded `tailored_key` — a sha256 over résumé, posting,
prompt, projects, provider and model — which identifies a cache entry and
cannot be read back into a job. Naming the posting the document was written for
took a reverse join through `applications` or `matches`, and a résumé attached
to neither could not be traced at all.

Revision ID: c8d2f6b34a17
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "c8d2f6b34a17"
down_revision = "b4c7e1a9d052"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "resumes",
        sa.Column("tailored_for_posting_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_resumes_tailored_for_posting",
        "resumes",
        "postings",
        ["tailored_for_posting_id"],
        ["id"],
        # Not CASCADE. A posting can be pruned when a board drops it; the
        # résumé that went to an employer is a record of what was sent and
        # outlives the listing.
        ondelete="SET NULL",
    )
    op.create_index("ix_resumes_tailored_for_posting_id", "resumes", ["tailored_for_posting_id"])


def downgrade() -> None:
    op.drop_index("ix_resumes_tailored_for_posting_id", table_name="resumes")
    op.drop_constraint("fk_resumes_tailored_for_posting", "resumes", type_="foreignkey")
    op.drop_column("resumes", "tailored_for_posting_id")
