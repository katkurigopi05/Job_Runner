"""api_postings view

`postings` without its embedding, for Data API Builder (`make dab`). The tool
reads every column of an object it serves and cannot read pgvector's type:
pointed at `postings` it stopped at start-up with "Reading as 'System.Object'
is not supported for fields having DataTypeName 'public.vector'". A view is
the only way to hand it the table without that column.

The columns are named, so one added to `postings` later is not served until
somebody adds it here. Left out with the vector: the bookkeeping that
describes it (`embedding_model`, `embedding_revision`) and the extraction and
merge bookkeeping, which nothing outside the pipeline reads.

One thing this costs. Postgres will not change the type of a column a view
uses, or drop it, so a later migration that does either to one of these
columns has to drop the view first and create it again after.

A view only: no row and no table changes.

Revision ID: d4e6f8a0b1c3
Revises: c3d5e7f9a1b2
Create Date: 2026-10-06 15:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

revision: str = "d4e6f8a0b1c3"
down_revision: str | Sequence[str] | None = "c3d5e7f9a1b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE VIEW api_postings AS
        SELECT
            id,
            company_id,
            ats_type,
            external_id,
            url,
            title,
            location,
            description_raw,
            content_hash,
            published_at,
            first_seen_at,
            last_seen_at,
            closed_at,
            salary_min,
            salary_max,
            salary_currency,
            salary_period,
            requirements_json,
            canonical_job_id
        FROM postings
        """
    )


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS api_postings")
