"""Chunk search spaces: bge-small, or Qwen with bge-small for uncovered postings.

Distances never cross model boundaries. Both use the same ranking query and
current-text predicate; the caller merges their ranks. Plain string keys retain
the original search exactly when CHAT_QWEN_SEARCH is off.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

import structlog
from sqlalchemy import ColumnElement, and_, exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from packages.core.models import Posting
from packages.core.models_chunks import PostingChunk, PostingChunkVector
from packages.matching import query_vectors
from packages.matching.chunk_vectors import QWEN_4B as QWEN_4B
from packages.matching.embed import SentenceTransformerEmbedder

log = structlog.get_logger(__name__)


class ExactRanking:
    """One exact Qwen scan, owned by one question, reused by all its stages.

    DISTINCT ON selects the best span per posting in SQL. No embedding leaves
    Postgres; only the compact ranking is kept, never a cross-question cache.
    """

    def __init__(self, vector: list[float]) -> None:
        self.vector = vector
        self.rows: list[tuple[uuid.UUID, int, int, float, uuid.UUID | None]] | None = None

    async def read(
        self,
        session: AsyncSession,
        *,
        among: list[uuid.UUID] | None,
        companies: list[uuid.UUID] | None,
    ) -> list[tuple[uuid.UUID, int, int]]:
        if self.rows is None:
            # Current-hash equality is correlated with the posting join. The
            # planner estimates almost no matches and otherwise chooses a
            # random vector-table lookup per chunk. Materialize distances first
            # so the large vectors are read once in physical scan order.
            distances = (
                select(
                    PostingChunkVector.chunk_id,
                    PostingChunkVector.embedding.cosine_distance(self.vector).label("distance"),
                )
                .where(PostingChunkVector.embedding_model == QWEN_4B)
                .cte("qwen_distances")
                .prefix_with("MATERIALIZED")
            )
            query = (
                select(
                    PostingChunk.posting_id,
                    PostingChunk.start,
                    PostingChunk.length,
                    distances.c.distance,
                    Posting.company_id,
                )
                .select_from(distances)
                .join(PostingChunk, PostingChunk.id == distances.c.chunk_id)
                .join(Posting, Posting.id == PostingChunk.posting_id)
                .where(*_current_chunks())
                .distinct(PostingChunk.posting_id)
                .order_by(PostingChunk.posting_id, distances.c.distance, PostingChunk.ordinal)
            )
            self.rows = [
                (pid, start, length, dist, company)
                for pid, start, length, dist, company in (await session.execute(query)).all()
            ]
            self.rows.sort(key=lambda row: (row[3], str(row[0])))
        selected = set(among) if among is not None else None
        employers = set(companies) if companies else None
        return [
            (pid, start, length)
            for pid, start, length, dist, company in self.rows
            if (pid in selected if selected is not None else dist < 1.0)
            and (employers is None or company in employers)
        ]


@dataclass(frozen=True)
class ChunkSpace:
    model: str
    without_qwen: bool = False
    ranking: ExactRanking | None = field(default=None, compare=False, repr=False)


type Space = str | ChunkSpace


def has_qwen() -> ColumnElement[bool]:
    """An open posting's current chunks have at least one Qwen vector.

    Aliases keep this correlated only to Posting, even inside a chunk query.
    """
    chunk, vector = aliased(PostingChunk), aliased(PostingChunkVector)
    return exists(
        select(1)
        .select_from(chunk)
        .join(vector, vector.chunk_id == chunk.id)
        .where(
            chunk.posting_id == Posting.id,
            chunk.content_hash.is_not_distinct_from(Posting.content_hash),
            vector.embedding_model == QWEN_4B,
        )
        .correlate(Posting)
    )


def is_qwen(space: Space) -> bool:
    return isinstance(space, ChunkSpace) and space.model == QWEN_4B


def _current_chunks() -> list[ColumnElement[bool]]:
    return [
        Posting.closed_at.is_(None),
        PostingChunk.content_hash.is_not_distinct_from(Posting.content_hash),
    ]


def chunk_filter(space: Space) -> list[ColumnElement[bool]]:
    model = space.model if isinstance(space, ChunkSpace) else space
    stamp = PostingChunkVector.embedding_model if is_qwen(space) else PostingChunk.embedding_model
    filters = [*_current_chunks(), stamp == model]
    if isinstance(space, ChunkSpace) and space.without_qwen:
        filters.append(~has_qwen())
    return filters


def reachable(space: Space) -> ColumnElement[bool]:
    query = select(1).select_from(PostingChunk)
    if is_qwen(space):
        query = query.join(PostingChunkVector, PostingChunkVector.chunk_id == PostingChunk.id)
    return exists(
        query.where(PostingChunk.posting_id == Posting.id, *chunk_filter(space)).correlate(Posting)
    )


async def chunk_ranking(
    session: AsyncSession,
    model: Space,
    vector: list[float],
    *,
    among: list[uuid.UUID] | None,
    companies: list[uuid.UUID] | None,
    limit: int,
) -> tuple[list[tuple[uuid.UUID, int, int]], bool]:
    """Postings by their best current chunk; whether the read exhausted its pool."""
    if isinstance(model, ChunkSpace) and is_qwen(model) and model.ranking is not None:
        # Already exhaustive: widening changes only how many postings are
        # hydrated, never re-reads the vector table or truncates a posting's spans.
        return await model.ranking.read(session, among=among, companies=companies), True
    column = PostingChunkVector.embedding if is_qwen(model) else PostingChunk.embedding
    distance = column.cosine_distance(vector)
    query = select(PostingChunk.posting_id, PostingChunk.start, PostingChunk.length, distance).join(
        Posting, Posting.id == PostingChunk.posting_id
    )
    if is_qwen(model):
        query = query.join(PostingChunkVector, PostingChunkVector.chunk_id == PostingChunk.id)
    query = query.where(*chunk_filter(model))
    if among is not None:
        query = query.where(PostingChunk.posting_id.in_(among))
    else:
        query = (
            query.where(distance < 1.0)
            .order_by(distance, PostingChunk.posting_id, PostingChunk.ordinal)
            .limit(limit)
        )
    if companies:
        query = query.where(Posting.company_id.in_(companies))
    rows = (await session.execute(query)).all()
    best: dict[uuid.UUID, tuple[float, int, int]] = {}
    for posting_id, start, length, dist in rows:
        if posting_id not in best or dist < best[posting_id][0]:
            best[posting_id] = (dist, start, length)
    ranked = sorted(best.items(), key=lambda item: (item[1][0], str(item[0])))
    every = among is not None or len(rows) < limit
    return [(pid, start, length) for pid, (_, start, length) in ranked], every


def interleave[T](per_space: list[list[T]], *, key: Callable[[T], uuid.UUID]) -> list[T]:
    """Rank one in each space before rank two in any; never compare distances."""
    ranked: list[T] = []
    taken: set[uuid.UUID] = set()
    for rank in range(max((len(hits) for hits in per_space), default=0)):
        for hits in per_space:
            if rank < len(hits) and (posting_id := key(hits[rank])) not in taken:
                taken.add(posting_id)
                ranked.append(hits[rank])
    return ranked


@dataclass(frozen=True)
class ChunkQueries:
    vectors: dict[Space, list[float]]
    qwen: bool = False
    fallback_reason: str | None = None


async def queries(
    vectors: dict[str, list[float]],
    subject: str,
    *,
    enabled: bool,
    keep_alive: str | None = None,
) -> ChunkQueries:
    if not enabled or not subject:
        return ChunkQueries({model: vector for model, vector in vectors.items()})
    try:
        query = await query_vectors.embed_question(subject, keep_alive=keep_alive)
    except query_vectors.QueryEmbeddingError as exc:
        return ChunkQueries(
            {model: vector for model, vector in vectors.items()}, fallback_reason=str(exc)
        )
    return ChunkQueries(
        {
            ChunkSpace(QWEN_4B, ranking=ExactRanking(list(query.values))): list(query.values),
            **{ChunkSpace(model, without_qwen=True): vector for model, vector in vectors.items()},
        },
        qwen=True,
    )


async def release_embedder() -> None:
    """Memory courtesy; a down daemon must reach the provider's own remedy."""
    try:
        await query_vectors.unload_embedder()
    except query_vectors.QueryEmbeddingError as exc:
        log.warning("query_embedder_release_failed", reason=str(exc))


class Question:
    """Keep embeddings warm within a question; release once after its last use."""

    def __init__(self, *, free_embedder: bool) -> None:
        self.free_embedder = free_embedder
        self.attempted = False

    async def __aenter__(self) -> Question:
        return self

    async def __aexit__(self, *args: object) -> None:
        if self.free_embedder and self.attempted:
            await release_embedder()

    async def queries(
        self, vectors: dict[str, list[float]], subject: str, *, enabled: bool
    ) -> ChunkQueries:
        self.attempted |= enabled and bool(subject)
        return await queries(
            vectors,
            subject,
            enabled=enabled,
            keep_alive="5m" if self.free_embedder else None,
        )


async def coverage(
    session: AsyncSession,
    vectors: dict[Space, list[float]],
    posting_spaces: Sequence[tuple[str, int | None]] = (),
) -> tuple[int, int]:
    """Disjoint coverage, counted as sets rather than a lookup for every posting."""
    qwen = (
        select(PostingChunk.posting_id)
        .join(Posting, Posting.id == PostingChunk.posting_id)
        .join(PostingChunkVector, PostingChunkVector.chunk_id == PostingChunk.id)
        .where(*chunk_filter(ChunkSpace(QWEN_4B)))
        .distinct()
        .cte("qwen_coverage")
    )
    bge_chunks = select(PostingChunk.posting_id).join(
        Posting, Posting.id == PostingChunk.posting_id
    )
    bge_chunks = bge_chunks.where(*chunk_filter(SentenceTransformerEmbedder.name))
    has_bge_query = any(
        isinstance(space, ChunkSpace) and space.model == SentenceTransformerEmbedder.name
        for space in vectors
    )
    if not has_bge_query:
        bge_chunks = bge_chunks.where(Posting.id.is_(None))
    bge_postings = select(Posting.id.label("posting_id")).where(
        Posting.closed_at.is_(None),
        Posting.description_embedding.is_not(None),
        Posting.embedding_model == SentenceTransformerEmbedder.name,
        Posting.embedding_revision.is_(None),
    )
    if (SentenceTransformerEmbedder.name, None) not in posting_spaces:
        bge_postings = bge_postings.where(Posting.id.is_(None))
    bge = bge_chunks.union(bge_postings).cte("bge_coverage")
    qwen_count, bge_count = (
        await session.execute(
            select(
                func.count().filter(qwen.c.posting_id.is_not(None)),
                func.count().filter(
                    and_(qwen.c.posting_id.is_(None), bge.c.posting_id.is_not(None))
                ),
            )
            .select_from(Posting)
            .outerjoin(qwen, qwen.c.posting_id == Posting.id)
            .outerjoin(bge, bge.c.posting_id == Posting.id)
            .where(Posting.closed_at.is_(None))
        )
    ).one()
    return int(qwen_count), int(bge_count)


def notice(reason: str | None) -> str | None:
    return f"searched with bge-small: {reason}" if reason else None
