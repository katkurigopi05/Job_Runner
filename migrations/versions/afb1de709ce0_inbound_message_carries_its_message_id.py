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
    """Add the column and the index the duplicate check reads."""
    op.add_column(
        "inbound_messages",
        sa.Column("message_id", sa.String(length=998), nullable=True),
    )
    # Unique, and partial so the NULLs on pre-existing rows do not collide
    # with each other. It is what `route_message` conflicts against: a SELECT
    # then an INSERT is two statements and two concurrent `handle_inbox` tasks
    # can both pass the check — `search(None, "UNSEEN")` returns the same ids
    # to both until one of them FETCHes and marks them seen. The constraint
    # makes "one row per (candidate, message)" true by construction rather
    # than by timing.
    op.create_index(
        "uq_inbound_messages_candidate_message",
        "inbound_messages",
        ["candidate_id", "message_id"],
        unique=True,
        postgresql_where=sa.text("message_id IS NOT NULL"),
    )


def downgrade() -> None:
    """Drop both. The de-duplication falls back to the old triple."""
    op.drop_index("uq_inbound_messages_candidate_message", table_name="inbound_messages")
    op.drop_column("inbound_messages", "message_id")
