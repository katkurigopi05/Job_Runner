"""posting salary and structured requirements

Adds the columns `matching/requirements.py` fills at ingestion: salary range,
currency and period, and a JSON reading of required, preferred and
unclassified skills and education, each with its evidence quote.

**No backfill in the migration.** Extraction is regex work over every stored
description (14,892 on the owner's database), which belongs in a resumable
command rather than inside a DDL transaction: `make extract-requirements`.
Until it runs, NULL `requirements_version` reads as "not extracted", and the
filters treat those postings exactly like postings that state nothing —
unknown, never satisfied.

Additive only; no existing value changes.

Revision ID: c7d2e9a1b4f6
Revises: b3f0c1d2e4a5
Create Date: 2026-09-14 11:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c7d2e9a1b4f6"
down_revision: str | Sequence[str] | None = "b3f0c1d2e4a5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("postings", sa.Column("salary_min", sa.Float(), nullable=True))
    op.add_column("postings", sa.Column("salary_max", sa.Float(), nullable=True))
    op.add_column("postings", sa.Column("salary_currency", sa.String(length=3), nullable=True))
    op.add_column("postings", sa.Column("salary_period", sa.String(length=10), nullable=True))
    op.add_column(
        "postings",
        sa.Column("requirements_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column("postings", sa.Column("requirements_version", sa.Integer(), nullable=True))
    op.add_column(
        "postings",
        sa.Column("requirements_extracted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_check_constraint(
        "ck_postings_salary_period",
        "postings",
        "salary_period IS NULL OR salary_period IN ('hour', 'day', 'week', 'month', 'year')",
    )
    op.create_check_constraint(
        "ck_postings_salary_order",
        "postings",
        "salary_min IS NULL OR salary_max IS NULL OR salary_min <= salary_max",
    )
    op.create_index(
        "ix_postings_requirements_version", "postings", ["requirements_version"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_postings_requirements_version", table_name="postings")
    op.drop_constraint("ck_postings_salary_order", "postings", type_="check")
    op.drop_constraint("ck_postings_salary_period", "postings", type_="check")
    for column in (
        "requirements_extracted_at",
        "requirements_version",
        "requirements_json",
        "salary_period",
        "salary_currency",
        "salary_max",
        "salary_min",
    ):
        op.drop_column("postings", column)
