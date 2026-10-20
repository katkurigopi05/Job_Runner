"""Persist Workday's bounded detail-validation and pending cursors.

Revision ID: a6c8e0f2b4d7
Revises: f2c8d4e6a9b1
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a6c8e0f2b4d7"
down_revision: str | None = "f2c8d4e6a9b1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("company_crawl_states", sa.Column("workday_detail_cursor", sa.String(200)))
    op.add_column("company_crawl_states", sa.Column("workday_pending_cursor", sa.String(200)))


def downgrade() -> None:
    # Only scheduling cursors are lost; postings and their history survive.
    op.drop_column("company_crawl_states", "workday_pending_cursor")
    op.drop_column("company_crawl_states", "workday_detail_cursor")
