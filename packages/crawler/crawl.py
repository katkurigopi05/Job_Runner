"""One crawl cycle: poll each company, emit only what changed.

Change detection is the whole point. Career boards are mostly static between
polls, so a cycle that re-emitted every posting each run would flood matching
and the review queue with work that is not new. Two hashes guard against that:

- **Board-level** — if the whole response is byte-identical to last time,
  nothing on that board changed and no postings are parsed at all.
- **Posting-level** — a board that changed usually changed in one posting, so
  each is hashed separately and only genuinely new or edited ones are emitted.

Gate 5 asks that a second run emits zero postings. That falls out of this.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

import structlog
from sqlalchemy import case, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models import Company, Posting
from packages.crawler.extract import CompanySeed, ExtractedPosting, extractor_for
from packages.crawler.fetch import Blocked, PoliteFetcher

log = structlog.get_logger(__name__)

#: Called with each company's outcome as the cycle produces it. The `Company`
#: is `None` when the seed never reached the registry — see `crawl_company`.
ResultHook = Callable[["Company | None", "CompanyResult"], Awaitable[None]]


@dataclass
class CompanyResult:
    company: str
    fetched: bool = False
    new_postings: int = 0
    updated_postings: int = 0
    closed_postings: int = 0
    #: Postings that were closed and are listed again unchanged. Counted
    #: apart from `updated` because nothing about them changed — there is
    #: nothing to re-embed, only a feed to correct.
    reopened_postings: int = 0
    skipped_reason: str | None = None
    error: str | None = None
    waited_seconds: float = 0.0
    #: Set when the board fetched and parsed to nothing while we still hold
    #: open postings for it. Almost always a broken extractor, not an empty
    #: board, so nothing is closed and this is surfaced instead.
    suspect_parse: bool = False
    #: robots.txt said no, as opposed to any other reason for skipping.
    #:
    #: A separate flag rather than reading `skipped_reason`, which carries
    #: three unrelated outcomes — "not due yet", "no extractor for X", and the
    #: text of a `Blocked`. Telling them apart by substring works until a
    #: reason is reworded, and the caller that gets it wrong here is the one
    #: deciding how long to back a host off.
    blocked: bool = False
    #: True when the board answered but was byte-identical to last time. The
    #: cycle did its job; there was simply nothing new.
    unchanged: bool = False
    #: Ids of the postings a downstream stage would have to look at again.
    #: Carried so matching can work from what changed rather than re-reading
    #: every open posting in the database.
    changed_posting_ids: list[uuid.UUID] = field(default_factory=list)

    @property
    def emitted(self) -> int:
        return self.new_postings + self.updated_postings

    @property
    def status(self) -> str:
        """One word for what happened, for `CompanyCrawlState.last_status`."""
        if self.error:
            return "error"
        if self.blocked:
            return "blocked"
        if self.suspect_parse:
            return "suspect"
        if self.skipped_reason:
            return "skipped"
        if self.unchanged:
            return "unchanged"
        return "ok"


@dataclass
class CrawlReport:
    results: list[CompanyResult] = field(default_factory=list)

    @property
    def emitted(self) -> int:
        return sum(r.emitted for r in self.results)

    @property
    def fetched(self) -> int:
        return sum(1 for r in self.results if r.fetched)

    @property
    def blocked(self) -> list[str]:
        return [r.company for r in self.results if r.skipped_reason]

    @property
    def failed(self) -> list[str]:
        return [r.company for r in self.results if r.error]

    @property
    def suspect(self) -> list[str]:
        """Boards that parsed to nothing while holding open postings."""
        return [r.company for r in self.results if r.suspect_parse]

    def summary(self) -> str:
        summary = (
            f"{self.fetched} boards fetched, {self.emitted} postings emitted, "
            f"{len(self.blocked)} skipped, {len(self.failed)} failed"
        )
        if self.suspect:
            summary += f", {len(self.suspect)} suspect ({', '.join(self.suspect)})"
        failures = [f"{result.company}: {result.error}" for result in self.results if result.error]
        return f"{summary} [{'; '.join(failures)}]" if failures else summary


async def upsert_company(session: AsyncSession, seed: CompanySeed) -> Company:
    """Find or create the Company row for a seed entry."""
    company = await session.scalar(select(Company).where(Company.name == seed.name))
    if company is None:
        company = Company(name=seed.name)
        session.add(company)

    company.domain = seed.domain
    company.careers_url = seed.careers_url
    company.ats_type = seed.ats
    # Written every cycle so the row can name its own board. Until now the
    # slug lived only in the seed file, which is fine while a cycle is one
    # pass over that file and every call already holds the `CompanySeed` —
    # and useless to anything handed a company id instead.
    company.slug = seed.slug
    company.poll_interval_s = seed.poll_interval_s
    await session.flush()
    return company


def is_due(company: Company, *, now: datetime | None = None) -> bool:
    """Whether this company's poll interval has elapsed."""
    if company.last_polled_at is None:
        return True
    current = now or datetime.now(UTC)
    last = company.last_polled_at
    if last.tzinfo is None:
        last = last.replace(tzinfo=UTC)
    return (current - last).total_seconds() >= company.poll_interval_s


@dataclass
class StoreResult:
    """What one board's postings did, by id rather than by count.

    Counts are what a report needs; ids are what the next stage needs.
    `handle_crawl` re-embeds and re-scores every open posting whenever a cycle
    emits anything, which is affordable at 29 companies and is the whole cost
    of a cycle at 3,500. It cannot do better than that while the crawler only
    says *how many* postings changed.
    """

    new: list[uuid.UUID] = field(default_factory=list)
    updated: list[uuid.UUID] = field(default_factory=list)
    #: Postings that were closed and are listed again, byte for byte as
    #: before. Not `updated`: nothing about them changed, so there is nothing
    #: to re-embed — but they are open again and the feed has to say so.
    reopened: list[uuid.UUID] = field(default_factory=list)

    @property
    def changed(self) -> list[uuid.UUID]:
        """Everything a downstream stage would have to look at again."""
        return [*self.new, *self.updated]


async def _store(
    session: AsyncSession,
    company: Company,
    extracted: list[ExtractedPosting],
    *,
    now: datetime | None = None,
) -> StoreResult:
    """Upsert a board's postings in one statement, returning what changed.

    Three things happen here that did not before.

    **Every sighting is recorded, not only the changes.** A posting whose
    content is identical used to be skipped entirely, so `last_seen_at` is
    written for all of them. That is what makes "still listed" a fact rather
    than an inference from `_close_missing`'s set difference.

    **A posting that reappears is open again even if nothing about it
    changed.** The old code cleared `closed_at` inside the branch it took only
    when the hash differed, so a posting that was closed and then relisted
    unchanged — a requisition put on hold and resumed, which is the ordinary
    case — stayed closed forever, invisible to matching, with the board saying
    plainly that it was open.

    **The read is narrow.** Classifying used to load whole `Posting` objects
    for the company, including `description_raw` and a 384-dimension vector
    per row, to compare a hash. Only four small columns are needed.
    """
    current = now or datetime.now(UTC)
    result = StoreResult()
    if not extracted:
        return result

    # A board that lists the same id twice would otherwise take the cycle
    # down: Postgres refuses an `ON CONFLICT DO UPDATE` whose VALUES touch one
    # row twice ("cannot affect row a second time"), and it refuses the whole
    # statement, so one malformed board would cost every posting in it. The
    # last listing wins, which is the one a sequential read would have left in
    # place.
    deduped = {item.external_id: item for item in extracted}
    if len(deduped) != len(extracted):
        log.warning(
            "board_listed_a_posting_twice",
            company=company.name,
            listed=len(extracted),
            distinct=len(deduped),
        )
    extracted = list(deduped.values())

    # Four columns, on the new `company_id` index. The embeddings and
    # descriptions this used to pull back were never looked at.
    rows = await session.execute(
        select(Posting.id, Posting.external_id, Posting.content_hash, Posting.closed_at).where(
            Posting.company_id == company.id
        )
    )
    existing = {row.external_id: row for row in rows if row.external_id}

    values = [
        {
            "company_id": company.id,
            "ats_type": item.ats_type,
            "external_id": item.external_id,
            "url": item.url,
            "title": item.title,
            "location": item.location,
            "description_raw": item.description_raw,
            "published_at": item.published_at,
            "content_hash": item.content_hash,
            "first_seen_at": current,
            "last_seen_at": current,
        }
        for item in extracted
    ]

    statement = pg_insert(Posting).values(values)
    changed = Posting.content_hash.is_distinct_from(statement.excluded.content_hash)
    statement = statement.on_conflict_do_update(
        constraint="uq_postings_company_external_id",
        set_={
            # Always: the board listed it just now, and it is open whatever we
            # believed a moment ago.
            "last_seen_at": statement.excluded.last_seen_at,
            "closed_at": None,
            # Small columns, written unconditionally. `url` is deliberately
            # among them: `posting_hash` covers title, location and body but
            # not the link, so a posting that moved used to keep the old URL
            # indefinitely — the apply pipeline would follow a dead link and
            # report the posting closed.
            "url": statement.excluded.url,
            "title": statement.excluded.title,
            "location": statement.excluded.location,
            "content_hash": statement.excluded.content_hash,
            # A board that stops reporting a date must not erase the one we
            # already have; `published_at` is the only evidence that
            # `poll_interval_s` is set sensibly.
            "published_at": func.coalesce(statement.excluded.published_at, Posting.published_at),
            # Guarded, unlike the rest. A job description is large enough to
            # be stored out of line, and assigning it its own current value
            # still rewrites it. Every other column here is a few bytes.
            "description_raw": case(
                (changed, statement.excluded.description_raw), else_=Posting.description_raw
            ),
            # An edited posting's vector describes text it no longer contains,
            # so it is dropped along with the text it was made from.
            #
            # `embed_postings` re-embeds a posting whose vector is missing or
            # whose stamps are from another model or corpus revision — and an
            # edit changes none of those. So a requisition rewritten from a
            # Python role into a Rust one kept the vector for the Python one:
            # measured, not supposed. Cosine against it does not fail, it
            # returns a plausible number, and the feed ranks by it.
            #
            # Clearing it here rather than re-embedding here on purpose.
            # Embedding wants the corpus statistics and the crawler has no
            # business holding them; a NULL vector is a fact the next matching
            # pass already knows how to act on.
            "description_embedding": case((changed, None), else_=Posting.description_embedding),
            "embedding_model": case((changed, None), else_=Posting.embedding_model),
            "embedding_revision": case((changed, None), else_=Posting.embedding_revision),
        },
    )
    # `first_seen_at` is absent from the update set on purpose: it is when we
    # noticed the posting, and a posting we notice again was not born again.
    await session.execute(statement)

    for item in extracted:
        previous = existing.get(item.external_id)
        if previous is None:
            continue
        if previous.content_hash != item.content_hash:
            result.updated.append(previous.id)
        elif previous.closed_at is not None:
            result.reopened.append(previous.id)

    # New rows have no id until the insert lands, so they are read back rather
    # than guessed at.
    if len(existing) < len(extracted):
        inserted = await session.execute(
            select(Posting.id).where(
                Posting.company_id == company.id,
                Posting.external_id.in_(
                    [item.external_id for item in extracted if item.external_id not in existing]
                ),
            )
        )
        result.new = list(inserted.scalars().all())

    await session.flush()
    return result


async def _close_missing(
    session: AsyncSession,
    company: Company,
    extracted: list[ExtractedPosting],
    *,
    seen_at: datetime,
) -> tuple[int, bool]:
    """Mark postings the board no longer lists as closed.

    Returns `(closed, suspect)`. A board that fetched cleanly and parsed to
    *nothing* while we still hold open postings is refused: the far likelier
    explanation is that the extractor broke — the ATS changed its payload
    shape, or served a degraded response with a 200 — than that an employer
    closed every requisition between two polls.

    Getting this wrong is expensive and silent. Closing the whole set drops
    those postings out of the match feed with no error anywhere, and the next
    successful crawl re-creates them as new, so even the audit trail reads
    like normal churn. Declining to act costs one stale posting until the
    extractor is fixed; the alternative costs the feed.
    """
    open_count = await session.scalar(
        select(func.count())
        .select_from(Posting)
        .where(Posting.company_id == company.id, Posting.closed_at.is_(None))
    )

    if not extracted and open_count:
        log.warning(
            "crawl_parse_yielded_nothing",
            company=company.name,
            open_postings=open_count,
            action="left open; extractor is the likely fault",
        )
        return 0, True

    # One statement, and no `Posting` objects. This used to load every open
    # posting for the company — `description_raw` and a 384-dimension vector
    # per row — in order to write one timestamp on some of them.
    #
    # "Absent from the board" is read off `last_seen_at` rather than from a
    # `NOT IN` list of everything that *was* present. `_store` has just
    # stamped `seen_at` on every posting this response listed, so anything
    # still carrying an older stamp was not in it. That is the same question
    # asked the cheap way round: a board with 5,000 open roles would otherwise
    # put 5,000 ids into the statement to describe the handful that are gone.
    #
    # It is only sound because both halves share one timestamp, which is why
    # `seen_at` is a parameter here rather than a fresh `now()` — two clocks a
    # few milliseconds apart would close the entire board.
    #
    # `external_id IS NOT NULL` keeps the old loop's guard: a posting with no
    # external id cannot be matched against the board's list, so it cannot be
    # concluded to be absent from it.
    statement = (
        update(Posting)
        .where(
            Posting.company_id == company.id,
            Posting.closed_at.is_(None),
            Posting.external_id.is_not(None),
            (Posting.last_seen_at.is_(None)) | (Posting.last_seen_at < seen_at),
        )
        .values(closed_at=seen_at)
    )
    result = await session.execute(statement)
    await session.flush()
    return result.rowcount or 0, False


async def _poll_company(
    session: AsyncSession,
    seed: CompanySeed,
    fetcher: PoliteFetcher,
    *,
    force: bool = False,
) -> CompanyResult:
    """Poll one company. Does not commit."""
    result = CompanyResult(company=seed.name)

    extractor = extractor_for(seed.ats)
    if extractor is None:
        result.skipped_reason = f"no extractor for {seed.ats}"
        return result

    company = await upsert_company(session, seed)

    if not force and not is_due(company):
        result.skipped_reason = "not due yet"
        return result

    url = extractor.board_url(seed.slug)

    try:
        response = await fetcher.fetch(url)
    except Blocked as exc:
        # Being told no is a normal outcome, not an error to work around.
        log.info("crawl_blocked", company=seed.name, reason=str(exc))
        result.skipped_reason = str(exc)
        result.blocked = True
        return result
    except Exception as exc:  # noqa: BLE001 - one bad host must not stop the cycle
        log.warning("crawl_fetch_failed", company=seed.name, error=type(exc).__name__)
        result.error = f"fetch failed: {type(exc).__name__}"
        return result

    result.fetched = True
    result.waited_seconds = response.waited
    company.last_polled_at = datetime.now(UTC)

    if not response.ok:
        result.error = f"HTTP {response.status}"
        log.warning(
            "crawl_board_request_failed",
            company=seed.name,
            slug=seed.slug,
            url=url,
            status=response.status,
        )
        return result

    # Board-level short circuit: identical bytes means nothing to parse.
    if company.board_hash == response.content_hash:
        log.debug("board_unchanged", company=seed.name)
        result.unchanged = True
        return result

    extracted = extractor.parse(response.text, seed.slug)
    # One timestamp for both halves of the write. `_close_missing` decides
    # what is gone by comparing against exactly the stamp `_store` wrote, so
    # they cannot be allowed to read the clock separately.
    seen_at = datetime.now(UTC)
    stored = await _store(session, company, extracted, now=seen_at)
    result.new_postings = len(stored.new)
    result.updated_postings = len(stored.updated)
    result.reopened_postings = len(stored.reopened)
    result.changed_posting_ids = stored.changed
    result.closed_postings, result.suspect_parse = await _close_missing(
        session, company, extracted, seen_at=seen_at
    )

    # A suspect parse deliberately does not record the hash. Recording it
    # would make the next cycle short-circuit on "unchanged" and the warning
    # would never be raised again — the failure would go quiet, which is the
    # thing this is here to prevent.
    if not result.suspect_parse:
        company.board_hash = response.content_hash

    await session.flush()
    return result


async def crawl_company(
    session: AsyncSession,
    seed: CompanySeed,
    fetcher: PoliteFetcher,
    *,
    force: bool = False,
    on_result: ResultHook | None = None,
) -> CompanyResult:
    """Poll one company, and hand the outcome to `on_result` if given.

    The hook is the seam through which crawl *state* is written without this
    module knowing that such a thing exists. `packages.crawler.runs` imports
    `CompanyResult` from here; if the recording were called from here the two
    would import each other, and the rule that survives refactors is the one
    the import graph enforces rather than the one a comment asks for.

    It is handed the `Company` row rather than the seed because the state it
    writes is keyed on that row — and `None` when there is no row, which
    happens when the seed names an ATS we have no extractor for and nothing
    was ever upserted.
    """
    result = await _poll_company(session, seed, fetcher, force=force)
    if on_result is not None:
        company = await session.scalar(select(Company).where(Company.name == seed.name))
        await on_result(company, result)
    return result


async def crawl_all(
    session: AsyncSession,
    seeds: list[CompanySeed],
    fetcher: PoliteFetcher,
    *,
    force: bool = False,
    on_result: ResultHook | None = None,
    report: CrawlReport | None = None,
) -> CrawlReport:
    """Run a full cycle over the registry. Does not commit.

    A caller may pass the `report` in rather than take the returned one. That
    looks redundant until the cycle raises partway: the report built here is
    lost with the stack frame, so a caller recording the run would write zeros
    over a cycle that had in fact polled most of the registry.
    """
    report = report if report is not None else CrawlReport()
    for seed in seeds:
        report.results.append(
            await crawl_company(session, seed, fetcher, force=force, on_result=on_result)
        )
    log.info("crawl_cycle_complete", summary=report.summary())
    return report
