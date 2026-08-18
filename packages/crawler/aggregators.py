"""Job-board aggregators — breadth the company registry cannot reach.

The registry in `seeds/companies.yaml` answers "what is new at the companies
the owner chose". That is the right question most of the time, and it has one
structural blind spot: it can never surface a role at a company nobody
thought to list.

Aggregators answer the other question. One request returns hundreds of
postings across companies we have never heard of, which is a different
economics entirely from one request per company — and it is what makes
"search everywhere" affordable at all.

## What is allowed in here

Free endpoints only, and no key where possible. §11 bars paid APIs, and a
free tier that bills on overage is a paid API with a grace period. Every
source below is public and unmetered at this volume.

## Named source failures

A source that returns nothing says why — quota, auth, timeout, a shape it did
not recognise. An empty list and a broken source look identical from the
outside and call for opposite responses, and the difference is invisible
unless somebody records it. This is the same reasoning behind
`CrawlReport.suspect`; the crawler learned it the expensive way.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Protocol

import structlog

from packages.crawler.extract import ExtractedPosting, posting_hash, strip_html
from packages.crawler.fetch import Blocked, PoliteFetcher

log = structlog.get_logger(__name__)

#: One aggregator page is already hundreds of postings. Past this we are
#: importing a job board, not scouting.
DEFAULT_LIMIT = 500


@dataclass
class AggregatorPosting:
    """A posting from an aggregator, before it is attached to a company."""

    posting: ExtractedPosting
    company_name: str
    source: str
    #: The employer's own site, when the aggregator gives one. This is the
    #: thread ATS resolution pulls on.
    company_url: str | None = None


@dataclass
class SourceResult:
    """What one source returned, and why, if it returned nothing."""

    source: str
    postings: list[AggregatorPosting] = field(default_factory=list)
    #: Populated whenever the source produced nothing usable. Never left None
    #: on an empty result — an unexplained zero is the failure this guards.
    failure: str | None = None

    @property
    def ok(self) -> bool:
        return self.failure is None


class AggregatorSource(Protocol):
    name: str
    url: str

    def parse(self, body: str) -> list[AggregatorPosting]: ...


def _posting(
    *,
    source: str,
    external_id: str,
    url: str,
    title: str | None,
    location: str | None,
    description: str | None,
    company_name: str,
    company_url: str | None = None,
) -> AggregatorPosting:
    extracted = ExtractedPosting(
        external_id=f"{source}:{external_id}",
        url=url,
        title=title or None,
        location=location or None,
        description_raw=strip_html(description) or None,
        # Unknown until resolution says otherwise. An aggregator posting is a
        # lead, and calling it applyable before we have found a form would
        # put it in the queue for a pipeline that cannot finish it.
        ats_type="unknown",
    )
    extracted.content_hash = posting_hash(extracted)
    return AggregatorPosting(
        posting=extracted, company_name=company_name, source=source, company_url=company_url
    )


class RemotiveSource:
    """Remotive's public feed. Remote roles, no key, no quota."""

    name = "remotive"
    url = "https://remotive.com/api/remote-jobs"

    def parse(self, body: str) -> list[AggregatorPosting]:
        payload = json.loads(body)
        jobs: list[dict[str, Any]] = payload.get("jobs") or []
        out = []
        for job in jobs:
            if not isinstance(job, dict) or not job.get("id"):
                continue
            out.append(
                _posting(
                    source=self.name,
                    external_id=str(job["id"]),
                    url=job.get("url") or "",
                    title=job.get("title"),
                    location=job.get("candidate_required_location"),
                    description=job.get("description"),
                    company_name=job.get("company_name") or "unknown",
                    # Remotive publishes no employer site, so resolution has
                    # only the posting URL to work with here.
                )
            )
        return out


class ArbeitnowSource:
    """Arbeitnow's public board. No key, and not remote-only."""

    name = "arbeitnow"
    url = "https://www.arbeitnow.com/api/job-board-api"

    def parse(self, body: str) -> list[AggregatorPosting]:
        payload = json.loads(body)
        jobs: list[dict[str, Any]] = payload.get("data") or []
        out = []
        for job in jobs:
            if not isinstance(job, dict):
                continue
            slug = job.get("slug")
            if not slug:
                continue
            out.append(
                _posting(
                    source=self.name,
                    external_id=str(slug),
                    url=job.get("url") or "",
                    title=job.get("title"),
                    location=job.get("location"),
                    description=job.get("description"),
                    company_name=job.get("company_name") or "unknown",
                )
            )
        return out


class RemoteOkSource:
    """RemoteOK's public feed.

    The first element of the array is a legal notice, not a job. Treating it
    as one would put an entry titled `None` at the top of every match feed.
    """

    name = "remoteok"
    url = "https://remoteok.com/api"

    def parse(self, body: str) -> list[AggregatorPosting]:
        payload = json.loads(body)
        if not isinstance(payload, list):
            raise ValueError("expected a list")
        out = []
        for job in payload:
            if not isinstance(job, dict) or "legal" in job:
                continue
            job_id = job.get("id") or job.get("slug")
            if not job_id:
                continue
            out.append(
                _posting(
                    source=self.name,
                    external_id=str(job_id),
                    url=job.get("url") or job.get("apply_url") or "",
                    title=job.get("position") or job.get("title"),
                    location=job.get("location"),
                    description=job.get("description"),
                    company_name=job.get("company") or "unknown",
                    company_url=job.get("company_url") or None,
                )
            )
        return out


SOURCES: tuple[AggregatorSource, ...] = (
    RemotiveSource(),
    ArbeitnowSource(),
    RemoteOkSource(),
)


async def fetch_source(
    source: AggregatorSource, fetcher: PoliteFetcher, *, limit: int = DEFAULT_LIMIT
) -> SourceResult:
    """Fetch and parse one source. Never raises — failures are the report."""
    try:
        response = await fetcher.fetch(source.url)
    except Blocked as exc:
        return SourceResult(source.name, failure=f"blocked: {exc}")
    except Exception as exc:  # noqa: BLE001 - one bad source must not end the run
        return SourceResult(source.name, failure=f"fetch failed: {type(exc).__name__}")

    if not response.ok:
        detail = "rate limited" if response.status == 429 else "http error"
        return SourceResult(source.name, failure=f"{detail}: HTTP {response.status}")

    try:
        postings = source.parse(response.text)
    except (json.JSONDecodeError, ValueError, TypeError, KeyError) as exc:
        # A 200 we cannot read is a changed API, not an empty board. Same
        # distinction the crawler draws, for the same reason.
        return SourceResult(source.name, failure=f"unreadable payload: {type(exc).__name__}")

    if not postings:
        return SourceResult(source.name, failure="parsed cleanly but returned no postings")

    log.info("aggregator_fetched", source=source.name, postings=len(postings))
    return SourceResult(source.name, postings=postings[:limit])


async def fetch_all(
    fetcher: PoliteFetcher,
    sources: tuple[AggregatorSource, ...] = SOURCES,
    *,
    limit: int = DEFAULT_LIMIT,
) -> list[SourceResult]:
    """Fetch every source. Each reports its own outcome."""
    results = [await fetch_source(source, fetcher, limit=limit) for source in sources]
    for result in results:
        if not result.ok:
            log.warning("aggregator_failed", source=result.source, reason=result.failure)
    return results
