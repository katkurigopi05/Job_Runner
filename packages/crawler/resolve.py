"""Resolve an aggregator posting to the ATS form it actually applies through.

An aggregator hands us a link to *its own* page for a job. The apply pipeline
needs the employer's real form — a Greenhouse, Lever or Ashby URL — because
that is what the adapters know how to fill.

Most of the time the thread is short. Aggregators overwhelmingly list jobs
that live on one of these three, and the link either is one already or the
page contains one. So resolution is: look at the URL, and if that says
nothing, fetch the page once and look for a supported form in it.

## What a failure means here

An unresolved posting is **kept, not dropped**. It is a real job at a real
company; we simply cannot finish the application for it. It stays in the
match feed with `ats_type` left as `unknown`, and the owner applies by hand.
Dropping it would be throwing away the answer to the question the aggregators
were added to answer.

The one thing resolution must never do is guess. Marking a posting
`greenhouse` because the page mentioned Greenhouse somewhere would send the
worker to fill a form that is not there, and the failure would surface as a
`site_error` on a real application rather than as the honest
`manual_completion_required`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import structlog

from packages.ats.registry import detect_ats
from packages.crawler.fetch import Blocked, PoliteFetcher

log = structlog.get_logger(__name__)

#: Supported-ATS URLs as they appear embedded in an employer's careers page:
#: an apply button, an iframe, a canonical link.
_EMBEDDED_ATS_RE = re.compile(
    r"https?://(?:"
    r"(?:boards|job-boards)\.greenhouse\.io/[\w.-]+/jobs/\d+"
    r"|jobs\.lever\.co/[\w.-]+/[0-9a-f-]{16,}"
    r"|jobs\.ashbyhq\.com/[\w.-]+/[0-9a-f-]{16,}"
    r")",
    re.I,
)


@dataclass
class Resolution:
    """Where a posting actually applies, if we could establish it."""

    url: str
    ats: str | None = None
    #: How we know. Useful when a resolution later turns out to be wrong.
    via: str = "unresolved"

    @property
    def applyable(self) -> bool:
        return self.ats is not None


def resolve_from_url(url: str) -> Resolution:
    """Resolution by URL shape alone. No network."""
    ats = detect_ats(url)
    if ats is not None:
        return Resolution(url=url, ats=ats, via="url")
    return Resolution(url=url)


def find_embedded(html: str) -> str | None:
    """The first supported ATS URL embedded in a page, if there is one."""
    match = _EMBEDDED_ATS_RE.search(html)
    return match.group(0) if match else None


async def resolve(
    url: str, fetcher: PoliteFetcher, *, company_url: str | None = None
) -> Resolution:
    """Resolve one posting, fetching at most two pages.

    The posting URL is tried first because it is the more specific of the
    two: an employer's careers page lists many jobs, and the one embedded
    form we find there may not be the job we came for.
    """
    direct = resolve_from_url(url)
    if direct.applyable:
        return direct

    for candidate, label in ((url, "posting"), (company_url, "company")):
        if not candidate:
            continue
        try:
            response = await fetcher.fetch(candidate)
        except Blocked as exc:
            log.info("resolve_blocked", url=candidate, reason=str(exc))
            continue
        except Exception as exc:  # noqa: BLE001 - an unresolved posting is a lead
            log.info("resolve_fetch_failed", url=candidate, error=type(exc).__name__)
            continue

        if not response.ok:
            continue

        embedded = find_embedded(response.text)
        if embedded is None:
            continue

        ats = detect_ats(embedded)
        if ats is None:
            # The pattern matched but no adapter claims it. Trust the
            # adapter, not the regex.
            continue
        return Resolution(url=embedded, ats=ats, via=label)

    return Resolution(url=url)
