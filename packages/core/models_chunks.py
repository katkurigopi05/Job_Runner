"""Posting chunks: one vector per 500 characters of a posting, not per posting.

One vector per posting covers what bge-small reads, its first 512 tokens.
Postings run to a median 5,874 characters and put their requirements last,
so a skill named there is invisible to the vector: of 143 postings that
mention RAG, the mention fell inside the embedded window in 27. Chunking the
way the owner's DP-800 work does (`AI_GENERATE_CHUNKS`, fixed 500-character
chunks overlapping by 25 characters, as it was read until 2026-11-06; 25 percent
since) took a semantic search for "RAG" from 0 of the top
10 to 4. `matching/chunks.py` has the rest.

The text is not stored. A chunk is its offsets into the posting's
whitespace-normalised description, which the posting already holds once; the
owner pruned postings to save space, and 80,000 copies of 500 characters
would have added 40 MB back. `content_hash` says which version of the text
the offsets point into, so an edited posting is re-chunked rather than read
at the wrong place.

`halfvec`, float16: half of float32's storage. The owner's DP-800 notes
measured the same top 10 in the same order at 106 rows; here, the same top-10
precision over 80,440 chunks, with near-ties further down reordering.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pgvector.sqlalchemy import BIT, HALFVEC
from sqlalchemy import (
    Computed,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from packages.core.models import EMBEDDING_DIM, Base


class PostingChunk(Base):
    """One fixed-size window of a posting's description, and its vector."""

    __tablename__ = "posting_chunks"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    posting_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("postings.id", ondelete="CASCADE"), nullable=False
    )
    #: 0, 1, 2 … in reading order.
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    #: Character offsets into the whitespace-normalised description.
    start: Mapped[int] = mapped_column(Integer, nullable=False)
    length: Mapped[int] = mapped_column(Integer, nullable=False)
    #: The posting's `content_hash` when it was chunked.
    content_hash: Mapped[str | None] = mapped_column(String(64))
    #: Characters each chunk shares with the next when it was cut
    #: (`chunks.OVERLAP`). Rows from before 2026-11-06 were cut at 25; a row cut
    #: at any other overlap than today's is stale, like one cut from old text.
    overlap: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("25"))
    #: Which embedder made the vector. Vectors from different models are not
    #: comparable, so a search only ever compares within one.
    embedding_model: Mapped[str] = mapped_column(String(64), nullable=False)
    embedding: Mapped[list[float]] = mapped_column(HALFVEC(EMBEDDING_DIM), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("posting_id", "ordinal", name="uq_posting_chunks_posting_ordinal"),
        Index("ix_posting_chunks_posting_id", "posting_id"),
    )


#: Qwen3-Embedding-4B's width.
LARGE_EMBEDDING_DIM = 2560


class PostingChunkVector(Base):
    """A chunk's vector from a larger model, beside the 384-dim one it already has.

    Qwen3-Embedding-4B found the posting a question was written from in its
    top five for 35 of 56 natural questions and 10 of 56 paraphrased ones over
    the whole index, where bge-small found 21 and 2 (2026-11-02). Its vectors
    are 2,560 wide, so they are a table of their own rather than a second
    width in `posting_chunks.embedding`, which everything that reads
    bge-small's vectors would then have to tell apart.

    A row belongs to one chunk and goes with it: a re-chunked posting loses
    its old chunks and their vectors together, so a vector never outlives the
    text it was made from.
    """

    __tablename__ = "posting_chunk_vectors"

    chunk_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("posting_chunks.id", ondelete="CASCADE"),
        primary_key=True,
    )
    #: Which model made the vector; only vectors of one model are compared.
    embedding_model: Mapped[str] = mapped_column(String(64), primary_key=True)
    embedding: Mapped[list[float]] = mapped_column(HALFVEC(LARGE_EMBEDDING_DIM), nullable=False)
    #: The vector's sign bits, kept by Postgres, for a fast first pass: 320
    #: bytes a chunk against 5 KB, so the whole column is 85 MB against 1.3 GB.
    #: Search shortlists by Hamming distance here and re-ranks those chunks
    #: with `embedding` (`chunk_search.ShortlistRanking`).
    embedding_bits: Mapped[str] = mapped_column(
        BIT(LARGE_EMBEDDING_DIM),
        Computed(f"binary_quantize(embedding)::bit({LARGE_EMBEDDING_DIM})", persisted=True),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


__all__ = ["LARGE_EMBEDDING_DIM", "PostingChunk", "PostingChunkVector"]
