"""A crawled posting URL has to be one an adapter can act on.

Greenhouse's board API returns `absolute_url`, which is whatever the employer
configured. Most companies leave it pointing at the board. Stripe does not: its
635 postings come back as `https://stripe.com/jobs/search?gh_jid=<id>`, a page
on Stripe's own site. `detect_ats` returns None for it, the worker routes by
URL, and so every one of those postings was stored as something no adapter
claims — `unsupported_site` for a board we crawl first-hand.

The company slug and the job id are both in hand at extraction time, so the
canonical board URL is always constructible. Nothing here fetches.
"""

from __future__ import annotations

import json

from packages.ats.registry import detect_ats
from packages.crawler.extract import GreenhouseExtractor


def _board(absolute_url: str | None) -> str:
    job: dict[str, object] = {
        "id": 4012345,
        "title": "Backend Engineer",
        "location": {"name": "San Francisco, CA"},
        "content": "<p>We are hiring.</p>",
    }
    if absolute_url is not None:
        job["absolute_url"] = absolute_url
    return json.dumps({"jobs": [job]})


def test_a_board_url_is_kept_as_the_employer_wrote_it() -> None:
    """The common case. Nothing is rewritten that already works."""
    body = _board("https://job-boards.greenhouse.io/acme/jobs/4012345")

    (posting,) = GreenhouseExtractor().parse(body, "acme")

    assert posting.url == "https://job-boards.greenhouse.io/acme/jobs/4012345"


def test_a_company_site_url_is_resolved_to_the_board() -> None:
    """Stripe's shape. The posting is real; the URL is not one we can drive."""
    body = _board("https://stripe.com/jobs/search?gh_jid=4012345")

    (posting,) = GreenhouseExtractor().parse(body, "stripe")

    assert posting.url == "https://job-boards.greenhouse.io/stripe/jobs/4012345"
    assert detect_ats(posting.url) == "greenhouse"


def test_a_missing_url_falls_back_to_the_board() -> None:
    body = _board(None)

    (posting,) = GreenhouseExtractor().parse(body, "acme")

    assert detect_ats(posting.url) == "greenhouse"


def test_every_extracted_url_reaches_an_adapter() -> None:
    """The property that matters, over the shapes a board actually returns."""
    urls = [
        "https://boards.greenhouse.io/acme/jobs/4012345",
        "https://job-boards.greenhouse.io/acme/jobs/4012345",
        "https://stripe.com/jobs/search?gh_jid=4012345",
        "https://www.example.com/careers/openings/backend-engineer",
        "",
    ]
    for url in urls:
        (posting,) = GreenhouseExtractor().parse(_board(url), "acme")
        assert detect_ats(posting.url) == "greenhouse", url
