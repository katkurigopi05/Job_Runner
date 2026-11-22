"""Persist application interview rounds, prep revisions and judgments."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "71ad2ce490f6"
down_revision = "11f53d87f4a2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("applications", sa.Column("submission_sources", JSONB()))
    op.create_table(
        "interview_rounds",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "application_id",
            sa.Uuid(),
            sa.ForeignKey("applications.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "task_id",
            sa.Uuid(),
            sa.ForeignKey("application_tasks.id", ondelete="SET NULL"),
            unique=True,
        ),
        sa.Column("notes", sa.String(6000)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_interview_rounds_application_id", "interview_rounds", ["application_id"])
    op.create_table(
        "interview_plans",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "round_id",
            sa.Uuid(),
            sa.ForeignKey("interview_rounds.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("request_id", sa.Uuid(), unique=True, nullable=False),
        sa.Column("source_key", sa.String(64)),
        sa.Column("inputs", JSONB(), nullable=False),
        sa.Column("progress", JSONB(), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("queue_id", sa.Uuid(), sa.ForeignKey("queue_tasks.id", ondelete="SET NULL")),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_interview_plans_round_id", "interview_plans", ["round_id"])
    op.create_index("ix_interview_plans_source_key", "interview_plans", ["source_key"])
    op.create_table(
        "interview_judgments",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "plan_id",
            sa.Uuid(),
            sa.ForeignKey("interview_plans.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("requirement", sa.String(64), nullable=False),
        sa.Column("evidence", sa.String(64), nullable=False),
        sa.Column("grade", sa.Integer(), nullable=False),
        sa.UniqueConstraint("plan_id", "requirement", "evidence", name="uq_interview_judgment"),
    )
    op.create_index("ix_interview_judgments_plan_id", "interview_judgments", ["plan_id"])


def downgrade() -> None:
    op.drop_table("interview_judgments")
    op.drop_table("interview_plans")
    op.drop_table("interview_rounds")
    op.drop_column("applications", "submission_sources")
