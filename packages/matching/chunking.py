"""Split a posting so the embedder sees all of it.

`bge-small-en-v1.5` has a 512-token window. `score.embed_postings` encodes
`f"{title}\\n{description_raw}"` as one vector, so anything past that window is
dropped by the tokenizer — silently, because `encode()` returns a vector
either way.

Measured on the twelve real crawled postings in `tests/fixtures/golden`:

| | |
|---|---|
| postings over the 512-token limit | **12 of 12** |
| median length | 1,385 tokens |
| median fraction embedded | **37%** |
| skill-bearing sentences embedded | **15.1%** |
| postings whose requirements section is entirely truncated away | **4 of 12** |

The last two are the ones that matter. Skills do not sit uniformly through a
posting — they cluster in the requirements section, and that comes after the
company's pitch. So the 37% that *was* embedded is disproportionately the
pitch, and CLAUDE.md §15 already records that exact asymmetry costing the ATS
scorer its vocabulary: "`world`, `problems`, `believe` and the company's own
name recur throughout" while `Python`, `Java` and `C++` appear once each, late.
That was fixed for the scorer. One layer up, the embedder still had it.

## Why 1536 characters

About 384 tokens, so a chunk plus the title prefix clears 512 with room for
the tokenizer to disagree with the 4-chars-per-token estimate. Splitting at the
window exactly would put the two in a race.

## Why 25% overlap

The thing overlap prevents is a semantic unit landing across a boundary,
diluted in both neighbours and whole in neither. Measured as the fraction of
multi-sentence skill-bearing blocks that survive intact inside at least one
chunk, over the same twelve postings, varying the definition of a block:

```text
chunk 1536      0%      10%      15%      20%      25%
 >100 chars  90.4%    91.5%    96.4%   100.0%   100.0%
 >150 chars  79.2%    75.0%    91.7%   100.0%   100.0%
 >200 chars  86.7%    69.2%    91.7%   100.0%   100.0%
 >300 chars  55.6%    50.0%    66.7%   100.0%   100.0%
```

20% is the cheapest that reaches 100% at this size. 25% is what ships, for two
reasons. It also reaches 100% at 2048 where 20% does not, so the constant
survives a change of chunk size rather than silently falling off a cliff. And
**10% is repeatedly worse than no overlap at all** — 69.2% against 86.7% — which
is not noise: a shorter step shifts every boundary, and a boundary that lands
inside a block destroys it whether or not the neighbours overlap. An overlap
chosen to save storage can therefore cost coverage, so the margin is kept.

Storage is the cost and it is small: 12 postings become 58 chunks, so ~4.8 per
posting, and the owner's 19,018 postings become roughly 92,000 rows.

## What this does not establish

That retrieval gets better. That needs `bge-small` — blocked here by the
environment's `huggingface.co` denial — and graded queries, which
`docs/ML_EVALUATION.md` records the corpus not having. This measures whether
the text survives chunking intact, which is the half that is knowable offline.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: Characters per chunk. ~384 tokens, leaving headroom under bge-small's 512.
CHUNK_CHARS = 1536

#: Fraction of a chunk that repeats in the next one. See the module docstring:
#: chosen because it reaches full block coverage at 1536 *and* at 2048, and
#: because a smaller overlap measured worse than none.
OVERLAP_PCT = 25

#: Chunks shorter than this are dropped rather than embedded. A trailing
#: fragment carries no claim worth matching, and an embedding of it is a
#: near-duplicate row competing with its own parent chunk in the feed.
MIN_CHARS = 120

#: Prefer to break here, in order. A chunk that starts mid-sentence embeds a
#: fragment whose subject is in the previous chunk.
_BOUNDARY = re.compile(r"\n\n|\n|(?<=[.!?])\s")


@dataclass(frozen=True)
class Chunk:
    """One window of a posting, and where it came from.

    `start` and `end` are offsets into the original text rather than a copy of
    it, so a caller can show the surrounding context without this module
    deciding how much of it to keep.
    """

    ordinal: int
    start: int
    end: int
    text: str


def _snap(text: str, position: int, limit: int) -> int:
    """Move `position` back to the nearest boundary, if one is close enough.

    Bounded by `limit` so a posting written as one long paragraph is still
    chunked at roughly the intended size rather than collapsing to one chunk.
    """
    window = text[max(0, position - limit) : position]
    matches = list(_BOUNDARY.finditer(window))
    if not matches:
        return position
    return max(0, position - limit) + matches[-1].end()


def chunk_text(
    text: str,
    *,
    size: int = CHUNK_CHARS,
    overlap_pct: int = OVERLAP_PCT,
    min_chars: int = MIN_CHARS,
) -> list[Chunk]:
    """Split `text` into overlapping windows, broken at sentence boundaries.

    A text that fits in one window yields exactly one chunk covering all of it,
    so the common short posting costs nothing extra and reads identically to
    what `embed_postings` produced before.
    """
    if not text or not text.strip():
        return []
    if len(text) <= size:
        return [Chunk(ordinal=0, start=0, end=len(text), text=text)]

    if not 0 <= overlap_pct < 100:
        raise ValueError(f"overlap_pct must be in [0, 100), got {overlap_pct}")
    step = max(1, int(size * (1 - overlap_pct / 100)))
    snap_limit = max(1, size // 8)

    chunks: list[Chunk] = []
    start = 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            end = _snap(text, end, snap_limit)
            # `_snap` can land on or before `start` when a window holds no
            # boundary at all. Falling back to the hard cut keeps the loop
            # moving; without this a boundary-free posting never terminates.
            if end <= start:
                end = min(start + size, len(text))
        body = text[start:end]
        if body.strip() and (len(body) >= min_chars or not chunks):
            chunks.append(Chunk(ordinal=len(chunks), start=start, end=end, text=body))
        if end >= len(text):
            break
        nxt = _snap(text, start + step, snap_limit)
        start = nxt if nxt > start else start + step
    return chunks


def chunk_posting(title: str | None, description: str | None) -> list[Chunk]:
    """Chunks for one posting, each carrying the title.

    The title is repeated into every chunk rather than embedded once, because
    a chunk is retrieved on its own: a requirements list that never says
    "Backend Engineer" is a worse match for a backend query than the same list
    under its own heading, and the title is the strongest two or three tokens a
    posting has. The offsets stay relative to the description so a caller can
    still locate the chunk in the original text.
    """
    body = description or ""
    chunks = chunk_text(body)
    if not title:
        return chunks
    prefix = f"{title}\n"
    return [
        Chunk(ordinal=c.ordinal, start=c.start, end=c.end, text=prefix + c.text) for c in chunks
    ]


__all__ = ["CHUNK_CHARS", "MIN_CHARS", "OVERLAP_PCT", "Chunk", "chunk_posting", "chunk_text"]
