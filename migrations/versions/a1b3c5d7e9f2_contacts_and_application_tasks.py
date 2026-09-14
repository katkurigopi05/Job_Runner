"""contacts and application tasks

People the owner deals with about an application, and the interviews,
assessments and follow-ups attached to it — with deadlines, checklists and a
reminder time. New tables only; no existing row changes.

Revision ID: a1b3c5d7e9f2
Revises: f0a2b4c6d8e1
Create Date: 2026-09-14 17:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a1b3c5d7e9f2"
down_revision: str | Sequence[str] | None = "f0a2b4c6d8e1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _id() -> sa.Column:
    return sa.Column(
        "id",
        postgresql.UUID(as_uuid=True),
        server_default=sa.text("gen_random_uuid()"),
        nullable=False,
    )


def _stamp(name: str) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)


def upgrade() -> None:
    op.create_table(
        "contacts",
        _id(),
        sa.Column("candidate_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=True),
        sa.Column("phone", sa.String(length=50), nullable=True),
        sa.Column("company", sa.String(length=200), nullable=True),
        sa.Column("role", sa.String(length=200), nullable=True),
        sa.Column("profile_url", sa.Text(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        _stamp("created_at"),
        _stamp("updated_at"),
        sa.ForeignKeyConstraint(["candidate_id"], ["candidates.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_contacts_candidate_id", "contacts", ["candidate_id"])
    op.create_table(
        "application_contacts",
        _id(),
        sa.Column("application_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("contact_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "relationship",
            sa.String(length=30),
            server_default=sa.text("'recruiter'"),
            nullable=False,
        ),
        _stamp("created_at"),
        sa.CheckConstraint(
            "relationship IN ('recruiter', 'hiring_manager', 'interviewer', 'referrer', 'other')",
            name="ck_application_contacts_relationship",
        ),
        sa.ForeignKeyConstraint(["application_id"], ["applications.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["contact_id"], ["contacts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("application_id", "contact_id", name="uq_application_contacts_pair"),
    )
    op.create_table(
        "application_tasks",
        _id(),
        sa.Column("application_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reminder_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reminded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("location", sa.Text(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "checklist_json",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "source", sa.String(length=20), server_default=sa.text("'owner'"), nullable=False
        ),
        _stamp("created_at"),
        _stamp("updated_at"),
        sa.CheckConstraint(
            "kind IN ('interview', 'assessment', 'follow_up', 'prep', 'other')",
            name="ck_application_tasks_kind",
        ),
        sa.CheckConstraint("source IN ('owner', 'inbox')", name="ck_application_tasks_source"),
        sa.CheckConstraint(
            "reminder_at IS NULL OR due_at IS NULL OR reminder_at <= due_at",
            name="ck_application_tasks_reminder_before_due",
        ),
        sa.ForeignKeyConstraint(["application_id"], ["applications.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_application_tasks_application_id", "application_tasks", ["application_id"])
    op.create_index("ix_application_tasks_due_at", "application_tasks", ["due_at"])
    op.create_index("ix_application_tasks_reminder_at", "application_tasks", ["reminder_at"])


def downgrade() -> None:
    op.drop_table("application_tasks")
    op.drop_table("application_contacts")
    op.drop_index("ix_contacts_candidate_id", table_name="contacts")
    op.drop_table("contacts")
