"""profile bound on how many years a posting may demand

`Posting` records no experience requirement, so a role asking for ten years was
ranked against a new graduate's résumé by cosine similarity alone — and a
cosine cannot refuse a seniority demand. `matching/experience.py` reads the
demand out of the posting text; this column is the owner's limit on it.

**The owner's bound, not a count of their experience.** CLAUDE.md §1 keeps a
search filter separate from the profile's description of the applicant, and
here they are genuinely different numbers: somebody with two years may still
want to see the five-year roles, and somebody with ten may not want the
twelve-year ones.

**Deliberately not backfilled.** NULL means "do not filter on experience",
which is the shipped behaviour and what every existing row gets. The data that
could seed a guess is the résumé, and inferring a bound from it would narrow
the feed on a number nobody stated — the same reasoning
`profiles.target_seniority` records.

Only a demand the posting states as *mandatory* ever excludes. A preferred one
lowers a rubric dimension instead, so the distinction is visible rather than
silent.

Existing profile data is untouched: this only adds a nullable column.

Revision ID: 7a1c94b8e3d2
Revises: 61d3f1f0f701
Create Date: 2026-09-13 07:40:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "7a1c94b8e3d2"
down_revision: str | Sequence[str] | None = "61d3f1f0f701"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the column. No backfill — see the module docstring."""
    op.add_column(
        "profiles",
        sa.Column("max_required_experience_years", sa.Integer(), nullable=True),
    )
    # A bound outside this range is a typo rather than a preference, and an
    # unchecked one is the failure `target_seniority` records: a value the
    # filter cannot use reads as "no preference", so the feed looks unfiltered
    # with nothing to explain it. 0 is meaningful — "only roles that ask for no
    # experience" is a real search for a new graduate.
    op.create_check_constraint(
        "ck_profiles_max_required_experience_years",
        "profiles",
        "max_required_experience_years IS NULL OR max_required_experience_years BETWEEN 0 AND 50",
    )


def downgrade() -> None:
    op.drop_constraint("ck_profiles_max_required_experience_years", "profiles", type_="check")
    op.drop_column("profiles", "max_required_experience_years")
