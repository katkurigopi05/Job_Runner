"""Company name in, job board out.

Distinct from `resolve.py`, which takes an aggregator's link to a posting
and finds the employer's real form behind it. This starts from nothing but
a name.

`POST /detect` classifies a URL you already have. This answers the question
the owner actually asks: *here are 400 companies in the Bay Area — which of
them are hiring?* Nothing in the project could do that. The registry had to be
written by hand, one careers URL at a time, which is why it sat at 29 entries
for so long.

**How a name becomes a board.** Every ATS this project crawls addresses a
company by a slug in a URL — `boards-api.greenhouse.io/v1/boards/{slug}/jobs`
and so on. So a name is turned into a handful of plausible slugs, each is
tried against each vendor, and a company *resolves* when a board answers and
lists at least one posting. Nothing is inferred: a board either responded with
jobs or it did not.

**Why "at least one posting" and not "the board exists".** Greenhouse answers
200 with an empty list for slugs that were never anyone's. Treating existence
as success fills the registry with boards that parse to nothing on every cycle
forever, which is precisely the failure `make validate-seeds` was written to
clean up after — 21 of the original 50 entries turned out to be exactly that.

**Politeness is not optional and not reimplemented here.** Every request goes
through `PoliteFetcher`, so robots.txt and the per-host floor apply the same
as they do in a crawl. That is also the honest constraint on this: the four
ATS API hosts sit at the 2s shared floor (§2.6 as amended), and probing is
serialized per host. Four hundred names against three vendors is roughly forty
minutes of wall clock, and there is no version of this that is faster and
still within the rules.

The slug-validation idea, and the observation that a board with zero jobs is
not a resolution, are both from `santifer/career-ops`'s `discover-ats.mjs`
(MIT). See `docs/REFERENCE.md` §7.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import structlog

from packages.crawler.extract import extractor_for
from packages.crawler.fetch import Blocked, PoliteFetcher

log = structlog.get_logger(__name__)

#: Characters permitted in a slug that will be interpolated into a URL. A
#: company name arrives from a CSV the owner did not necessarily write, so it
#: is untrusted input reaching a URL — the one place where "it is only a job
#: board" stops being a good enough reason to skip validation.
SLUG_RE = re.compile(r"^[A-Za-z0-9._-]+$")

#: Probed in this order. Greenhouse first because it is the largest and the
#: one whose fill path is proven here; Workable last because its widget
#: endpoint answers 200 with near-empty bodies for unknown accounts, so it
#: produces the most false starts.
VENDORS: tuple[str, ...] = ("greenhouse", "lever", "ashby", "workable")

#: Suffixes that are part of a legal name and never part of a board slug.
_NOISE = (
    "inc",
    "inc.",
    "llc",
    "ltd",
    "ltd.",
    "corp",
    "corp.",
    "corporation",
    "co",
    "co.",
    "company",
    "the",
    "labs",
    "technologies",
    "technology",
)


def slug_candidates(name: str) -> list[str]:
    """Plausible board slugs for a company name, best guess first.

    Ashby slugs are case-sensitive (`AlephAlpha`, `DeepL`), so the original
    casing is kept as a candidate rather than lowercasing everything and
    quietly missing those boards.
    """
    cleaned = re.sub(r"[^\w\s.-]", " ", name).strip()
    if not cleaned:
        return []

    words = [w for w in cleaned.split() if w]
    meaningful = [w for w in words if w.lower().strip(".") not in _NOISE] or words

    lowered = [w.lower() for w in meaningful]
    candidates = [
        "".join(lowered),  # "acmecorp"
        "-".join(lowered),  # "acme-corp"
        lowered[0],  # "acme"
        "".join(meaningful),  # "AcmeCorp" — Ashby is case-sensitive
    ]

    seen: list[str] = []
    for candidate in candidates:
        stripped = candidate.strip("-.")
        if stripped and SLUG_RE.match(stripped) and stripped not in seen:
            seen.append(stripped)
    return seen


@dataclass(frozen=True)
class Resolved:
    """A company whose board answered with at least one open posting."""

    name: str
    ats: str
    slug: str
    board_url: str
    open_jobs: int


@dataclass
class ResolveReport:
    resolved: list[Resolved] = field(default_factory=list)
    #: Names nothing answered for, with why. Reported rather than dropped: a
    #: company with a bespoke careers page is a real company we simply cannot
    #: reach yet, and silently losing it from a 400-row CSV hides that.
    unresolved: list[tuple[str, str]] = field(default_factory=list)
    #: Requests refused by robots.txt, kept apart from "not found" because
    #: they mean something entirely different about the company.
    blocked: list[tuple[str, str]] = field(default_factory=list)
    probes: int = 0

    def summary(self) -> str:
        return (
            f"{len(self.resolved)} resolved, {len(self.unresolved)} not found, "
            f"{len(self.blocked)} blocked, {self.probes} probes"
        )


async def _probe(
    fetcher: PoliteFetcher, vendor: str, slug: str
) -> tuple[int | None, str, str | None]:
    """Try one vendor and slug. Returns (job count, url, blocked reason)."""
    extractor = extractor_for(vendor)
    if extractor is None:
        return None, "", None

    url = extractor.board_url(slug)
    try:
        response = await fetcher.fetch(url)
    except Blocked as exc:
        return None, url, str(exc)
    except Exception as exc:  # noqa: BLE001 - one bad host must not stop the sweep
        log.debug("probe_failed", vendor=vendor, slug=slug, error=type(exc).__name__)
        return None, url, None

    if not response.ok:
        return None, url, None

    # Parsing rather than trusting the status: a 200 with an empty list is the
    # common answer for a slug that was never anyone's.
    try:
        postings = extractor.parse(response.text, slug)
    except Exception:  # noqa: BLE001 - a body we cannot parse is not a board
        return None, url, None

    return len(postings), url, None


async def resolve_one(
    name: str,
    fetcher: PoliteFetcher,
    *,
    vendors: tuple[str, ...] = VENDORS,
) -> Resolved | tuple[str, str]:
    """Find one company's board. Returns a Resolved, or (name, reason)."""
    candidates = slug_candidates(name)
    if not candidates:
        return name, "no usable slug could be derived from the name"

    blocked_reason: str | None = None

    for vendor in vendors:
        for slug in candidates:
            count, url, blocked = await _probe(fetcher, vendor, slug)
            if blocked:
                blocked_reason = blocked
                continue
            if count:
                return Resolved(name=name, ats=vendor, slug=slug, board_url=url, open_jobs=count)

    if blocked_reason:
        return name, f"blocked: {blocked_reason}"
    return name, "no board found on any supported ATS"


async def resolve_all(
    names: list[str],
    fetcher: PoliteFetcher | None = None,
    *,
    vendors: tuple[str, ...] = VENDORS,
    on_result: object = None,
) -> ResolveReport:
    """Resolve a list of company names.

    Sequential on purpose. The rate limiter is per host and every candidate
    for a given vendor hits the same host, so concurrency would spend its time
    queued behind the same 2s floor while making the traffic look burstier to
    the far end.
    """
    active = fetcher or PoliteFetcher()
    report = ResolveReport()

    for name in names:
        stripped = name.strip()
        if not stripped:
            continue
        outcome = await resolve_one(stripped, active, vendors=vendors)
        report.probes += 1
        if isinstance(outcome, Resolved):
            report.resolved.append(outcome)
        elif outcome[1].startswith("blocked:"):
            report.blocked.append(outcome)
        else:
            report.unresolved.append(outcome)

        if callable(on_result):
            on_result(stripped, outcome)

    log.info("resolve_complete", summary=report.summary())
    return report
