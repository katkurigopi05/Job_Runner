"""rejoin the corpus-statistics and target-seniority heads

Revision ID: 6a75498178db
Revises: 9d43fb4965f3, b1c4e7a92f30
Create Date: 2026-09-01 01:25:19.103959

Both branches added migrations after `9e2a7c4d31bf`, so merging them left the
chain with two heads: this branch's `9d43fb4965f3` (corpus statistics, and
which embedder produced a vector) and main's `b1c4e7a92f30`, the last of four.
Alembic refuses `upgrade head` with more than one, which is where CI stopped —
before any test ran, so the failure said nothing about the code.

Empty on purpose. The two heads touch different tables and neither depends on
the other; there is nothing to reconcile, only a fork to close. A merge
revision that *did* something would be a schema change hiding inside a
bookkeeping commit.
"""

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "6a75498178db"
down_revision: str | Sequence[str] | None = ("9d43fb4965f3", "b1c4e7a92f30")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Nothing to do — see the module docstring."""


def downgrade() -> None:
    """Nothing to undo; the fork reopens on either side."""
