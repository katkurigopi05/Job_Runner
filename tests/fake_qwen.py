"""A stand-in for Qwen3-Embedding-4B in search tests: no model, no network.

Posting chunks have Qwen vectors only since 2026-11-22. Tests that searched by
meaning through bge-small's chunk vectors give each chunk a "Qwen" vector made
by the lexical embedder, padded to Qwen's width, and embed the question the
same way. Padding with zeros leaves every cosine as the lexical embedder has
it, so what is near in these tests is what was near before.
"""

from __future__ import annotations

import pytest
from sqlalchemy import exists, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.config import Settings, get_settings
from packages.core.models import Posting
from packages.core.models_chunks import LARGE_EMBEDDING_DIM, PostingChunk, PostingChunkVector
from packages.matching import query_vectors
from packages.matching.chunk_vectors import QWEN_4B
from packages.matching.chunks import chunk_postings, normalize
from packages.matching.embed import LexicalEmbedder

LEXICAL = LexicalEmbedder()


def widen(vector: list[float]) -> list[float]:
    """A lexical vector at Qwen's width; the cosine between two is unchanged."""
    return [*vector, *([0.0] * (LARGE_EMBEDDING_DIM - len(vector)))]


def qwen_like(text: str) -> list[float]:
    return widen(LEXICAL.encode([text])[0])


async def embed_chunks(session: AsyncSession) -> None:
    """Cut every open posting, then give each chunk without one a fake Qwen vector."""
    await chunk_postings(session)
    rows = (
        await session.execute(
            select(
                PostingChunk.id, PostingChunk.start, PostingChunk.length, Posting.description_raw
            )
            .join(Posting, Posting.id == PostingChunk.posting_id)
            .where(
                ~exists().where(
                    PostingChunkVector.chunk_id == PostingChunk.id,
                    PostingChunkVector.embedding_model == QWEN_4B,
                )
            )
        )
    ).all()
    if not rows:
        return
    await session.execute(
        insert(PostingChunkVector),
        [
            {
                "chunk_id": chunk_id,
                "embedding_model": QWEN_4B,
                "embedding": qwen_like(normalize(body or "")[start : start + length]),
            }
            for chunk_id, start, length, body in rows
        ],
    )
    await session.flush()


def use_fake_qwen(monkeypatch: pytest.MonkeyPatch, settings: Settings | None = None) -> list[str]:
    """Turn Qwen search on, embedding each question lexically; returns the questions."""
    asked: list[str] = []

    async def embed(question: str, *, keep_alive: str | None = None) -> query_vectors.QueryVector:
        asked.append(question)
        return query_vectors.QueryVector(QWEN_4B, "fake-qwen", tuple(qwen_like(question)), 0.0)

    async def released() -> None:
        pass

    monkeypatch.setattr(settings or get_settings(), "chat_qwen_search", True)
    monkeypatch.setattr(query_vectors, "embed_question", embed)
    monkeypatch.setattr(query_vectors, "unload_embedder", released, raising=False)
    return asked
