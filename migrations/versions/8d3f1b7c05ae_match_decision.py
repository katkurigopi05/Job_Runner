"""The owner's verdict on a posting, beside the machine's.

Swiping right and left is not only a nicer feed. It is the hand-labeled set
CLAUDE.md §15 says does not exist: Gate 5 asks whether "the ones you'd actually
apply to rank in the top 10", and until now nothing recorded which ones those
were. Every decision is one label, produced by using the tool rather than by
sitting down to annotate.

That matters immediately. The first real scoring run produced a maximum score
of 0.271 against a `min_match_score` of 0.75 — a threshold the metric cannot
reach. Decisions are what let the threshold be derived from what the owner
actually wants instead of guessed.

Also adds the unique constraint that should always have been on this table.
`score_and_store` looks up existing rows in memory and updates them; nothing
stopped a second row for the same pair, and once decisions live here a
duplicate means a lost verdict.

Revision ID: 8d3f1b7c05ae
Revises: 7c1e4a2b9d30
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "8d3f1b7c05ae"
down_revision = "7c1e4a2b9d30"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("matches", sa.Column("decision", sa.String(20), nullable=True))
    op.add_column("matches", sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True))
    # Collapse any duplicates before the constraint refuses them. Keeps the
    # newest row per pair, which carries the most recent score.
    op.execute(
        """
        DELETE FROM matches a USING matches b
        WHERE a.profile_id = b.profile_id
          AND a.posting_id = b.posting_id
          AND a.created_at < b.created_at
        """
    )
    op.create_unique_constraint(
        "uq_matches_profile_posting", "matches", ["profile_id", "posting_id"]
    )
    op.create_index(
        "ix_matches_profile_undecided",
        "matches",
        ["profile_id"],
        postgresql_where=sa.text("decision IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_matches_profile_undecided", table_name="matches")
    op.drop_constraint("uq_matches_profile_posting", "matches", type_="unique")
    op.drop_column("matches", "decided_at")
    op.drop_column("matches", "decision")
