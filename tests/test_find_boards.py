"""Company name to job board.

The failure this guards against is not "we missed a company" — that is
visible, and the unresolved list names every one. It is the opposite: adding a
board that does not exist. Greenhouse answers **200 with an empty list** for
slugs that were never anyone's, so treating a response as a resolution fills
the registry with entries that parse to nothing on every cycle forever. That
is exactly what `make validate-seeds` had to clean up after, when 21 of the
original 50 seeds turned out to be dead boards.

And because the names come from a CSV the owner did not necessarily write,
they are untrusted input reaching a URL.
"""

from __future__ import annotations

import httpx
import pytest

from packages.crawler.fetch import PoliteFetcher
from packages.crawler.find_boards import (
    SLUG_RE,
    Resolved,
    resolve_all,
    resolve_one,
    slug_candidates,
)

# --------------------------------------------------------------------------
# Slugs
# --------------------------------------------------------------------------


def test_legal_suffixes_do_not_become_part_of_the_slug() -> None:
    """No board is called `acme-inc`. The company is `acme`."""
    candidates = slug_candidates("Acme Inc.")

    assert "acme" in candidates
    assert not any("inc" in candidate for candidate in candidates)


def test_multi_word_names_try_both_joined_and_hyphenated() -> None:
    candidates = slug_candidates("Scale AI")

    assert "scaleai" in candidates
    assert "scale-ai" in candidates


def test_original_casing_survives_as_a_candidate() -> None:
    """Ashby slugs are case-sensitive — `AlephAlpha`, `DeepL` are real boards.

    Lowercasing everything would silently miss every one of them, and the
    company would land in the unresolved list looking like it does not hire.
    """
    assert "DeepL" in slug_candidates("DeepL")


def test_punctuation_never_reaches_a_url() -> None:
    """A CSV is untrusted input, and these slugs are interpolated into URLs."""
    for name in ("Acme/../../etc", "Acme; DROP TABLE", "Acme?x=1&y=2", "../../root"):
        for candidate in slug_candidates(name):
            assert SLUG_RE.match(candidate), f"{candidate!r} from {name!r}"


def test_a_name_with_nothing_usable_yields_nothing() -> None:
    assert slug_candidates("   ") == []
    assert slug_candidates("!!!") == []


def test_candidates_are_deduplicated() -> None:
    candidates = slug_candidates("Notion")

    assert len(candidates) == len(set(candidates))


# --------------------------------------------------------------------------
# Resolution
# --------------------------------------------------------------------------


def _transport(bodies: dict[str, str]) -> httpx.MockTransport:
    """Answer robots.txt permissively; serve `bodies` by URL substring."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow:")
        for fragment, body in bodies.items():
            if fragment in str(request.url):
                return httpx.Response(200, text=body)
        return httpx.Response(404, text="")

    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_an_empty_board_is_not_a_resolution() -> None:
    """Greenhouse answers 200 with `{"jobs": []}` for slugs nobody owns.

    This is the whole point of the module. A board that exists and lists
    nothing is indistinguishable, on every later crawl, from a board we are
    failing to parse — and it costs a request an hour forever.
    """
    fetcher = PoliteFetcher(transport=_transport({"greenhouse": '{"jobs": []}'}))

    outcome = await resolve_one("Ghost Company", fetcher)

    assert not isinstance(outcome, Resolved)
    assert "no board found" in outcome[1]


@pytest.mark.asyncio
async def test_a_board_with_postings_resolves() -> None:
    body = '{"jobs": [{"id": 1, "title": "Engineer", "absolute_url": "https://x/1"}]}'
    fetcher = PoliteFetcher(transport=_transport({"greenhouse": body}))

    outcome = await resolve_one("Acme", fetcher)

    assert isinstance(outcome, Resolved)
    assert outcome.ats == "greenhouse"
    assert outcome.slug == "acme"
    assert outcome.open_jobs == 1


@pytest.mark.asyncio
async def test_a_body_that_will_not_parse_is_not_a_resolution() -> None:
    """A 200 proves a server answered, not that it answered with a board."""
    fetcher = PoliteFetcher(transport=_transport({"greenhouse": "<html>not json</html>"}))

    outcome = await resolve_one("Acme", fetcher)

    assert not isinstance(outcome, Resolved)


@pytest.mark.asyncio
async def test_robots_refusal_is_reported_apart_from_not_found() -> None:
    """Being told no means something different from not existing.

    Folding the two together would hide a whole ATS going off-limits behind a
    list that reads as "these companies aren't hiring".
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /")
        return httpx.Response(200, text='{"jobs": []}')

    report = await resolve_all(["Acme"], PoliteFetcher(transport=httpx.MockTransport(handler)))

    assert report.blocked
    assert report.unresolved == []


@pytest.mark.asyncio
async def test_blank_rows_in_the_csv_are_skipped_silently() -> None:
    report = await resolve_all(["", "   "], PoliteFetcher(transport=_transport({})))

    assert report.probes == 0
    assert report.summary().startswith("0 resolved")


# --------------------------------------------------------------------------
# Resolving from a URL the company list supplied
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_board_url_is_read_not_guessed() -> None:
    """The slug is in the URL. Guessing from the name would be choosing worse.

    Verified live against `jobs.ashbyhq.com/ramp`, which name-guessing also
    finds — and against `linear.app/careers`, whose slug is `Linear` with a
    capital L, which name-guessing does not.
    """
    body = '{"jobs": [{"id": 1, "title": "Engineer", "absolute_url": "https://x/1"}]}'
    fetcher = PoliteFetcher(transport=_transport({"greenhouse": body}))

    outcome = await resolve_one(
        "Whatever They Are Called", fetcher, url="https://job-boards.greenhouse.io/acmeco"
    )

    assert isinstance(outcome, Resolved)
    assert outcome.slug == "acmeco"
    assert outcome.ats == "greenhouse"


@pytest.mark.asyncio
async def test_a_careers_page_is_read_for_the_board_behind_it() -> None:
    """`vercel.com/careers` is a wrapper around Greenhouse. Verified live."""
    page = "<html><a href='https://boards.greenhouse.io/acmeco/jobs/4001'>Apply</a></html>"
    body = '{"jobs": [{"id": 1, "title": "Engineer", "absolute_url": "https://x/1"}]}'
    fetcher = PoliteFetcher(
        transport=_transport({"careers": page, "boards-api.greenhouse.io": body})
    )

    outcome = await resolve_one("Acme", fetcher, url="https://acme.com/careers")

    assert isinstance(outcome, Resolved)
    assert outcome.slug == "acmeco"


@pytest.mark.asyncio
async def test_a_named_board_with_no_roles_does_not_fall_back_to_guessing() -> None:
    """The company's own page named this board. Empty means not hiring.

    Falling through to the name-guesser here would go looking for a *different*
    company that happens to share a word, and report its jobs under this
    company's name. A wrong hit is worse than an honest miss.
    """
    fetcher = PoliteFetcher(transport=_transport({"greenhouse": '{"jobs": []}'}))

    outcome = await resolve_one("Acme", fetcher, url="https://job-boards.greenhouse.io/acmeco")

    assert not isinstance(outcome, Resolved)
    assert "lists no open roles" in outcome[1]


@pytest.mark.asyncio
async def test_a_dead_url_falls_back_to_the_name() -> None:
    """A URL that yields nothing is not evidence, so the guess is still worth
    making. Only a URL that *answered* suppresses the fallback."""
    body = '{"jobs": [{"id": 1, "title": "Engineer", "absolute_url": "https://x/1"}]}'
    fetcher = PoliteFetcher(transport=_transport({"greenhouse": body}))

    outcome = await resolve_one("Acme", fetcher, url="https://dead.example.com/careers")

    assert isinstance(outcome, Resolved)
    assert outcome.slug == "acme"
