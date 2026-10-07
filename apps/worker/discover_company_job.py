"""Find one company's board, as its own queue task.

This is the handoff the import path was missing. `scripts/find_boards.py` could
already resolve a board from a company's own URL, but only as a manual sweep
that wrote nothing until it had probed every company and kept no record of what
it had tried — so an interrupted run started over, and a board found in minute
one was unusable until hour two.

Three properties make it a task rather than a loop:

**Evidence first.** `resolve_one` prefers a supplied URL and, when that yields a
board, tries nothing else. The fallback — guessing a slug from the company name
and probing each vendor — runs only when the evidence produced nothing, so a
company with a usable website costs one or two requests to its own host instead
of four to the contended ATS hosts.

**Published immediately, and atomically.** A verification and the crawl task it
earns are written in the *same transaction*, so there is no window where a
company is verified and nothing is scheduled. A crash before the commit loses
the attempt and the queue retries it; a crash after it loses nothing. Dispatch
does not have to come round again first.

**Resumable.** The outcome, the evidence, the attempt count and the next attempt
are all on the row. Restarting re-reads them; a company already verified is
returned from immediately rather than re-probed.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

import structlog
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.config import get_settings
from packages.core.enums import SourceStatus
from packages.core.models import Company
from packages.core.queue import ClaimedTask, enqueue
from packages.crawler.board_directory import confirms
from packages.crawler.board_directory import load_default as load_directory
from packages.crawler.company_csv import is_search_url
from packages.crawler.defer import defer_if_host_busy
from packages.crawler.dispatch import CRAWL_COMPANY_TASK_KIND
from packages.crawler.duplicate_boards import holder_of, set_aside
from packages.crawler.fetch import PoliteFetcher, build_fetcher
from packages.crawler.find_boards import VENDORS, Resolved, fetch_board, resolve_one
from packages.crawler.runs import discovery_backoff, ensure_state

log = structlog.get_logger(__name__)

DISCOVER_COMPANY_TASK_KIND = "discover_company"

__all__ = ["DISCOVER_COMPANY_TASK_KIND", "handle_discover_company", "lead_for", "should_guess"]


class MalformedDiscoveryTask(ValueError):
    """The payload does not name a company to discover."""


def lead_for(company: Company) -> str | None:
    """The best URL to start discovery from, or None if the row has none.

    Ordered by how much it claims. A careers URL already on the row was put
    there by an import that recognised it; the supplied careers value from the
    sheet comes next; the website is the weakest and the commonest.

    A **search link is never a lead**, whichever column it came from. It is a
    recipe for finding a page, not a page, and fetching one would point the
    crawler at a search engine — which is both useless and exactly what
    `is_search_url` exists to separate from a company's own domain.
    """
    for candidate in (company.careers_url, company.supplied_career_url, company.supplied_website):
        value = (candidate or "").strip()
        if not value or not value.lower().startswith(("http://", "https://")):
            continue
        if is_search_url(value):
            continue
        return value
    return None


def should_guess(lead: str | None, policy: str) -> bool:
    """Whether to fall back to guessing a slug from the company's name.

    `no_website`, the default, guesses only when there is no site to read. On
    the owner's sheet guessing found none of the 188 companies that had one —
    their own pages had already said everything guessing could — and it was
    nearly all of the wall clock, at ~10 probes to the shared ATS hosts each.
    """
    if policy == "always":
        return True
    if policy == "never":
        return False
    return lead is None


@dataclass(frozen=True)
class DirectoryResult:
    """What the directories offered: a confirmed board, and what was turned down."""

    resolved: Resolved | None = None
    why: str | None = None
    confirmed_by: str | None = None
    #: Boards that listed postings but whose postings never named the company.
    #: Kept on the evidence: a namesake turned down is a lead the owner can
    #: check by hand, and silently dropping it would hide that it was offered.
    unconfirmed: tuple[str, ...] = ()


async def _from_directory(
    fetcher: PoliteFetcher, company: Company, lead: str | None, vendors: tuple[str, ...]
) -> DirectoryResult:
    """A board a public directory lists for this company, confirmed live.

    The directory is a proposal, never a verdict. Each candidate is fetched,
    and counts only if it lists at least one open posting — the bar
    `resolve_one` holds a guess to, since an empty 200 is usually a slug nobody
    owns — **and** `board_directory.confirms` finds a domain saying it is this
    company's. A name alone gave four namesakes in 33 on 2026-10-05.
    """
    directory = load_directory()
    if directory is None:
        return DirectoryResult()
    website = company.supplied_website or lead
    unconfirmed: list[str] = []
    for candidate in directory.candidates(company.name, website):
        if candidate.vendor not in vendors:
            continue
        postings, url, _blocked = await fetch_board(fetcher, candidate.vendor, candidate.slug)
        if not postings:
            continue
        confirmed_by = confirms(candidate, website, postings)
        if confirmed_by is None:
            unconfirmed.append(f"{candidate.vendor}/{candidate.slug}")
            log.info(
                "directory_candidate_unconfirmed",
                company=company.name,
                ats=candidate.vendor,
                slug=candidate.slug,
                why=candidate.why,
            )
            continue
        resolved = Resolved(
            name=company.name,
            ats=candidate.vendor,
            slug=candidate.slug,
            board_url=url,
            open_jobs=len(postings),
        )
        return DirectoryResult(
            resolved=resolved,
            why=candidate.why,
            confirmed_by=confirmed_by,
            unconfirmed=tuple(unconfirmed),
        )
    return DirectoryResult(unconfirmed=tuple(unconfirmed))


async def _set_aside_as_duplicate(
    session: AsyncSession, company: Company, holder: Company, outcome: Resolved, now: datetime
) -> None:
    """`company` resolved to a board `holder` already polls. No crawl is queued."""
    await set_aside(session, company, holder, ats=outcome.ats, slug=outcome.slug, now=now)
    log.info(
        "discover_company_duplicate_board",
        company=company.name,
        holder=holder.name,
        ats=outcome.ats,
        slug=outcome.slug,
    )


def _company_id(payload: dict) -> uuid.UUID:
    raw = payload.get("company_id")
    if not raw:
        raise MalformedDiscoveryTask("discover_company task has no company_id")
    try:
        return uuid.UUID(str(raw))
    except ValueError as exc:
        raise MalformedDiscoveryTask(f"company_id is not a uuid: {raw!r}") from exc


async def handle_discover_company(session: AsyncSession, claimed: ClaimedTask) -> None:
    """Resolve one company's board and, on success, schedule its first crawl."""
    payload = claimed.task.payload_json or {}
    company_id = _company_id(payload)

    company = await session.get(Company, company_id)
    if company is None:
        log.warning("discover_company_gone", company_id=str(company_id))
        return

    if company.source_status == SourceStatus.VERIFIED.value:
        # Idempotent: at-least-once delivery means this task can arrive after
        # another worker has already resolved the company. Re-probing would
        # spend the rate limiter to learn what the row already says.
        log.info("discover_company_already_verified", company=company.name)
        return

    state = await ensure_state(session, company.id)
    lead = lead_for(company)
    now = datetime.now(UTC)
    settings = get_settings()
    vendors = (
        tuple(
            v.strip()
            for v in (settings.crawler_discovery_vendors or ",".join(VENDORS)).split(",")
            if v.strip() in VENDORS
        )
        or VENDORS
    )

    outcome: Resolved | tuple[str, str]
    async with build_fetcher() as fetcher:
        # The directory goes first because its probes go to the shared ATS
        # hosts (a 2s floor) rather than the company's own (60s), and on the
        # trial it proposed 23 of the 33 boards found. A miss costs at most
        # MAX_CANDIDATES probes and falls through to the site unchanged.
        found = await _from_directory(fetcher, company, lead, vendors)
        if found.resolved is not None:
            outcome = found.resolved
        else:
            # A company's own host carries §2.6's ordinary 60s floor, and
            # discovery makes up to two requests to it (the page, then one
            # careers link). So the wait here is a minute, not two seconds,
            # and sleeping through it in a worker slot is the costliest
            # version of this problem.
            if lead:
                # Raises `TaskDeferred` if it hands the task back. The attempt
                # is then not recorded, because nothing was tried — counting a
                # limiter deferral as an attempt would back the company off as
                # though its own site had failed.
                await defer_if_host_busy(session, claimed, fetcher, lead)
            outcome = await resolve_one(
                company.name,
                fetcher,
                url=lead,
                vendors=vendors,
                guess=should_guess(lead, settings.crawler_name_guessing),
            )

    state.discovery_last_at = now
    state.discovery_attempts += 1

    # Recorded on success and failure alike: a namesake turned down beside a
    # board found on the company's own site is still worth seeing.
    unconfirmed = {"unconfirmed": list(found.unconfirmed)} if found.unconfirmed else {}

    if isinstance(outcome, Resolved):
        if found.resolved is not None:
            method, via = "discovery_from_directory", "directory"
        elif lead:
            method, via = "discovery_from_url", "url"
        else:
            method, via = "discovery_from_name", "name"
        # One board, one verified row. The owner's sheet lists a company
        # under an old name and a new one (ZEIT and Vercel), both resolve to
        # the same board, and verifying both stored every posting on it twice.
        holder = await holder_of(session, outcome.ats, outcome.slug, other_than=company.id)
        if holder is not None:
            await _set_aside_as_duplicate(session, company, holder, outcome, now)
            return
        evidence = {
            "method": method,
            **(
                {"why": found.why, "confirmed_by": found.confirmed_by}
                if found.resolved is not None
                else {}
            ),
            **unconfirmed,
            "lead": lead,
            "ats": outcome.ats,
            "slug": outcome.slug,
            "board_url": outcome.board_url,
            "open_jobs": outcome.open_jobs,
            "at": now.isoformat(),
        }
        try:
            # A savepoint, opened before the row is touched: opening one
            # flushes what is pending, so a change made first would meet the
            # index outside it and take the whole transaction down.
            async with session.begin_nested():
                company.ats_type = outcome.ats
                company.slug = outcome.slug
                company.careers_url = outcome.board_url
                company.source_status = SourceStatus.VERIFIED.value
                company.source_verified_at = now
                company.discovery_failure = None
                company.source_evidence = evidence
                # Nothing more is owed to discovery, and the board is owed a
                # first fetch now. Both written here, in the handler's
                # transaction, with the crawl task — see the module docstring
                # on why that is one write.
                state.discovery_next_at = None
                state.next_due_at = None
                await enqueue(
                    session,
                    CRAWL_COMPANY_TASK_KIND,
                    {"company_id": str(company.id), "force": True, "trigger": "discovery"},
                )
                await session.flush()
        except IntegrityError:
            # Another task verified this board between the question above and
            # this write: neither saw the other. `uq_companies_verified_board`
            # refused the second, and by now the first is there to be found.
            # The rollback expired the row; it is read again before anything
            # looks at it, or the first attribute touched is a lazy load.
            await session.refresh(company)
            holder = await holder_of(session, outcome.ats, outcome.slug, other_than=company.id)
            if holder is None:
                raise
            await _set_aside_as_duplicate(session, company, holder, outcome, now)
            return
        log.info(
            "discover_company_verified",
            company=company.name,
            ats=outcome.ats,
            slug=outcome.slug,
            open_jobs=outcome.open_jobs,
            via=via,
        )
        return

    _name, reason = outcome
    company.source_status = SourceStatus.FAILED.value
    company.discovery_failure = reason
    company.source_evidence = {
        "method": "discovery_failed",
        **unconfirmed,
        "lead": lead,
        "reason": reason,
        "attempts": state.discovery_attempts,
        "at": now.isoformat(),
    }
    state.discovery_next_at = now + discovery_backoff(state.discovery_attempts)
    await session.flush()
    log.info(
        "discover_company_unresolved",
        company=company.name,
        reason=reason,
        attempts=state.discovery_attempts,
        next_attempt=state.discovery_next_at.isoformat(),
    )
