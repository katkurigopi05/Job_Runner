"""Add structured posting locations without changing existing source text.

Revision ID: f2b4d6e8a0c1
Revises: b5d7f9a1c3e6
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "f2b4d6e8a0c1"
down_revision = "b5d7f9a1c3e6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for column in (
        sa.Column("country_code", sa.String(2), nullable=True),
        sa.Column("state_code", sa.String(2), nullable=True),
        sa.Column("is_remote", sa.Boolean(), nullable=True),
        sa.Column("locations_json", postgresql.JSONB(), nullable=True),
        sa.Column("location_version", sa.Integer(), nullable=True),
        sa.Column("listed_location", sa.Text(), nullable=True),
    ):
        op.add_column("postings", column)
    op.create_index("ix_postings_location", "postings", ["country_code", "state_code", "is_remote"])


def downgrade() -> None:
    op.drop_index("ix_postings_location", table_name="postings")
    for column in (
        "listed_location",
        "location_version",
        "locations_json",
        "is_remote",
        "state_code",
        "country_code",
    ):
        op.drop_column("postings", column)
