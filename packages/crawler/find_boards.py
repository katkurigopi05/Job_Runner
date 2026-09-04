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

from packages.ats.registry import detect_ats
from packages.crawler.discover import slug_from_ats_url
from packages.crawler.extract import extractor_for
from packages.crawler.fetch import Blocked, PoliteFetcher
from packages.crawler.resolve import find_embedded

log = structlog.get_logger(__name__)


#: Hosts whose URLs are not evidence about anything. A real company list in
#: the wild had `google.com/search?q=site:acme.com+careers+jobs` in its careers
#: column for 3,864 of 3,869 rows — someone generated a search link per
#: company rather than finding the page. Following those would be a robots
#: violation and would learn nothing.
_NON_EVIDENCE_HOSTS = (
    "google.",
    "bing.com",
    "duckduckgo.com",
    "search.yahoo.",
    "linkedin.com",
)


def normalize_header(header: str) -> str:
    """`Company Name` and `Jobs/Careers URL` become `company_name`, `jobs_careers_url`.

    Spreadsheet headers are written for people. Matching them literally meant
    a file with `Company Name` fell through to the headerless path, which
    parsed the header row itself as a company.

    Lives here rather than in each reader because two copies drift, and
    `company_csv.py` needs the identical rule — a sheet it cannot match a
    column in is a sheet it refuses outright.
    """
    return re.sub(r"[^a-z0-9]+", "_", header.strip().lower()).strip("_")


def usable_url(url: str | None) -> str | None:
    """The URL if it could name a board, None if it is a search link."""
    if not url:
        return None
    lowered = url.strip().lower()
    if not lowered.startswith(("http://", "https://")):
        return None
    if any(host in lowered for host in _NON_EVIDENCE_HOSTS):
        return None
    return url.strip()


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


#: Board *roots*, as they appear in a company list. Deliberately separate
#: from `detect_ats` and `slug_from_ats_url`, which match a **posting** URL —
#: they need the `/jobs/{id}` tail and return None for a bare board. That is
#: right for their job (classifying a posting) and useless for this one: a
#: careers column holds `jobs.ashbyhq.com/ramp` far more often than it holds a
#: link to one specific role.
_BOARD_ROOT_RES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "greenhouse",
        re.compile(
            r"^https?://(?:www\.)?(?:job-)?boards\.greenhouse\.io/(?P<slug>[A-Za-z0-9._-]+)/?$",
            re.I,
        ),
    ),
    (
        # The embed form, which is what a company's own page usually iframes.
        "greenhouse",
        re.compile(
            r"^https?://boards\.greenhouse\.io/embed/job_board\?for=(?P<slug>[A-Za-z0-9._-]+)",
            re.I,
        ),
    ),
    (
        "lever",
        re.compile(
            r"^https?://jobs\.(?:eu\.)?lever\.co/(?P<slug>[A-Za-z0-9._-]+)/?$",
            re.I,
        ),
    ),
    (
        "ashby",
        re.compile(
            r"^https?://jobs\.ashbyhq\.com/(?P<slug>[A-Za-z0-9._-]+)/?$",
            re.I,
        ),
    ),
    (
        "workable",
        re.compile(
            r"^https?://(?:apply|jobs)\.workable\.com/(?P<slug>[A-Za-z0-9._-]+)/?$",
            re.I,
        ),
    ),
)


def board_root(url: str) -> tuple[str, str] | None:
    """`(vendor, slug)` when the URL is a board's front page."""
    for vendor, pattern in _BOARD_ROOT_RES:
        match = pattern.match(url.strip())
        if match:
            slug = match.group("slug")
            if SLUG_RE.match(slug):
                return vendor, slug
    return None


async def from_url(url: str, fetcher: PoliteFetcher) -> tuple[str, str] | None:
    """`(vendor, slug)` for a URL the owner supplied, or None.

    Two shapes arrive in a company list, and they need different handling.

    **The URL is already a board** — `job-boards.greenhouse.io/acme`. The slug
    is right there, so nothing is guessed and nothing is fetched.

    **The URL is the company's own careers page** — `acme.com/careers`. Those
    pages are overwhelmingly a wrapper around a supported ATS, with the real
    form linked or embedded, so the page is fetched once and read for one.
    `find_embedded` already does exactly this for aggregator links; the
    problem is the same one, arriving from a spreadsheet instead.

    This is strictly better than guessing a slug from the name: `acme.com`
    might be `acmecorp`, `acme-inc`, or `getacme` on Greenhouse, and only the
    page knows which.
    """
    root = board_root(url)
    if root:
        return root

    # A link to one specific posting also names the board.
    if detect_ats(url):
        slug = slug_from_ats_url(url)
        vendor = detect_ats(url)
        if slug and vendor and SLUG_RE.match(slug):
            return vendor, slug

    try:
        response = await fetcher.fetch(url)
    except Blocked as exc:
        log.info("careers_page_blocked", url=url, reason=str(exc))
        return None
    except Exception as exc:  # noqa: BLE001 - a careers page is a hint, not a dependency
        log.debug("careers_page_failed", url=url, error=type(exc).__name__)
        return None

    if not response.ok:
        return None

    embedded = find_embedded(response.text)
    if not embedded:
        return None

    vendor = detect_ats(embedded)
    slug = slug_from_ats_url(embedded)
    if vendor and slug and SLUG_RE.match(slug):
        return vendor, slug
    return None


async def resolve_one(
    name: str,
    fetcher: PoliteFetcher,
    *,
    url: str | None = None,
    vendors: tuple[str, ...] = VENDORS,
) -> Resolved | tuple[str, str]:
    """Find one company's board. Returns a Resolved, or (name, reason).

    A `url` from the company list is tried first and, when it yields a vendor
    and slug, is the only thing tried. That is the point: the URL is evidence
    and the name is a guess, so falling back to guessing after the evidence
    said something would be choosing the worse answer.

    It falls through to name-guessing only when the URL yields nothing at all
    — a dead link, a page with no ATS behind it, a robots refusal.
    """
    blocked_reason: str | None = None

    if url:
        known = await from_url(url, fetcher)
        if known:
            vendor, slug = known
            count, board_url, blocked = await _probe(fetcher, vendor, slug)
            if count:
                return Resolved(
                    name=name, ats=vendor, slug=slug, board_url=board_url, open_jobs=count
                )
            if blocked:
                blocked_reason = blocked
            # A board named by the company's own page but listing nothing is
            # the clearest "not hiring" this tool can produce. Reported as
            # such rather than sending the name-guesser looking for a
            # different company that happens to share a word.
            elif count == 0:
                return name, f"board found ({vendor}/{slug}) but it lists no open roles"

    candidates = slug_candidates(name)
    if not candidates:
        return name, "no usable slug could be derived from the name"

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
    names: list[str] | list[tuple[str, str | None]],
    fetcher: PoliteFetcher | None = None,
    *,
    vendors: tuple[str, ...] = VENDORS,
    on_result: object = None,
) -> ResolveReport:
    """Resolve a list of company names, or (name, url) pairs.

    Accepts both shapes because a company list arrives either way: a bare
    column of names, or names beside the careers URLs someone already
    collected. The pair form resolves far more of them — see `from_url`.

    Sequential on purpose. The rate limiter is per host and every candidate
    for a given vendor hits the same host, so concurrency would spend its time
    queued behind the same 2s floor while making the traffic look burstier to
    the far end.
    """
    active = fetcher or PoliteFetcher()
    report = ResolveReport()

    for entry in names:
        stripped, url = (entry, None) if isinstance(entry, str) else entry
        stripped = stripped.strip()
        if not stripped:
            continue
        outcome = await resolve_one(stripped, active, url=url, vendors=vendors)
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
