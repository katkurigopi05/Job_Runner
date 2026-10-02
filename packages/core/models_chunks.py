"""Posting chunks: one vector per 500 characters of a posting, not per posting.

One vector per posting covers what bge-small reads, its first 512 tokens.
Postings run to a median 5,874 characters and put their requirements last,
so a skill named there is invisible to the vector: of 143 postings that
mention RAG, the mention fell inside the embedded window in 27. Chunking the
way the owner's DP-800 work does (`AI_GENERATE_CHUNKS`, fixed 500-character
chunks overlapping by 25) took a semantic search for "RAG" from 0 of the top
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

from pgvector.sqlalchemy import HALFVEC
from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, UniqueConstraint, func, text
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


__all__ = ["PostingChunk"]
