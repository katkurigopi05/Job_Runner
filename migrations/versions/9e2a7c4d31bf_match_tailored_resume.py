"""A résumé tailored for a posting before any application exists.

Tailoring is the slowest step in the apply pipeline and the only one that
spends a provider's quota. Running it inside `apply_job` makes every
application wait on a model, which at fifty a day is the difference between a
queue that flows and one that stalls on every card.

Keyed on `matches` rather than `applications` because tailoring depends on the
job description, and a Match is already exactly one (profile, posting) pair —
the same key the work is done against. It also means the tailoring can happen
before the owner decides to apply at all.

`SET NULL` on delete: losing the tailored PDF is not a reason to lose the
match, and an application can always fall back to the base résumé.

Revision ID: 9e2a7c4d31bf
Revises: 8d3f1b7c05ae
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "9e2a7c4d31bf"
down_revision = "8d3f1b7c05ae"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "matches",
        sa.Column("tailored_resume_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_matches_tailored_resume",
        "matches",
        "resumes",
        ["tailored_resume_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_matches_tailored_resume", "matches", type_="foreignkey")
    op.drop_column("matches", "tailored_resume_id")
