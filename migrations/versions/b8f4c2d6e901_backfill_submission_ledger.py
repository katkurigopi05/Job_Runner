"""Protect historical submissions and interrupted pre-ledger attempts.

Stop/drain all workers before upgrading; older workers do not honor fences.
No application or event is deleted or rewritten. Alias collisions keep the
earliest confirmed application, while the complete history remains intact.
"""

import sqlalchemy as sa
from alembic import op

from packages.core.submission_identity import submission_key_v1

revision = "b8f4c2d6e901"
down_revision = "14ebc80a6ecb"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    # A rolled-back pre-ledger submit may leave QUEUED/FAILED rather than
    # RUNNING. A previously claimed apply task is evidence of possible work,
    # not proof of delivery: preserve it as uncertain, never as submitted.
    rows = connection.execute(
        sa.text("""
            SELECT a.id, a.candidate_id, a.url, a.status, a.created_at, a.updated_at,
                   COALESCE(a.posting_id, (
                       SELECT p.id FROM postings p WHERE p.url = a.url LIMIT 1
                   )) AS posting_id
            FROM applications a
            WHERE a.status IN ('submitted', 'running') OR EXISTS (
                SELECT 1 FROM queue_tasks q
                WHERE q.kind = 'apply' AND q.attempts > 0
                  AND q.payload_json->>'application_id' = a.id::text
            )
            ORDER BY (a.status = 'submitted') DESC, a.created_at, a.id
        """)
    ).mappings()
    for app in rows:
        confirmed = app["status"] == "submitted"
        connection.execute(
            sa.text("""
                INSERT INTO applied_postings
                    (application_id, candidate_id, posting_id, job_key, url,
                     state, source, attempted_at, confirmed_at)
                VALUES (:id, :candidate_id, :posting_id, :job_key, :url,
                        :state, 'legacy', :attempted_at, :confirmed_at)
                ON CONFLICT DO NOTHING
            """),
            {
                "id": app["id"],
                "candidate_id": app["candidate_id"],
                "posting_id": app["posting_id"],
                "job_key": submission_key_v1(app["url"]),
                "url": app["url"],
                "state": "submitted" if confirmed else "uncertain",
                # Legacy data has no precise click timestamp. updated_at is
                # historical evidence only, not a claimed exact submit time.
                "attempted_at": app["updated_at"],
                "confirmed_at": app["updated_at"] if confirmed else None,
            },
        )


def downgrade() -> None:
    # The previous revision understands these rows. Keep them: removing
    # submission evidence during a code rollback could send a form twice.
    pass
