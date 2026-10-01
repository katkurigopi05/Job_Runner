"""Export every posting to a CSV — `make export-postings`.

One row per posting, open and closed:

    company_name, job_type, job_title, job_posting_url, posted_date,
    application_deadline, deadline_source, last_seen_on_board, closed_on,
    expired, job_description, embedding_model, description_embedding

Three decisions shape what the columns mean.

**The embedding is re-encoded with bge-small for every row, not copied from
pgvector.** On 2026-09-30 `description_embedding` held bge-small vectors for
11,183 postings, `lexical-idf@2` for 2,000, vectors with no model stamp for
2,999 and nothing for 6,769. A CSV carrying that mix invites a cosine across
models, which returns a plausible number and means nothing. Every row here is
one model, encoded from exactly the text `score.embed_postings` encodes — so
for the rows pgvector holds as bge-small the two are identical, which the
first export measured: maximum difference 0.0 across all 11,183. The cost is
about 80 postings a second on the first run; vectors are cached by the digest
of their text, so a re-export encodes only what changed.

**A deadline is read only from the posting's own words.** Job boards do not
publish one: 78 of 22,951 postings stated a date, 300 said "until filled".
Month-name and ISO dates only — "10/11/2026" is October or November depending
on who wrote it, and a wrong deadline is worse than none. A date with no year
is the first such date on or after the posting date.

**`expired` is yes, no or unknown, and unknown is most of it.** Yes when the
crawler found the posting removed from the employer's board (`closed_on`) or a
stated deadline has passed; no when a stated deadline is still ahead; unknown
otherwise. "Still listed at the last crawl" is not "still open today", and
`last_seen_on_board` says how old that observation is — run `make crawl` first
if it is stale.

The file is written under `storage/`, which is gitignored: it holds every
posting's full text.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import re
import sys
import time
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from packages.core import db as core_db
from packages.core.models import Company, Posting
from packages.matching.roles import canonical

if TYPE_CHECKING:
    import numpy as np

DEFAULT_OUT = Path("storage/exports/postings_with_embeddings.csv")
MODEL = "BAAI/bge-small-en-v1.5"

COLUMNS = (
    "company_name",
    "job_type",
    "job_title",
    "job_posting_url",
    "posted_date",
    "application_deadline",
    "deadline_source",
    "last_seen_on_board",
    "closed_on",
    "expired",
    "job_description",
    "embedding_model",
    "description_embedding",
)

#: `roles.canonical` keys, as a person would write them. "AI Engineer" is one
#: of the aliases of `machine_learning_engineer`.
JOB_TYPES = {
    "software_engineer": "Software Engineer",
    "backend_engineer": "Backend Engineer",
    "frontend_engineer": "Frontend Engineer",
    "fullstack_engineer": "Full Stack Engineer",
    "data_engineer": "Data Engineer",
    "data_scientist": "Data Scientist",
    "machine_learning_engineer": "AI / ML Engineer",
    "data_analyst": "Data Analyst",
    "devops_engineer": "DevOps / SRE",
}

# --- reading a deadline ---------------------------------------------------------

_MONTH = r"(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?"
_MONTH_NAMES = "jan feb mar apr may jun jul aug sep oct nov dec"
_MONTHS = {name: i for i, name in enumerate(_MONTH_NAMES.split(), 1)}
_CUE = re.compile(
    r"(application deadline|deadline to apply|deadline for applications|"
    r"applications? (?:will )?close|closing date|apply by|"
    r"accepting applications (?:until|through)|"
    r"applications (?:will be )?accepted (?:until|through)|"
    r"posting (?:will )?(?:close|expire)s?|will be open until|open until)",
    re.I,
)
_MDY = re.compile(rf"\b{_MONTH}\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s*(\d{{4}})?", re.I)
_DMY = re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+{_MONTH},?\s*(\d{{4}})?", re.I)
_ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_WINDOW = re.compile(r"application window is (\d{1,3}) days from the date", re.I)
_UNTIL_FILLED = re.compile(
    r"until (?:the )?(?:position|role|job) is (?:closed and )?filled|on an ongoing basis|"
    r"on a rolling basis|until filled",
    re.I,
)

#: How far past a cue a date may sit. "Application Deadline: December 16,
#: 2026" is adjacent; a date two sentences on is about something else.
_CUE_REACH = 80


def _make(year: int | None, month: int, day: int, posted: date | None) -> date | None:
    try:
        if year is not None:
            return date(year, month, day)
        base = posted or date.today()
        found = date(base.year, month, day)
        return found if found >= base else date(base.year + 1, month, day)
    except ValueError:
        return None


def _first_date(text: str, posted: date | None) -> date | None:
    """The earliest unambiguous date in `text`."""
    candidates: list[tuple[int, date | None]] = []
    for match in _MDY.finditer(text):
        year = int(match[3]) if match[3] else None
        candidates.append(
            (match.start(), _make(year, _MONTHS[match[1][:3].lower()], int(match[2]), posted))
        )
    for match in _DMY.finditer(text):
        year = int(match[3]) if match[3] else None
        candidates.append(
            (match.start(), _make(year, _MONTHS[match[2][:3].lower()], int(match[1]), posted))
        )
    for match in _ISO.finditer(text):
        candidates.append(
            (match.start(), _make(int(match[1]), int(match[2]), int(match[3]), posted))
        )
    dated = sorted((position, found) for position, found in candidates if found is not None)
    return dated[0][1] if dated else None


def read_deadline(description: str, posted: date | None) -> tuple[date | None, str]:
    """(deadline, where it came from). Never inferred from a posting's age."""
    mentioned = False
    for cue in _CUE.finditer(description):
        mentioned = True
        found = _first_date(description[cue.end() : cue.end() + _CUE_REACH], posted)
        if found:
            return found, "stated in posting"
    window = _WINDOW.search(description)
    if window and posted:
        days = int(window[1])
        return posted + timedelta(days=days), f"posted date + {days}-day window stated in posting"
    if _UNTIL_FILLED.search(description):
        return None, "open until filled"
    if mentioned:
        return None, "deadline mentioned, date not readable"
    return None, ""


def expiry(closed: date | None, deadline: date | None, today: date) -> str:
    """yes, no or unknown — see the module docstring for why unknown dominates."""
    if closed is not None:
        return "yes"
    if deadline is not None:
        return "yes" if deadline < today else "no"
    return "unknown"


def job_type(title: str | None) -> str:
    return JOB_TYPES.get(canonical(title or "") or "", "Other")


def vector_text(vector: list[float]) -> str:
    """pgvector's own text format. Nine significant digits round-trip float32."""
    return "[" + ",".join(f"{value:.9g}" for value in vector) + "]"


# --- encoding ---------------------------------------------------------------------


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def encode(texts: list[str], cache: Path) -> list[list[float]]:
    """bge-small vectors for `texts`, reusing any cached for identical text."""
    import numpy as np

    from packages.matching.embed import SentenceTransformerEmbedder

    known: dict[str, np.ndarray] = {}
    if cache.exists():
        stored = np.load(cache)
        known = dict(zip(stored["digests"].tolist(), stored["vectors"], strict=True))

    digests = [_digest(text) for text in texts]
    missing = sorted({d: t for d, t in zip(digests, texts, strict=True) if d not in known}.items())
    distinct = len(set(digests))
    print(
        f"embeddings: {len(texts)} postings, {distinct} distinct texts "
        f"({len(texts) - distinct} repeat another posting word for word); "
        f"{distinct - len(missing)} cached, {len(missing)} to encode",
        flush=True,
    )
    if missing:
        try:
            embedder = SentenceTransformerEmbedder(MODEL)
        except Exception as exc:  # noqa: BLE001 - missing package or model
            sys.exit(f"cannot load {MODEL} ({type(exc).__name__}); install the embeddings extra")
        started, step = time.perf_counter(), 1024
        for i in range(0, len(missing), step):
            batch = missing[i : i + step]
            for (digest, _), vector in zip(
                batch, embedder.encode([t for _, t in batch]), strict=True
            ):
                known[digest] = np.asarray(vector, dtype=np.float32)
            done = i + len(batch)
            rate = done / (time.perf_counter() - started)
            print(f"  {done}/{len(missing)} ({rate:.0f}/s)", flush=True)
        cache.parent.mkdir(parents=True, exist_ok=True)
        np.savez(cache, digests=np.array(list(known)), vectors=np.stack(list(known.values())))
    return [known[digest].tolist() for digest in digests]


# --- export -----------------------------------------------------------------------


async def _rows() -> list[Any]:
    async with core_db.get_sessionmaker()() as session:
        result = await session.execute(
            select(
                Company.name,
                Posting.title,
                Posting.url,
                Posting.description_raw,
                Posting.published_at,
                Posting.last_seen_at,
                Posting.closed_at,
            )
            .outerjoin(Company, Company.id == Posting.company_id)
            .order_by(Posting.closed_at.is_not(None), Company.name, Posting.title, Posting.id)
        )
        return list(result.all())


def _day(value: datetime | None) -> date | None:
    """The calendar date on this machine, not in UTC.

    The database stores UTC. A crawl at 5pm in California is past midnight
    there, so `.date()` on the stored value dates it tomorrow — the first
    export after a crawl reported every posting as unseen "today" for exactly
    that reason.
    """
    return value.astimezone().date() if value else None


def write(out: Path, rows: list[Any], vectors: list[list[float]], today: date) -> Counter[str]:
    out.parent.mkdir(parents=True, exist_ok=True)
    tally: Counter[str] = Counter()
    with out.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(COLUMNS)
        for (company, title, url, description, published, last_seen, closed), vector in zip(
            rows, vectors, strict=True
        ):
            posted, last_seen_on, closed_on = _day(published), _day(last_seen), _day(closed)
            deadline, source = read_deadline(description or "", posted)
            expired = expiry(closed_on, deadline, today)
            tally[f"expired={expired}"] += 1
            writer.writerow(
                (
                    company or "",
                    job_type(title),
                    title or "",
                    url,
                    posted.isoformat() if posted else "",
                    deadline.isoformat() if deadline else "",
                    source,
                    last_seen_on.isoformat() if last_seen_on else "",
                    closed_on.isoformat() if closed_on else "",
                    expired,
                    description or "",
                    MODEL,
                    vector_text(vector),
                )
            )
    return tally


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args(argv)

    rows = asyncio.run(_rows())
    # Exactly the text `score.embed_postings` encodes, so bge rows match pgvector.
    texts = [f"{title or ''}\n{description or ''}" for _, title, _, description, *_ in rows]
    vectors = encode(texts, args.out.parent / ".embedding_cache.npz")
    tally = write(args.out, rows, vectors, date.today())

    print(f"wrote {len(rows)} postings to {args.out} ({args.out.stat().st_size / 1e6:.0f} MB)")
    print(", ".join(f"{key} {count}" for key, count in sorted(tally.items())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
