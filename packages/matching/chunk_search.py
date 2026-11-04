"""Chunk search spaces: bge-small, or Qwen with bge-small for uncovered postings.

Distances never cross model boundaries. Both use the same ranking query and
current-text predicate; the caller merges their ranks. Plain string keys retain
the original search exactly when CHAT_QWEN_SEARCH is off.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

import structlog
from sqlalchemy import ColumnElement, and_, any_, exists, func, literal, select
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from packages.core.models import Posting
from packages.core.models_chunks import PostingChunk, PostingChunkVector
from packages.matching import query_vectors
from packages.matching.chunk_vectors import QWEN_4B as QWEN_4B
from packages.matching.embed import SentenceTransformerEmbedder

log = structlog.get_logger(__name__)


#: Chunks the 1-bit shortlist takes before exact re-ranking, at least. On
#: 2026-11-02 a shortlist of 500 gave the exact top five on all 112 benchmark
#: questions; 2,000 leaves room for the filters (area, place, level) that drop
#: shortlisted postings before enough are kept.
SHORTLIST_CHUNKS = 2000
#: How much wider the shortlist is than the window a caller asks for.
SHORTLIST_FACTOR = 4

#: A posting's nearest chunk: posting, span start, span length, distance, company.
Row = tuple[uuid.UUID, int, int, float, uuid.UUID | None]


def sign_bits(vector: Sequence[float]) -> str:
    """What `binary_quantize` stores: 1 where a value is above zero."""
    return "".join("1" if value > 0 else "0" for value in vector)


_UUIDS = ARRAY(PG_UUID(as_uuid=True))


def one_of(column: ColumnElement[uuid.UUID] | Any, ids: Sequence[uuid.UUID]) -> ColumnElement[bool]:
    """`column = ANY(:ids)`: one array parameter however many ids there are.

    An IN list spends a parameter an id, and asyncpg refuses a statement with
    more than 32,767. A widened shortlist passed that on 2026-11-03.
    """
    return column == any_(literal(list(ids), type_=_UUIDS))


class ShortlistRanking:
    """Qwen chunks by exact distance, found through their sign bits; one per question.

    The sign bits (`embedding_bits`, 85 MB for the whole table) pick the
    nearest chunks by Hamming distance, and only those are scored with the full
    vectors, so the 1.3 GB of vectors is never scanned whole. Measured
    2026-11-03: the exact scan this replaces was 1.28 s of the 2.9 s Qwen added
    to a question. Named postings and companies are few, and are scored exactly
    without a shortlist. The shortlist is kept for the question, shared by its
    stages, and widened when a caller needs more; nothing outlives the question.
    """

    def __init__(self, vector: list[float]) -> None:
        self.vector = vector
        self.bits = sign_bits(vector)
        self._size = 0
        self._complete = False
        self._rows: list[Row] = []

    async def _best(
        self,
        session: AsyncSession,
        *,
        chunk_ids: list[uuid.UUID] | None = None,
        posting_ids: list[uuid.UUID] | None = None,
        companies: list[uuid.UUID] | None = None,
    ) -> list[Row]:
        """Each posting's nearest current chunk among those named, nearest first."""
        distance = PostingChunkVector.embedding.cosine_distance(self.vector)
        query = (
            select(
                PostingChunk.posting_id,
                PostingChunk.start,
                PostingChunk.length,
                distance,
                Posting.company_id,
            )
            .select_from(PostingChunkVector)
            .join(PostingChunk, PostingChunk.id == PostingChunkVector.chunk_id)
            .join(Posting, Posting.id == PostingChunk.posting_id)
            .where(PostingChunkVector.embedding_model == QWEN_4B, *_current_chunks())
        )
        if chunk_ids is not None:
            query = query.where(one_of(PostingChunkVector.chunk_id, chunk_ids))
        if posting_ids is not None:
            query = query.where(one_of(PostingChunk.posting_id, posting_ids))
        if companies:
            query = query.where(Posting.company_id.in_(companies))
        query = query.distinct(PostingChunk.posting_id).order_by(
            PostingChunk.posting_id, distance, PostingChunk.ordinal
        )
        rows: list[Row] = [
            (pid, start, length, float(dist), company)
            for pid, start, length, dist, company in (await session.execute(query)).all()
        ]
        return sorted(rows, key=lambda row: (row[3], str(row[0])))

    async def _shortlist(self, session: AsyncSession, size: int) -> None:
        hamming = PostingChunkVector.embedding_bits.hamming_distance(self.bits)
        ids = list(
            (
                await session.scalars(
                    select(PostingChunkVector.chunk_id)
                    .where(PostingChunkVector.embedding_model == QWEN_4B)
                    .order_by(hamming, PostingChunkVector.chunk_id)
                    .limit(size)
                )
            ).all()
        )
        self._size, self._complete = size, len(ids) < size
        self._rows = await self._best(session, chunk_ids=ids) if ids else []

    async def read(
        self,
        session: AsyncSession,
        *,
        among: list[uuid.UUID] | None,
        companies: list[uuid.UUID] | None,
        limit: int,
    ) -> tuple[list[tuple[uuid.UUID, int, int]], bool]:
        """Postings by their nearest chunk, and whether nothing was left unread."""
        if among is not None:
            rows = await self._best(session, posting_ids=among, companies=companies)
            return [(pid, start, length) for pid, start, length, _, _ in rows], True
        if companies:
            rows = await self._best(session, companies=companies)
            return [(p, s, n) for p, s, n, dist, _ in rows if dist < 1.0], True
        size = max(SHORTLIST_CHUNKS, limit * SHORTLIST_FACTOR)
        if size > self._size and not self._complete:
            await self._shortlist(session, size)
        kept = [(p, s, n) for p, s, n, dist, _ in self._rows if dist < 1.0]
        return kept, self._complete


@dataclass(frozen=True)
class ChunkSpace:
    model: str
    without_qwen: bool = False
    ranking: ShortlistRanking | None = field(default=None, compare=False, repr=False)


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
        # The question's shortlist, shared by its stages and widened on demand.
        return await model.ranking.read(session, among=among, companies=companies, limit=limit)
    column = PostingChunkVector.embedding if is_qwen(model) else PostingChunk.embedding
    distance = column.cosine_distance(vector)
    query = select(PostingChunk.posting_id, PostingChunk.start, PostingChunk.length, distance).join(
        Posting, Posting.id == PostingChunk.posting_id
    )
    if is_qwen(model):
        query = query.join(PostingChunkVector, PostingChunkVector.chunk_id == PostingChunk.id)
    query = query.where(*chunk_filter(model))
    if among is not None:
        query = query.where(one_of(PostingChunk.posting_id, among))
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
    #: (open postings the Qwen space covers, open postings only bge-small
    #: reaches), counted once per question; None when not counted.
    covered: tuple[int, int] | None = None

    @property
    def bge_needed(self) -> bool:
        """Whether any open posting lacks Qwen vectors, so bge-small must still search."""
        return self.covered is None or self.covered[1] > 0


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
            ChunkSpace(QWEN_4B, ranking=ShortlistRanking(list(query.values))): list(query.values),
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
        self.covered: tuple[int, int] | None = None

    async def __aenter__(self) -> Question:
        return self

    async def __aexit__(self, *args: object) -> None:
        if self.free_embedder and self.attempted:
            await release_embedder()

    async def queries(
        self,
        vectors: dict[str, list[float]],
        subject: str,
        *,
        enabled: bool,
        session: AsyncSession | None = None,
        posting_spaces: Sequence[tuple[str, int | None]] = (),
    ) -> ChunkQueries:
        """The question's spaces, with bge-small left out when Qwen covers every posting.

        Coverage is counted once per question. With nothing uncovered, the
        bge-small spaces for "postings without Qwen vectors" can only find
        nothing, and each of their scans cost about 0.2 s (2026-11-03).
        """
        self.attempted |= enabled and bool(subject)
        result = await queries(
            vectors,
            subject,
            enabled=enabled,
            keep_alive="5m" if self.free_embedder else None,
        )
        if not result.qwen or session is None:
            return result
        if self.covered is None:
            self.covered = await coverage(session, result.vectors, posting_spaces)
        if self.covered[1] == 0:
            only_qwen = {space: v for space, v in result.vectors.items() if is_qwen(space)}
            return ChunkQueries(only_qwen, qwen=True, covered=self.covered)
        return ChunkQueries(result.vectors, qwen=True, covered=self.covered)


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
