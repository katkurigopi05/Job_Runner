"""Score what changed, rather than everything, every time.

`handle_crawl` ends a cycle by loading every open posting and scoring all of
them for every profile. At 29 companies that is a few hundred rows. At 3,500
it is the whole corpus — descriptions and a 384-dimension vector each — pulled
into memory on any cycle where a single board moved.

The set that actually needs work is small and, more usefully, it is derivable
in SQL rather than something the crawler has to hand over:

- a posting with no vector — new, or edited, since `_store` drops the vector
  along with the text it was built from;
- a posting stamped with a different embedder or corpus revision, which is
  already how `embed_postings` defines staleness;
- a posting with no `Match` row for this profile, which is what a new posting
  and a newly added profile have in common.

Deriving it beats being told it. A posting missed because a worker died, a
cycle was interrupted, or a profile was added between runs still appears in
the next pass — where a list handed over by the crawler would have dropped it
silently, and a feed missing a posting looks exactly like a feed with nothing
new in it.

**Corpus statistics come from the corpus, never from the batch.** This does
not move the score — similarity comes from the embedder — it moves the
*explanation*: `missing_terms` and the legitimacy assessment, both written
into `Match.reasons_json` and read by the owner when deciding whether to
apply.

The failure is quieter than "the weights are slightly off". A batch is
normally smaller than `MIN_DOCUMENTS`, so statistics derived from it are not
`usable` at all, and the gap report falls back to the hand-written stopword
list that IDF exists to replace. So the postings scored incrementally would be
explained by one standard and the rest of the feed by another, with nothing
saying which. docs/REFERENCE.md §3.6 is about exactly this shape: a measure
computed against its own sample.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import structlog
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models import Match, Posting, Profile
from packages.matching.embed import Embedder, LexicalEmbedder, get_embedder
from packages.matching.idf import CorpusView, refresh_corpus_stats
from packages.matching.score import embed_postings, score_and_store

log = structlog.get_logger(__name__)

#: Postings handled in one pass. Bounds the memory a single task needs; what
#: is left over is picked up by the next pass, because the set is derived
#: rather than consumed.
DEFAULT_BATCH = 2000


@dataclass
class MatchingReport:
    embedded: int = 0
    scored: int = 0
    #: Whether the corpus statistics were rebuilt, which invalidates every
    #: stored vector and makes this pass a full one.
    rebuilt: bool = False
    revision: int | None = None
    per_profile: dict[str, int] = field(default_factory=dict)

    def summary(self) -> str:
        scope = "full (corpus rebuilt)" if self.rebuilt else "incremental"
        return f"{scope}: {self.embedded} embedded, {self.scored} scored"


def _embedder_for(view: CorpusView) -> Embedder | None:
    return LexicalEmbedder(frequencies=view.frequencies) if view.frequencies.usable else None


async def postings_needing_embedding(
    session: AsyncSession, *, revision: int | None, limit: int, full: bool = False
) -> list[Posting]:
    """Open postings whose vector is missing or from another space.

    `full` takes every open posting, which is what a corpus rebuild requires:
    a new revision means every stored vector was weighted by statistics that
    no longer exist, and cosine across those two spaces does not fail — it
    returns a plausible number.
    """
    query = select(Posting).where(Posting.closed_at.is_(None))
    if not full:
        query = query.where(
            or_(
                Posting.description_embedding.is_(None),
                Posting.embedding_revision.is_distinct_from(revision),
            )
        )
    rows = await session.scalars(query.order_by(Posting.first_seen_at.desc()).limit(limit))
    return list(rows.all())


async def postings_needing_scoring(
    session: AsyncSession, profile: Profile, *, limit: int, full: bool = False
) -> list[Posting]:
    """Open postings this profile has no `Match` row for.

    An outer join rather than `NOT IN (SELECT ...)`: the subquery form builds
    the profile's entire match set to answer a question about absence, which
    is the shape of read this module exists to stop doing.
    """
    query = select(Posting).where(Posting.closed_at.is_(None))
    if not full:
        query = query.outerjoin(
            Match,
            (Match.posting_id == Posting.id) & (Match.profile_id == profile.id),
        ).where(Match.id.is_(None))
    rows = await session.scalars(query.order_by(Posting.first_seen_at.desc()).limit(limit))
    return list(rows.all())


async def run_matching_pass(
    session: AsyncSession, *, limit: int = DEFAULT_BATCH, full: bool = False
) -> MatchingReport:
    """Embed and score whatever is outstanding. Does not commit."""
    view = await refresh_corpus_stats(session)
    # A rebuild restamps the whole corpus, so the pass has to be a full one
    # whatever the caller asked for.
    full = full or view.rebuilt
    report = MatchingReport(rebuilt=view.rebuilt, revision=view.revision)

    pending = await postings_needing_embedding(
        session, revision=view.revision, limit=limit, full=full
    )
    if pending:
        report.embedded = await embed_postings(
            session, pending, embedder=_embedder_for(view), revision=view.revision
        )

    embedder = _embedder_for(view) or get_embedder()
    profiles = list((await session.scalars(select(Profile))).all())
    for profile in profiles:
        batch = await postings_needing_scoring(session, profile, limit=limit, full=full)
        if not batch:
            continue
        await score_and_store(
            session,
            profile,
            batch,
            embedder=embedder,
            # Never from `batch`. See the module docstring.
            frequencies=view.frequencies if view.frequencies.usable else None,
        )
        report.per_profile[profile.label] = len(batch)
        report.scored += len(batch)

    log.info("matching_pass", summary=report.summary(), revision=view.revision)
    return report
