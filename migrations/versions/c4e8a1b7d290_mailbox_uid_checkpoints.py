"""Mailbox UID checkpoints.

Revision ID: c4e8a1b7d290
Revises: b8f4c2d6e901
Create Date: 2026-10-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c4e8a1b7d290"
down_revision: str | None = "b8f4c2d6e901"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "mailbox_checkpoints",
        sa.Column("source_key", sa.String(length=64), nullable=False),
        sa.Column("uid_validity", sa.BigInteger(), nullable=False),
        sa.Column("last_uid", sa.BigInteger(), nullable=False),
        sa.Column("initialized", sa.Boolean(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("uid_validity >= 0", name="ck_mailbox_checkpoints_uid_validity"),
        sa.CheckConstraint("last_uid >= 0", name="ck_mailbox_checkpoints_last_uid"),
        sa.PrimaryKeyConstraint("source_key"),
    )


def downgrade() -> None:
    op.drop_table("mailbox_checkpoints")
