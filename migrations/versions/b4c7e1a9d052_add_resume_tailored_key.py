"""add resumes.tailored_key

Reuse of a tailored résumé is keyed on this. Nullable because every résumé that
already exists was tailored before the key existed — and a NULL must read as
"do not reuse", which it does: the lookup matches on an exact key and NULL
never equals anything.

Revision ID: b4c7e1a9d052
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "b4c7e1a9d052"
down_revision = "9e2a7c4d31bf"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("resumes", sa.Column("tailored_key", sa.String(length=64), nullable=True))
    # Every apply does this lookup before deciding whether to call the model,
    # so it is on the hot path rather than a reporting convenience.
    op.create_index("ix_resumes_tailored_key", "resumes", ["tailored_key"])


def downgrade() -> None:
    op.drop_index("ix_resumes_tailored_key", table_name="resumes")
    op.drop_column("resumes", "tailored_key")
