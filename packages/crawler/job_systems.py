"""Job systems a careers page can be built on that discovery cannot read yet.

Discovery verifies a company only on Greenhouse, Lever, Ashby or Workable
(`find_boards.VENDORS`). A careers page on anything else was recorded as
"names no supported board", which is true and says nothing about what to do.
Naming the system is what turns a failure into a list of readers worth
writing: on a 40-company sample of the owner's failed sheet on 2026-10-19,
five careers pages were on Workday, BambooHR (two), Paylocity and
SuccessFactors.

A system is recognised only by a link to its own host, never by its name in
prose: "we moved payroll off Workday" names no board. Hosts are matched as a
whole label suffix, so `myworkdayjobs.com.evil.net` is not Workday.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from urllib.parse import urlparse

#: Host suffix -> the system's name. A suffix matches the host itself or any
#: subdomain of it. Ordered most specific first where two could overlap.
_SYSTEMS: tuple[tuple[str, str], ...] = (
    ("myworkdayjobs.com", "Workday"),
    ("myworkdaysite.com", "Workday"),
    ("smartrecruiters.com", "SmartRecruiters"),
    ("icims.com", "iCIMS"),
    ("jobvite.com", "Jobvite"),
    ("bamboohr.com", "BambooHR"),
    ("ats.rippling.com", "Rippling"),
    # Its job-board embed script (`/ats/embeds/...`), seen on a careers page.
    ("static-assets.ripplingcdn.com", "Rippling"),
    ("jobs.gem.com", "Gem"),
    ("applytojob.com", "JazzHR"),
    ("recruitee.com", "Recruitee"),
    ("teamtailor.com", "Teamtailor"),
    # Its jobs widget's script host.
    ("teamtailor-cdn.com", "Teamtailor"),
    ("jobs.personio.de", "Personio"),
    ("jobs.personio.com", "Personio"),
    ("breezy.hr", "Breezy"),
    ("pinpointhq.com", "Pinpoint"),
    ("app.dover.com", "Dover"),
    ("recruiting.paylocity.com", "Paylocity"),
    ("recruiting.ultipro.com", "UKG"),
    ("workforcenow.adp.com", "ADP"),
    ("successfactors.com", "SuccessFactors"),
    ("successfactors.eu", "SuccessFactors"),
    ("jobs.sap.com", "SuccessFactors"),
    ("taleo.net", "Taleo"),
    ("oraclecloud.com", "Oracle Recruiting"),
    ("eightfold.ai", "Eightfold"),
    ("trinethire.com", "TriNet"),
    ("zohorecruit.com", "Zoho Recruit"),
    ("polymer.co", "Polymer"),
    ("getro.com", "Getro"),
    ("wellfound.com", "Wellfound"),
)

_URL_RE = re.compile(r"""https?://[^\s"'<>\\)\]]+""", re.I)


def system_for_host(host: str) -> str | None:
    """The job system a host belongs to, or None."""
    host = host.lower().rstrip(".")
    for suffix, name in _SYSTEMS:
        if host == suffix or host.endswith("." + suffix):
            return name
    return None


#: Systems that host one job board for a network of companies — a venture
#: firm's portfolio, an accelerator's alumni — rather than for one employer.
#: A supported board named on such a page belongs to some company in the
#: network, not to the company whose site linked it: GGV Capital's careers
#: link went to a Getro board, and discovery took Cyderes's Lever board for
#: GGV's own (2026-10-19, `find_boards._read_page`).
NETWORK_BOARDS = frozenset({"Getro"})


def _systems_linked(html: str) -> Iterator[tuple[str, str]]:
    """`(name, url)` for each link in `html` to a job system, in document order."""
    for match in _URL_RE.finditer(html):
        url = match.group(0).rstrip("\".,;:'")
        try:
            host = urlparse(url).hostname or ""
        except ValueError:
            continue
        name = system_for_host(host)
        if name:
            yield name, url


def other_job_system(html: str) -> tuple[str, str] | None:
    """`(name, url)` for the first link in `html` to a job system we cannot read."""
    return next(_systems_linked(html), None)


def network_board(html: str) -> str | None:
    """The multi-company job-board system a page is built on, if it is one."""
    return next((name for name, _ in _systems_linked(html) if name in NETWORK_BOARDS), None)


__all__ = ["NETWORK_BOARDS", "network_board", "other_job_system", "system_for_host"]
