"""Chunking, and the coverage it exists to recover.

`bge-small-en-v1.5` truncates at 512 tokens and every one of the twelve real
crawled postings in `tests/fixtures/golden` is longer, so one vector per
posting embedded a median 37% of the text. The number that matters is not that
one: skills cluster in the requirements section and that comes last, so only
**15.1%** of skill-bearing sentences were inside the window, and on **4 of 12**
postings the requirements section was entirely outside it.

The positives here are measured on that real corpus rather than on text
written beside the chunker, because §15 records what writing a fixture beside
the code that reads it costs.
"""

from __future__ import annotations

import json
import pathlib
import re

import pytest

from packages.matching.chunking import (
    CHUNK_CHARS,
    MIN_CHARS,
    OVERLAP_PCT,
    chunk_posting,
    chunk_text,
)
from packages.matching.skill_vocab import find_skills

GOLDEN = pathlib.Path(__file__).parent / "fixtures/golden/postings.json"
POSTS = json.loads(GOLDEN.read_text())["postings"]

#: Characters bge-small sees before the tokenizer truncates, at ~4 per token.
WINDOW = 512 * 4


def _skill_sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", text) if s.strip() and find_skills(s)]


# --- the defect ------------------------------------------------------------


def test_every_real_posting_overflows_the_embedder_window() -> None:
    """The premise. If this stops being true, chunking stops being needed."""
    over = [p for p in POSTS if len(p["description"]) > WINDOW]
    assert len(over) == len(POSTS), f"only {len(over)} of {len(POSTS)} exceed the window"


def test_chunking_recovers_the_skills_truncation_was_dropping() -> None:
    """15.1% to 100% on the real corpus — the whole point of the table.

    Asserted as a floor rather than the exact figure: the corpus can gain
    postings, and a test that pins 100% would fail on a posting whose skills
    genuinely straddle every boundary. What must not regress is the gap.
    """
    before, after = [], []
    for post in POSTS:
        sentences = _skill_sentences(post["description"])
        if not sentences:
            continue
        chunks = chunk_posting(post["title"], post["description"])
        before.append(
            sum(1 for s in sentences if s in post["description"][:WINDOW]) / len(sentences)
        )
        after.append(sum(1 for s in sentences if any(s in c.text for c in chunks)) / len(sentences))

    truncated = sum(before) / len(before)
    chunked = sum(after) / len(after)
    assert truncated < 0.30, f"truncation was not the problem: {truncated:.1%} already covered"
    assert chunked > 0.95, f"chunking only reached {chunked:.1%}"


def test_no_chunk_overflows_the_window_once_the_title_is_added() -> None:
    """`chunk_posting` prefixes the title, so the budget is not `CHUNK_CHARS`.

    A chunk that overflows is the original defect at a smaller scale, and it
    would be invisible for the same reason: `encode()` returns a vector either
    way.
    """
    longest = max(len(c.text) for p in POSTS for c in chunk_posting(p["title"], p["description"]))
    assert longest <= WINDOW, f"longest chunk is {longest} chars against a {WINDOW} window"


# --- the mechanics ---------------------------------------------------------


def test_a_short_posting_is_one_chunk_covering_all_of_it() -> None:
    """The common case costs nothing and reads as it did before."""
    text = "Backend Engineer. We use Python and Postgres."
    chunks = chunk_text(text)
    assert len(chunks) == 1
    assert chunks[0].text == text
    assert (chunks[0].start, chunks[0].end) == (0, len(text))


def test_chunks_overlap_by_the_configured_fraction() -> None:
    body = ". ".join(f"Sentence number {i} about Python" for i in range(400)) + "."
    chunks = chunk_text(body)
    assert len(chunks) > 1
    steps = [b.start - a.start for a, b in zip(chunks, chunks[1:], strict=False)]
    expected = CHUNK_CHARS * (1 - OVERLAP_PCT / 100)
    # Boundaries snap to sentences, so the step varies around the nominal one.
    assert all(abs(s - expected) < CHUNK_CHARS * 0.2 for s in steps), steps


def test_offsets_point_into_the_original_description() -> None:
    """Callers locate a chunk by span; a wrong span quotes the wrong posting."""
    body = ". ".join(f"Clause {i} mentioning Kubernetes" for i in range(300)) + "."
    for chunk in chunk_text(body):
        assert body[chunk.start : chunk.end] == chunk.text


def test_the_title_rides_on_every_chunk_but_not_in_the_offsets() -> None:
    """A chunk is retrieved alone, so it needs to say what job it is.

    The offsets stay relative to the description — a caller showing context
    around a chunk must not be handed a span shifted by the title's length.
    """
    body = ". ".join(f"Requirement {i} involving Docker" for i in range(300)) + "."
    for chunk in chunk_posting("Staff Backend Engineer", body):
        assert chunk.text.startswith("Staff Backend Engineer\n")
        assert body[chunk.start : chunk.end] == chunk.text.split("\n", 1)[1]


def test_text_with_no_boundary_at_all_still_terminates() -> None:
    """`_snap` can land on or before the start; without the fallback it loops.

    A posting scraped as one unbroken string is not hypothetical — §15 records
    Ashby's location arriving as a whole concatenated sidebar.
    """
    chunks = chunk_text("x" * (CHUNK_CHARS * 3))
    assert len(chunks) > 1
    assert all(c.end > c.start for c in chunks)


def test_a_trailing_fragment_is_dropped_rather_than_embedded() -> None:
    """A near-empty tail competes with its own parent chunk in the feed."""
    body = "A" * (CHUNK_CHARS + MIN_CHARS // 4)
    assert all(len(c.text) >= MIN_CHARS for c in chunk_text(body))


def test_empty_and_blank_descriptions_yield_nothing() -> None:
    assert chunk_text("") == []
    assert chunk_text("   \n  ") == []
    assert chunk_posting("Engineer", None) == []


def test_an_impossible_overlap_is_refused() -> None:
    """100% overlap never advances. Refused rather than hung."""
    with pytest.raises(ValueError):
        chunk_text("x" * (CHUNK_CHARS * 2), overlap_pct=100)


# --- the chosen constants --------------------------------------------------


def test_the_overlap_is_the_one_that_was_measured() -> None:
    """25%, and 10% measured *worse than none* — 69.2% against 86.7%.

    Pinned because the obvious saving is to lower it. A shorter step shifts
    every boundary, and a boundary inside a block destroys it whether or not
    the neighbours overlap, so this is not a dial where less is cheaper.
    """
    assert OVERLAP_PCT == 25

    def coverage(pct: int) -> float:
        kept = []
        for post in POSTS:
            body = post["description"]
            chunks = chunk_text(body, overlap_pct=pct)
            blocks = _skill_blocks(body)
            if blocks:
                kept.append(
                    sum(1 for s, e in blocks if any(c.start <= s and e <= c.end for c in chunks))
                    / len(blocks)
                )
        return sum(kept) / len(kept)

    assert coverage(25) >= coverage(10), "25% no longer beats 10% on this corpus"
    assert coverage(25) > 0.95


def _skill_blocks(text: str) -> list[tuple[int, int]]:
    """Spans of consecutive skill-bearing sentences, as a requirements list."""
    out: list[tuple[int, int]] = []
    run: tuple[int, int] | None = None
    for match in re.finditer(r"[^.!?\n]+[.!?]?", text):
        if find_skills(match.group().strip()):
            run = (run[0] if run else match.start(), match.end())
        elif run:
            out.append(run)
            run = None
    if run:
        out.append(run)
    return [b for b in out if b[1] - b[0] > 200]


# --- persistence -----------------------------------------------------------


async def test_chunks_round_trip_through_the_halfvec_column(db_session) -> None:
    """float16 storage, and the embedder's values survive it.

    Measured at 92,000 rows before choosing the type: half the storage, the
    same top-10 in the same order on 40 of 40 queries, largest distance
    difference 5.46e-05. This is the narrower claim the suite can make without
    a corpus — that a vector written as halfvec reads back as the same vector
    to the precision float16 carries.
    """
    import uuid as _uuid

    from sqlalchemy import select

    from packages.core.models import Company, Posting, PostingChunk
    from packages.matching.embed import LexicalEmbedder
    from packages.matching.score import embed_chunks

    company = Company(name="Acme", domain="acme.test")
    db_session.add(company)
    await db_session.flush()

    body = ". ".join(f"Requirement {i} naming Python and Kubernetes" for i in range(300)) + "."
    posting = Posting(
        company_id=company.id,
        url=f"https://acme.test/{_uuid.uuid4()}",
        title="Staff Backend Engineer",
        description_raw=body,
        first_seen_at=__import__("datetime").datetime.now(__import__("datetime").UTC),
    )
    db_session.add(posting)
    await db_session.flush()

    embedder = LexicalEmbedder()
    written = await embed_chunks(db_session, [posting], embedder=embedder)
    assert written > 1, "a posting this long should produce several chunks"

    rows = (
        (
            await db_session.execute(
                select(PostingChunk)
                .where(PostingChunk.posting_id == posting.id)
                .order_by(PostingChunk.ordinal)
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == written
    assert [r.ordinal for r in rows] == list(range(len(rows)))
    assert all(r.embedding_model == embedder.name for r in rows)
    assert all(body[r.start_char : r.end_char] == r.text.split("\n", 1)[1] for r in rows)

    expected = embedder.encode([rows[0].text])[0]
    stored = list(rows[0].embedding)
    assert len(stored) == len(expected)
    # float16 carries ~3 decimal digits. The measurement that chose it allowed
    # 5.46e-05 on a cosine; this is the per-component form of the same claim.
    assert max(abs(a - b) for a, b in zip(stored, expected, strict=True)) < 1e-3


async def test_re_embedding_replaces_chunks_rather_than_merging_them(db_session) -> None:
    """A posting whose text changed has boundaries that moved.

    Pairing old rows to new ones by ordinal would attach a vector to text it
    was not made from — the mismatch `embedding_model` exists to catch, and
    invisible in the same way.
    """
    import uuid as _uuid
    from datetime import UTC, datetime

    from sqlalchemy import func, select

    from packages.core.models import Company, Posting, PostingChunk
    from packages.matching.embed import LexicalEmbedder
    from packages.matching.score import embed_chunks

    company = Company(name="Acme2", domain="acme2.test")
    db_session.add(company)
    await db_session.flush()

    long_body = ". ".join(f"Clause {i} about Python" for i in range(400)) + "."
    posting = Posting(
        company_id=company.id,
        url=f"https://acme2.test/{_uuid.uuid4()}",
        title="Engineer",
        description_raw=long_body,
        first_seen_at=datetime.now(UTC),
    )
    db_session.add(posting)
    await db_session.flush()

    embedder = LexicalEmbedder()
    first = await embed_chunks(db_session, [posting], embedder=embedder)
    assert first > 1

    posting.description_raw = "Engineer. We use Python."
    second = await embed_chunks(db_session, [posting], embedder=embedder)
    assert second == 1, "the shorter text should collapse to one chunk"

    count = await db_session.scalar(
        select(func.count()).select_from(PostingChunk).where(PostingChunk.posting_id == posting.id)
    )
    assert count == 1, f"{count} rows left behind — the old chunks were not replaced"


async def test_the_posting_vector_is_untouched_by_chunking(db_session) -> None:
    """`Match.score` ranks on the column, and this must not re-rank the feed.

    `rubric.py` argues it at length: the cosine is what `test_matching.py`
    validates and what `min_match_score` compares against.
    """
    import uuid as _uuid
    from datetime import UTC, datetime

    from packages.core.models import Company, Posting
    from packages.matching.embed import LexicalEmbedder
    from packages.matching.score import embed_chunks, embed_postings

    company = Company(name="Acme3", domain="acme3.test")
    db_session.add(company)
    await db_session.flush()

    body = ". ".join(f"Line {i} with Python" for i in range(300)) + "."
    posting = Posting(
        company_id=company.id,
        url=f"https://acme3.test/{_uuid.uuid4()}",
        title="Engineer",
        description_raw=body,
        first_seen_at=datetime.now(UTC),
    )
    db_session.add(posting)
    await db_session.flush()

    embedder = LexicalEmbedder()
    await embed_postings(db_session, [posting], embedder=embedder)
    whole = list(posting.description_embedding)

    await embed_chunks(db_session, [posting], embedder=embedder)
    assert list(posting.description_embedding) == whole
