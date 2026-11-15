"""résumé studio: internships and certifications

Two tables for what the owner states and confirms about themselves, read by
the Resume Studio (`packages/tailor/studio.py`). Additive and reversible: no
existing table changes.

Appended after the existing HNSW revision so databases already at that
revision receive these tables. IF NOT EXISTS preserves evidence on early
development databases that created them before HNSW was stamped.

Revision ID: b7d9f1a3c5e8
Revises: d7e9f1a3b5c7
Create Date: 2026-11-14 00:30:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b7d9f1a3c5e8"
down_revision: str | Sequence[str] | None = "d7e9f1a3b5c7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EMPTY_LIST = sa.text("'[]'::jsonb")


def _owned_by_candidate() -> list[sa.Column]:  # type: ignore[type-arg]
    return [
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            primary_key=True,
        ),
        sa.Column(
            "candidate_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("candidates.id", ondelete="CASCADE"),
            nullable=False,
        ),
    ]


def _confirmation() -> list[sa.Column]:  # type: ignore[type-arg]
    return [
        sa.Column("verified", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    ]


def upgrade() -> None:
    op.create_table(
        "internships",
        *_owned_by_candidate(),
        sa.Column("company", sa.String(length=200), nullable=False),
        sa.Column("role", sa.String(length=200), nullable=False),
        sa.Column("location", sa.String(length=200), nullable=True),
        sa.Column("started", sa.String(length=40), nullable=False),
        sa.Column("ended", sa.String(length=40), nullable=True),
        sa.Column("stack", postgresql.JSONB(), server_default=EMPTY_LIST, nullable=False),
        sa.Column("bullets", postgresql.JSONB(), server_default=EMPTY_LIST, nullable=False),
        *_confirmation(),
        if_not_exists=True,
    )
    op.create_index("ix_internships_candidate", "internships", ["candidate_id"], if_not_exists=True)

    op.create_table(
        "certifications",
        *_owned_by_candidate(),
        sa.Column("name", sa.String(length=300), nullable=False),
        sa.Column("issuer", sa.String(length=200), nullable=False),
        sa.Column("issued", sa.String(length=40), nullable=True),
        sa.Column("expires", sa.String(length=40), nullable=True),
        sa.Column("credential_id", sa.String(length=200), nullable=True),
        sa.Column("skills", postgresql.JSONB(), server_default=EMPTY_LIST, nullable=False),
        *_confirmation(),
        if_not_exists=True,
    )
    op.create_index(
        "ix_certifications_candidate", "certifications", ["candidate_id"], if_not_exists=True
    )


def downgrade() -> None:
    op.drop_index("ix_certifications_candidate", table_name="certifications")
    op.drop_table("certifications")
    op.drop_index("ix_internships_candidate", table_name="internships")
    op.drop_table("internships")
