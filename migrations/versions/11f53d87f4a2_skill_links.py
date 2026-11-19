"""Save the skill graph: posting, project and base résumé links to skills.

Four new tables, empty when created. `make skill-links` fills them (about ten
minutes for 55,000 postings, most of it re-reading postings stored by an older
requirements extractor); from then on the crawler, GitHub sync and résumé
upload keep them current. Nothing existing is changed.

Revision ID: 11f53d87f4a2
Revises: c8e0f2a4b6d9
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "11f53d87f4a2"
down_revision: str | Sequence[str] | None = "c8e0f2a4b6d9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Skipped table by table when present, like the Studio revisions before it:
    # a database stamped back past this revision with its tables kept must
    # still reach head (`tests/test_migration_resume_studio.py`).
    present = set(sa.inspect(op.get_bind()).get_table_names())
    if "posting_skills" not in present:
        _posting_skills()
    if "posting_skill_reads" not in present:
        _posting_skill_reads()
    if "project_skills" not in present:
        _project_skills()
    if "resume_skills" not in present:
        _resume_skills()


def _posting_skills() -> None:
    op.create_table(
        "posting_skills",
        sa.Column("posting_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("skill", sa.String(length=64), nullable=False),
        sa.Column("strength", sa.String(length=16), nullable=False),
        sa.Column("is_option", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("quote", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.CheckConstraint(
            "strength IN ('required', 'preferred', 'named')", name="ck_posting_skills_strength"
        ),
        sa.ForeignKeyConstraint(["posting_id"], ["postings.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("posting_id", "skill"),
    )
    op.create_index(
        "ix_posting_skills_skill_strength", "posting_skills", ["skill", "strength"], unique=False
    )


def _posting_skill_reads() -> None:
    op.create_table(
        "posting_skill_reads",
        sa.Column("posting_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("extractor_version", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=True),
        sa.Column("readable", sa.Boolean(), nullable=False),
        sa.Column(
            "linked_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(["posting_id"], ["postings.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("posting_id"),
    )


def _project_skills() -> None:
    op.create_table(
        "project_skills",
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("skill", sa.String(length=64), nullable=False),
        sa.Column("origin", sa.String(length=16), nullable=False),
        sa.Column("declared_as", sa.String(length=200), nullable=False),
        sa.CheckConstraint("origin IN ('language', 'dependency')", name="ck_project_skills_origin"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("project_id", "skill", "origin"),
    )
    op.create_index("ix_project_skills_skill", "project_skills", ["skill"], unique=False)


def _resume_skills() -> None:
    op.create_table(
        "resume_skills",
        sa.Column("resume_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("skill", sa.String(length=64), nullable=False),
        sa.ForeignKeyConstraint(["resume_id"], ["resumes.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("resume_id", "skill"),
    )
    op.create_index("ix_resume_skills_skill", "resume_skills", ["skill"], unique=False)


def downgrade() -> None:
    # Derived data only: every row can be rebuilt with `make skill-links`.
    op.drop_index("ix_resume_skills_skill", table_name="resume_skills")
    op.drop_table("resume_skills")
    op.drop_index("ix_project_skills_skill", table_name="project_skills")
    op.drop_table("project_skills")
    op.drop_table("posting_skill_reads")
    op.drop_index("ix_posting_skills_skill_strength", table_name="posting_skills")
    op.drop_table("posting_skills")
