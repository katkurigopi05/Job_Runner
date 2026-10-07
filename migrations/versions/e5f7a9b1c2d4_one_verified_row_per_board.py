"""one verified row per board

A board is an ATS and a slug. Eleven were each verified under two company
rows, because the owner's sheet lists a company under an old name and a new
one and discovery resolved both to the same board. Each was fetched twice a
cycle and every posting on it stored twice: 282 of 11,429 open postings.

A partial unique index, on verified rows only. Hint, unverified and failed
rows may still name a board any number of times, which is how a collision
stays something to look at. What is refused is two rows being polled for it.

It will not build over a database that already holds both, and it does not
choose between them: which row keeps a board, and what happens to the second
copy of each posting, is decided and tested in
`packages/crawler/duplicate_boards.py`. So this stops and says what to run.

Revision ID: e5f7a9b1c2d4
Revises: d4e6f8a0b1c3
Create Date: 2026-10-06 21:30:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e5f7a9b1c2d4"
down_revision: str | Sequence[str] | None = "d4e6f8a0b1c3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    held_twice = op.get_bind().scalar(
        sa.text(
            """
            SELECT count(*) FROM (
                SELECT 1 FROM companies
                WHERE source_status = 'verified' AND slug IS NOT NULL
                GROUP BY ats_type, slug HAVING count(*) > 1
            ) AS boards
            """
        )
    )
    if held_twice:
        raise RuntimeError(
            f"{held_twice} boards are verified under more than one company row. "
            "Run `make backup`, then `make merge-duplicate-boards apply=1`, then migrate again."
        )
    op.create_index(
        "uq_companies_verified_board",
        "companies",
        ["ats_type", "slug"],
        unique=True,
        postgresql_where=sa.text("source_status = 'verified'"),
    )


def downgrade() -> None:
    op.drop_index("uq_companies_verified_board", table_name="companies")
