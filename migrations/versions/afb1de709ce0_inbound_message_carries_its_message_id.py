"""inbound message carries its message id

`route_message` promised idempotency on `message_id` and de-duplicated on
(from_addr, application_id, subject) instead, because no such column existed.
That triple collides on the mail which legitimately repeats: an OTP resend
carries the same sender and subject with a new code, and was dropped before
reaching the block that hands the code to the state machine.

Nullable and deliberately not backfilled. Existing rows have no id to recover
— the header was never stored — and inventing one would make two different
messages look like the same message, which is the failure this column exists
to prevent. A NULL simply never matches, so old rows cannot suppress new mail.

Revision ID: afb1de709ce0
Revises: 9d6e40bcd915
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "afb1de709ce0"
down_revision = "9d6e40bcd915"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "inbound_messages",
        sa.Column("message_id", sa.String(length=998), nullable=True),
    )
    op.create_index(
        "ix_inbound_messages_candidate_message",
        "inbound_messages",
        ["candidate_id", "message_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_inbound_messages_candidate_message", table_name="inbound_messages")
    op.drop_column("inbound_messages", "message_id")
