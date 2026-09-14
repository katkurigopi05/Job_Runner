"""canonical jobs and posting versions

`canonical_jobs` groups listings confidently identified as the same
requisition across sources; `postings.canonical_job_id` is the link and
`postings.canonical_locked` records an owner's decision to keep one apart.
`posting_versions` keeps what a posting said before each change.

**Nothing existing is merged or rewritten here.** Grouping is decided by
`make canonicalize`, which reports what it attaches; versions start being
recorded by the crawler, with a baseline captured the first time a stored
posting changes. Every posting, match and application row is untouched.

Revision ID: e9f1a3b5c7d9
Revises: d8e3f0b2c5a7
Create Date: 2026-09-14 13:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "e9f1a3b5c7d9"
down_revision: str | Sequence[str] | None = "d8e3f0b2c5a7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "canonical_jobs",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("title_key", sa.String(length=300), nullable=False),
        sa.Column("requisition_id", sa.String(length=64), nullable=True),
        sa.Column(
            "evidence_json",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_canonical_jobs_company_title", "canonical_jobs", ["company_id", "title_key"]
    )
    op.create_table(
        "posting_versions",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("posting_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=True),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("location", sa.Text(), nullable=True),
        sa.Column("salary_min", sa.Float(), nullable=True),
        sa.Column("salary_max", sa.Float(), nullable=True),
        sa.Column("salary_currency", sa.String(length=3), nullable=True),
        sa.Column("salary_period", sa.String(length=10), nullable=True),
        sa.Column("requirements_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.ForeignKeyConstraint(["posting_id"], ["postings.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("posting_id", "version", name="uq_posting_versions_posting_version"),
    )
    op.create_index("ix_posting_versions_posting_id", "posting_versions", ["posting_id"])
    op.add_column(
        "postings", sa.Column("canonical_job_id", postgresql.UUID(as_uuid=True), nullable=True)
    )
    op.add_column(
        "postings",
        sa.Column(
            "canonical_locked", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
    )
    op.create_foreign_key(
        "postings_canonical_job_id_fkey",
        "postings",
        "canonical_jobs",
        ["canonical_job_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_postings_canonical_job_id", "postings", ["canonical_job_id"])


def downgrade() -> None:
    op.drop_index("ix_postings_canonical_job_id", table_name="postings")
    op.drop_constraint("postings_canonical_job_id_fkey", "postings", type_="foreignkey")
    op.drop_column("postings", "canonical_locked")
    op.drop_column("postings", "canonical_job_id")
    op.drop_index("ix_posting_versions_posting_id", table_name="posting_versions")
    op.drop_table("posting_versions")
    op.drop_index("ix_canonical_jobs_company_title", table_name="canonical_jobs")
    op.drop_table("canonical_jobs")
