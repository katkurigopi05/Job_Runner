"""crawl at scale: company slug, posting last_seen_at, indexes

Revision ID: c33a361f9611
Revises: afb1de709ce0
Create Date: 2026-09-12 10:52:28.372141

The unique constraint is the load-bearing part: it is what lets `_store` use
`INSERT ... ON CONFLICT` instead of reading every posting a company has and
branching in Python. Adding it to a table that already holds duplicates fails,
so this deduplicates first — and the way it does that is the part worth
reading.

**Losing rows are repointed, not simply deleted.** `matches`, `posting_labels`
and (through `SET NULL`) `applications` and `resumes` all reference a posting.
`posting_labels` cascades, so a plain `DELETE` of a duplicate would take the
owner's hand-written relevance grades with it — the scarce material CLAUDE.md
§15 says the matching benchmark is waiting on, destroyed silently by a schema
migration that reported success. So references move to the surviving row
first, and only rows that would collide with one the survivor already has are
dropped, because those are genuinely the same fact recorded twice.

The survivor is the oldest row by `first_seen_at`. It is the one whose id
downstream tables are most likely to already hold, and it carries the true
discovery date; a newer duplicate's content is copied onto it.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c33a361f9611"
down_revision: str | Sequence[str] | None = "afb1de709ce0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _deduplicate_postings() -> None:
    """Collapse rows sharing `(company_id, external_id)` onto one survivor."""
    bind = op.get_bind()

    bind.execute(
        sa.text("""
        CREATE TEMPORARY TABLE _posting_dupes ON COMMIT DROP AS
        WITH ranked AS (
            SELECT
                id,
                first_value(id) OVER (
                    PARTITION BY company_id, external_id
                    ORDER BY first_seen_at ASC, id ASC
                ) AS keep_id
            FROM postings
            WHERE company_id IS NOT NULL AND external_id IS NOT NULL
        )
        SELECT id AS loser_id, keep_id FROM ranked WHERE id <> keep_id
        """)
    )

    count = bind.execute(sa.text("SELECT count(*) FROM _posting_dupes")).scalar_one()
    if not count:
        return

    # The survivor keeps the newest content, so a duplicate that was the more
    # recently crawled of the pair does not lose its body on the way out.
    bind.execute(
        sa.text("""
        UPDATE postings AS keep
           SET title            = fresh.title,
               location         = fresh.location,
               description_raw  = fresh.description_raw,
               content_hash     = fresh.content_hash,
               url              = fresh.url,
               published_at     = COALESCE(fresh.published_at, keep.published_at),
               closed_at        = LEAST(keep.closed_at, fresh.closed_at)
          FROM (
              SELECT DISTINCT ON (d.keep_id) d.keep_id, p.*
                FROM _posting_dupes d
                JOIN postings p ON p.id = d.loser_id
               ORDER BY d.keep_id, p.first_seen_at DESC, p.id DESC
          ) AS fresh
         WHERE keep.id = fresh.keep_id
           AND fresh.first_seen_at > keep.first_seen_at
        """)
    )

    # No uniqueness on these two, so every reference can simply move.
    for table, column in (("resumes", "tailored_for_posting_id"), ("applications", "posting_id")):
        bind.execute(
            sa.text(f"""
            UPDATE {table} AS t
               SET {column} = d.keep_id
              FROM _posting_dupes d
             WHERE t.{column} = d.loser_id
            """)  # noqa: S608 - table and column are literals from the tuple above
        )

    # These two are unique per `(profile_id, posting_id)`. A loser row whose
    # profile already grades the survivor is the same judgement twice, so it
    # is dropped; anything else moves across.
    for table in ("matches", "posting_labels"):
        bind.execute(
            sa.text(f"""
            DELETE FROM {table} AS t
             USING _posting_dupes d
             WHERE t.posting_id = d.loser_id
               AND EXISTS (
                   SELECT 1 FROM {table} k
                    WHERE k.posting_id = d.keep_id
                      AND k.profile_id = t.profile_id
               )
            """)  # noqa: S608 - table name is a literal from the tuple above
        )
        bind.execute(
            sa.text(f"""
            UPDATE {table} AS t
               SET posting_id = d.keep_id
              FROM _posting_dupes d
             WHERE t.posting_id = d.loser_id
            """)  # noqa: S608 - table name is a literal from the tuple above
        )

    bind.execute(
        sa.text("DELETE FROM postings AS p USING _posting_dupes d WHERE p.id = d.loser_id")
    )
    print(f"  collapsed {count} duplicate posting(s) onto their surviving row")


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("companies", sa.Column("slug", sa.String(length=200), nullable=True))
    op.create_index("ix_companies_ats_slug", "companies", ["ats_type", "slug"], unique=False)
    op.create_index("ix_companies_last_polled_at", "companies", ["last_polled_at"], unique=False)

    # Recovered from the board URL where it is unambiguous. `upsert_company`
    # writes the slug from the seed on every cycle, so anything left NULL here
    # heals on the next crawl — this only spares a freshly enqueued task from
    # having to wait for that. A URL we do not recognise stays NULL rather
    # than being guessed at: a wrong slug points the crawler at another
    # employer's board, which is worse than no slug at all.
    #
    # Each vendor is matched twice, with and without its API prefix, because
    # the column holds both shapes — `job-boards.greenhouse.io/acme` from
    # discovery, `boards-api.greenhouse.io/v1/boards/acme/jobs` from a seed.
    # The prefixed form is tried first, or the short pattern would read `v1`
    # off the API URL and call it the slug.
    #
    # Written as eight flat patterns rather than four with `(?:...)?`, and not
    # for style: SQLAlchemy reads `:v1` inside `(?:v1/boards/)` as a bind
    # parameter and rewrites the regex to `(?$2/boards/)` before Postgres ever
    # sees it. The migration failed loudly, which is the good case — a
    # silently mangled pattern here would have written wrong slugs, and a
    # wrong slug points the crawler at another employer's board.
    board_slug_patterns = (
        r"greenhouse\.io/v1/boards/([A-Za-z0-9._-]+)",
        r"greenhouse\.io/([A-Za-z0-9._-]+)",
        r"lever\.co/v0/postings/([A-Za-z0-9._-]+)",
        r"lever\.co/([A-Za-z0-9._-]+)",
        r"ashbyhq\.com/posting-api/job-board/([A-Za-z0-9._-]+)",
        r"ashbyhq\.com/([A-Za-z0-9._-]+)",
        r"workable\.com/api/v1/widget/accounts/([A-Za-z0-9._-]+)",
        r"workable\.com/([A-Za-z0-9._-]+)",
    )
    extracted = ",\n                ".join(
        f"substring(careers_url from '{pattern}')" for pattern in board_slug_patterns
    )
    op.execute(f"""
        UPDATE companies SET slug = m.slug FROM (
            SELECT id, COALESCE(
                {extracted}
            ) AS slug
            FROM companies WHERE careers_url IS NOT NULL
        ) AS m
        WHERE companies.id = m.id AND m.slug IS NOT NULL AND companies.slug IS NULL
    """)

    op.add_column("postings", sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True))
    # `first_seen_at` is a moment we demonstrably had this posting in hand, so
    # it is a true lower bound for "last seen" rather than a guess. Leaving
    # these NULL would make every posting predating this migration look like
    # one we have never confirmed.
    op.execute("UPDATE postings SET last_seen_at = first_seen_at WHERE last_seen_at IS NULL")

    op.create_index("ix_postings_company_id", "postings", ["company_id"], unique=False)
    _deduplicate_postings()
    op.create_unique_constraint(
        "uq_postings_company_external_id", "postings", ["company_id", "external_id"]
    )


def downgrade() -> None:
    """Downgrade schema.

    The deduplication is not undone. Collapsed rows are gone and the grades
    that pointed at them now point at the survivor, which is the correct
    record either way — recreating duplicates to restore a shape nothing reads
    would be the destructive direction.
    """
    op.drop_constraint("uq_postings_company_external_id", "postings", type_="unique")
    op.drop_index("ix_postings_company_id", table_name="postings")
    op.drop_column("postings", "last_seen_at")
    op.drop_index("ix_companies_last_polled_at", table_name="companies")
    op.drop_index("ix_companies_ats_slug", table_name="companies")
    op.drop_column("companies", "slug")
