"""initial schema

Revision ID: 01a136f0838f
Revises:
Create Date: 2026-08-15 01:23:48.248035

"""

from collections.abc import Sequence

import pgvector.sqlalchemy
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "01a136f0838f"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # Required before any vector column can be created.
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "companies",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("name", sa.String(length=300), nullable=False),
        sa.Column("domain", sa.String(length=300), nullable=True),
        sa.Column("careers_url", sa.Text(), nullable=True),
        sa.Column("ats_type", sa.String(length=50), nullable=True),
        sa.Column("poll_interval_s", sa.Integer(), server_default=sa.text("3600"), nullable=False),
        sa.Column("last_polled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "queue_tasks",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("kind", sa.String(length=50), nullable=False),
        sa.Column(
            "payload_json",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "status", sa.String(length=20), server_default=sa.text("'pending'"), nullable=False
        ),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "run_after", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("locked_by", sa.String(length=200), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_queue_tasks_status_run_after", "queue_tasks", ["status", "run_after"], unique=False
    )
    op.create_table(
        "users",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("email"),
    )
    op.create_table(
        "candidates",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("email_mode", sa.String(length=16), nullable=False),
        sa.Column("managed_alias", sa.String(length=320), nullable=True),
        sa.Column("secrets_ref", sa.String(length=200), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("email_mode IN ('managed', 'self')", name="ck_candidates_email_mode"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "postings",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("company_id", sa.UUID(), nullable=True),
        sa.Column("ats_type", sa.String(length=50), nullable=True),
        sa.Column("external_id", sa.String(length=200), nullable=True),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=True),
        sa.Column("location", sa.String(length=300), nullable=True),
        sa.Column("description_raw", sa.Text(), nullable=True),
        sa.Column(
            "description_embedding", pgvector.sqlalchemy.vector.VECTOR(dim=384), nullable=True
        ),
        sa.Column("content_hash", sa.String(length=64), nullable=True),
        sa.Column(
            "first_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_postings_content_hash", "postings", ["content_hash"], unique=False)
    op.create_index(
        "ix_postings_first_seen_at",
        "postings",
        [sa.literal_column("first_seen_at DESC")],
        unique=False,
    )
    op.create_table(
        "resumes",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("candidate_id", sa.UUID(), nullable=False),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("storage_ref", sa.String(length=500), nullable=False),
        sa.Column("parsed_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("is_default", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["candidate_id"], ["candidates.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "profiles",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("candidate_id", sa.UUID(), nullable=False),
        sa.Column("label", sa.String(length=200), nullable=False),
        sa.Column("base_resume_id", sa.UUID(), nullable=True),
        sa.Column("phone", sa.String(length=50), nullable=True),
        sa.Column("location", sa.String(length=200), nullable=True),
        sa.Column("work_auth", sa.String(length=200), nullable=True),
        sa.Column("needs_sponsorship", sa.Boolean(), nullable=True),
        sa.Column(
            "links_json",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("salary_expectation", sa.String(length=100), nullable=True),
        sa.Column(
            "answers_kv_json",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("min_match_score", sa.Float(), server_default=sa.text("0.75"), nullable=False),
        sa.Column("auto_submit", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["base_resume_id"], ["resumes.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["candidate_id"], ["candidates.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "applications",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("candidate_id", sa.UUID(), nullable=False),
        sa.Column("profile_id", sa.UUID(), nullable=False),
        sa.Column("posting_id", sa.UUID(), nullable=True),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("ats", sa.String(length=50), nullable=True),
        sa.Column(
            "status", sa.String(length=30), server_default=sa.text("'queued'"), nullable=False
        ),
        sa.Column("failure_reason", sa.String(length=50), nullable=True),
        sa.Column("review_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("tailored_resume_id", sa.UUID(), nullable=True),
        sa.Column("cover_letter_ref", sa.String(length=500), nullable=True),
        sa.Column("receipt_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["candidate_id"], ["candidates.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["posting_id"], ["postings.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["profile_id"], ["profiles.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tailored_resume_id"], ["resumes.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("candidate_id", "url", name="uq_applications_candidate_url"),
    )
    op.create_index("ix_applications_status", "applications", ["status"], unique=False)
    op.create_table(
        "matches",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("profile_id", sa.UUID(), nullable=False),
        sa.Column("posting_id", sa.UUID(), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("reasons_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["posting_id"], ["postings.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["profile_id"], ["profiles.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_matches_profile_score",
        "matches",
        ["profile_id", sa.literal_column("score DESC")],
        unique=False,
    )
    op.create_table(
        "application_events",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("application_id", sa.UUID(), nullable=False),
        sa.Column("type", sa.String(length=50), nullable=False),
        sa.Column("payload_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(["application_id"], ["applications.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_application_events_application_id",
        "application_events",
        ["application_id"],
        unique=False,
    )
    op.create_table(
        "inbound_messages",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("candidate_id", sa.UUID(), nullable=False),
        sa.Column("application_id", sa.UUID(), nullable=True),
        sa.Column("from_addr", sa.String(length=320), nullable=False),
        sa.Column("subject", sa.Text(), nullable=True),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("classification", sa.String(length=50), nullable=True),
        sa.Column(
            "at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(["application_id"], ["applications.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["candidate_id"], ["candidates.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    # The vector extension is left installed — dropping it would break any
    # other database object that depends on it.
    op.drop_table("inbound_messages")
    op.drop_index("ix_application_events_application_id", table_name="application_events")
    op.drop_table("application_events")
    op.drop_index("ix_matches_profile_score", table_name="matches")
    op.drop_table("matches")
    op.drop_index("ix_applications_status", table_name="applications")
    op.drop_table("applications")
    op.drop_table("profiles")
    op.drop_table("resumes")
    op.drop_index("ix_postings_first_seen_at", table_name="postings")
    op.drop_index("ix_postings_content_hash", table_name="postings")
    op.drop_table("postings")
    op.drop_table("candidates")
    op.drop_table("users")
    op.drop_index("ix_queue_tasks_status_run_after", table_name="queue_tasks")
    op.drop_table("queue_tasks")
    op.drop_table("companies")
