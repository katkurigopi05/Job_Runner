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


# --- and the employer can still bounce us off it --------------------------
#
# Tested at `_check_still_ours` rather than through a server redirect:
# Playwright does not re-route a redirect produced by `route.fulfill`, so the
# follow-up request leaves the test and hits the real board. A test that
# reaches the network to prove an offline rule is a worse test than one that
# calls the rule.


def test_a_board_that_redirects_to_the_employers_own_site_is_unsupported() -> None:
    """Stripe hosts the application itself, for every posting it has.

    `job-boards.greenhouse.io/stripe/jobs/8172487` answers 200 at
    `stripe.com/careers/listing/abuse-investigator/8172487?gh_jid=8172487`,
    which carries no form at all — `form` count 0. Measured, not assumed.

    The landing page still names the job, which is what says the employer
    hosts it rather than that it is gone.
    """
    import pytest

    from packages.ats.base import UnsupportedSiteError
    from packages.ats.navigate import _check_still_ours
    from packages.ats.registry import adapter_for

    adapter = adapter_for("https://job-boards.greenhouse.io/stripe/jobs/8172487")

    with pytest.raises(UnsupportedSiteError):
        _check_still_ours(
            adapter,
            "https://stripe.com/careers/listing/abuse-investigator/8172487?gh_jid=8172487",
            "8172487",
        )


def test_a_redirect_to_a_careers_index_is_a_closed_posting() -> None:
    """Cloudflare's board works. This one role was taken down.

    `job-boards.greenhouse.io/cloudflare/jobs/7168950` lands on
    `cloudflare.com/careers/#open-roles` — the careers index, naming no job.
    Reporting that as `unsupported_site` would libel a board we poll
    successfully, and `unsupported_site` is what decides whether a company
    stays in the registry.
    """
    import pytest

    from packages.ats.base import PostingGone
    from packages.ats.navigate import _check_still_ours
    from packages.ats.registry import adapter_for

    adapter = adapter_for("https://job-boards.greenhouse.io/cloudflare/jobs/7168950")

    with pytest.raises(PostingGone):
        _check_still_ours(adapter, "https://www.cloudflare.com/careers/#open-roles", "7168950")


def test_a_redirect_with_no_job_id_to_compare_is_a_closed_posting() -> None:
    """Nothing to match on is not evidence the employer hosts it."""
    import pytest

    from packages.ats.base import PostingGone
    from packages.ats.navigate import _check_still_ours
    from packages.ats.registry import adapter_for

    adapter = adapter_for("https://job-boards.greenhouse.io/acme/jobs/4012345")

    with pytest.raises(PostingGone):
        _check_still_ours(adapter, "https://acme.com/careers", None)


def test_a_redirect_within_the_same_ats_is_fine() -> None:
    """`boards.` to `job-boards.` is Greenhouse's own move, not an exit."""
    from packages.ats.navigate import _check_still_ours
    from packages.ats.registry import adapter_for

    adapter = adapter_for("https://boards.greenhouse.io/acme/jobs/4012345")

    _check_still_ours(adapter, "https://job-boards.greenhouse.io/acme/jobs/4012345", "4012345")


def test_an_apply_route_is_still_ours() -> None:
    """Lever's `/apply` is where we deliberately sent ourselves."""
    from packages.ats.navigate import _check_still_ours
    from packages.ats.registry import adapter_for

    posting = "https://jobs.lever.co/qonto/8a0f1b2c-3d4e-5f60-7182-93a4b5c6d7e8"

    _check_still_ours(adapter_for(posting), f"{posting}/apply", None)
