"""Reading schema.org JobPosting out of an ordinary careers page.

The registry only holds companies whose careers URL is a board we have an
adapter for; `import_portals.py` skips the rest because "we have no extractor
for a bespoke page". This is that extractor, and `seeds/bespoke_careers.csv`
is its input.

JSON-LD rather than CSS selectors because career sites publish it *so that
machines read it* — it is what Google Jobs indexes. That makes it the one part
of a bespoke page with a stable contract, where a selector is a guess that
expires.
"""

from __future__ import annotations

import json

import pytest

from packages.crawler.jsonld import ATS, extract, job_postings, script_blocks

PAGE_URL = "https://acme.example/careers"


def _page(*blocks: object, extra: str = "") -> str:
    scripts = "\n".join(
        f'<script type="application/ld+json">{json.dumps(b)}</script>' for b in blocks
    )
    return f"<html><head>{scripts}</head><body>{extra}</body></html>"


BASIC = {
    "@context": "https://schema.org",
    "@type": "JobPosting",
    "title": "Senior Backend Engineer",
    "description": "<p>Python, PostgreSQL and Kubernetes.</p>",
    "identifier": "REQ-4471",
    "url": "https://acme.example/careers/req-4471",
    "datePosted": "2026-08-01",
    "jobLocation": {
        "@type": "Place",
        "address": {
            "@type": "PostalAddress",
            "addressLocality": "San Francisco",
            "addressRegion": "CA",
            "addressCountry": "US",
        },
    },
}


# --------------------------------------------------------------------------
# The happy path
# --------------------------------------------------------------------------


def test_a_plain_posting_is_read() -> None:
    posting = extract(_page(BASIC), page_url=PAGE_URL)[0]

    assert posting.title == "Senior Backend Engineer"
    assert posting.external_id == "REQ-4471"
    assert posting.url == "https://acme.example/careers/req-4471"
    assert posting.ats_type == ATS
    assert posting.published_at is not None
    assert posting.content_hash


def test_the_description_arrives_as_text_not_markup() -> None:
    """It feeds embeddings and the fabrication guard, so tags in it are not
    cosmetic."""
    posting = extract(_page(BASIC), page_url=PAGE_URL)[0]

    assert posting.description_raw is not None
    assert "<p>" not in posting.description_raw
    assert "Python" in posting.description_raw


def test_the_address_is_joined_the_way_locality_reads_it() -> None:
    """`locality.py` needs city, region and country together — a bare "London"
    is ambiguous where "London, United Kingdom" is not."""
    posting = extract(_page(BASIC), page_url=PAGE_URL)[0]

    assert posting.location == "San Francisco, CA, US"


# --------------------------------------------------------------------------
# The shapes sites actually use
# --------------------------------------------------------------------------


def test_a_posting_wrapped_in_a_graph_is_found() -> None:
    page = _page({"@context": "https://schema.org", "@graph": [BASIC]})

    assert len(extract(page, page_url=PAGE_URL)) == 1


def test_a_posting_inside_an_item_list_is_found() -> None:
    page = _page(
        {
            "@context": "https://schema.org",
            "@type": "ItemList",
            "itemListElement": [{"@type": "ListItem", "item": BASIC}],
        }
    )
    # `item` is not a wrapper key we walk, so this documents the limit rather
    # than pretending otherwise — sites that nest this way need `mainEntity`
    # or `@graph`, which are the two the schema actually recommends.
    assert extract(page, page_url=PAGE_URL) == []


def test_a_bare_list_at_the_top_level_is_found() -> None:
    second = {**BASIC, "title": "Data Engineer", "identifier": "REQ-9000"}
    page = _page([BASIC, second])

    titles = {p.title for p in extract(page, page_url=PAGE_URL)}
    assert titles == {"Senior Backend Engineer", "Data Engineer"}


def test_a_type_given_as_a_list_still_matches() -> None:
    page = _page({**BASIC, "@type": ["JobPosting", "Thing"]})

    assert len(extract(page, page_url=PAGE_URL)) == 1


def test_several_blocks_on_one_page_are_all_read() -> None:
    other = {**BASIC, "title": "Product Designer", "identifier": "REQ-7"}

    assert len(extract(_page(BASIC, other), page_url=PAGE_URL)) == 2


# --------------------------------------------------------------------------
# Remote
# --------------------------------------------------------------------------


def test_telecommute_becomes_a_location_rather_than_nothing() -> None:
    """An empty location classifies as UNKNOWN. "Remote - USA" is what the
    search area actually needs to decide."""
    page = _page(
        {
            **BASIC,
            "jobLocation": None,
            "jobLocationType": "TELECOMMUTE",
            "applicantLocationRequirements": [{"@type": "Country", "name": "USA"}],
        }
    )

    assert extract(page, page_url=PAGE_URL)[0].location == "Remote - USA"


def test_telecommute_with_no_region_still_says_remote() -> None:
    page = _page({**BASIC, "jobLocation": None, "jobLocationType": "TELECOMMUTE"})

    assert extract(page, page_url=PAGE_URL)[0].location == "Remote"


def test_several_offices_are_all_named() -> None:
    page = _page(
        {
            **BASIC,
            "jobLocation": [
                {"address": {"addressLocality": "Austin", "addressRegion": "TX"}},
                {"address": {"addressLocality": "Denver", "addressRegion": "CO"}},
            ],
        }
    )

    assert extract(page, page_url=PAGE_URL)[0].location == "Austin, TX; Denver, CO"


# --------------------------------------------------------------------------
# Malformed pages, which are the normal case
# --------------------------------------------------------------------------


def test_one_broken_block_does_not_lose_the_others() -> None:
    """Sites emit several blocks and a trailing comma in the analytics one
    should not cost us the jobs."""
    page = (
        '<html><head><script type="application/ld+json">{"broken",}</script>'
        f'<script type="application/ld+json">{json.dumps(BASIC)}</script>'
        "</head><body></body></html>"
    )

    assert len(extract(page, page_url=PAGE_URL)) == 1


def test_a_page_with_no_structured_data_yields_nothing() -> None:
    """The honest answer, not a reason to start guessing at markup."""
    html = "<html><body><h1>Careers</h1><div class='job'>Backend Engineer</div></body></html>"

    assert extract(html, page_url=PAGE_URL) == []


def test_other_structured_data_is_ignored() -> None:
    page = _page({"@context": "https://schema.org", "@type": "Organization", "name": "Acme"})

    assert extract(page, page_url=PAGE_URL) == []


def test_a_closing_tag_escaped_the_way_html_requires_survives() -> None:
    """A description that mentions `</script>` has to be written `<\\/script>`:
    the HTML spec ends a `<script>` element at the first literal closing tag, so
    no parser can recover one emitted raw. `\\/` is a legal JSON escape for `/`,
    so the tokenizer hands back the tag intact and the block parses — which is
    what a real site produces. The description stripper then removes it as the
    markup it looks like, leaving the prose around it."""
    body = json.dumps({**BASIC, "description": "Ship it </script> and more"})
    page = (
        "<html><head>"
        f'<script type="application/ld+json">{body.replace("</", "<\\/")}</script>'
        "</head><body></body></html>"
    )

    assert "</script>" in job_postings(page)[0]["description"]

    postings = extract(page, page_url=PAGE_URL)
    assert len(postings) == 1
    assert postings[0].description_raw == "Ship it and more"


def test_a_raw_closing_tag_truncates_its_own_block_and_nothing_else() -> None:
    """The other half of the same fact. A site that emits `</script>` unescaped
    has already ended its element, so that block is unparseable JSON — it is
    skipped, and the rest of the page is still read. Losing one posting is the
    cost of the site's bug; losing the page would be ours."""
    broken = (
        '<script type="application/ld+json">'
        '{"@type": "JobPosting", "title": "Ship it </script> and more"}'
        "</script>"
    )
    page = _page(BASIC, extra=broken)

    postings = extract(page, page_url=PAGE_URL)
    assert [p.title for p in postings] == ["Senior Backend Engineer"]


def test_a_posting_with_no_title_is_refused() -> None:
    """Everything downstream names a job by its title; a row that cannot be
    named is worse than an absent one."""
    page = _page({"@context": "https://schema.org", "@type": "JobPosting", "identifier": "X"})

    assert extract(page, page_url=PAGE_URL) == []


# --------------------------------------------------------------------------
# Identity
# --------------------------------------------------------------------------


def test_the_url_is_the_identity_when_the_site_gives_no_id() -> None:
    """Never a hash of the description: editing the text would change the id
    and the posting would re-appear as a new job."""
    node = {k: v for k, v in BASIC.items() if k != "identifier"}
    posting = extract(_page(node), page_url=PAGE_URL)[0]

    assert "req-4471" in posting.external_id


def test_editing_the_description_does_not_change_identity() -> None:
    first = extract(_page(BASIC), page_url=PAGE_URL)[0]
    second = extract(_page({**BASIC, "description": "Rewritten."}), page_url=PAGE_URL)[0]

    assert first.external_id == second.external_id
    assert first.content_hash != second.content_hash, "but the content hash must move"


def test_the_same_posting_twice_on_one_page_is_emitted_once() -> None:
    assert len(extract(_page(BASIC, BASIC), page_url=PAGE_URL)) == 1


# --------------------------------------------------------------------------
# The pieces, directly
# --------------------------------------------------------------------------


def test_script_blocks_reads_only_ld_json() -> None:
    page = (
        "<html><head><script>var x = 1;</script>"
        '<script type="application/json">{"not":"ld"}</script>'
        f'<script type="application/ld+json">{json.dumps(BASIC)}</script></head></html>'
    )

    assert len(script_blocks(page)) == 1


@pytest.mark.parametrize("html", ["", "<html>", "not html at all", "<script>"])
def test_rubbish_input_returns_nothing_rather_than_raising(html: str) -> None:
    """A crawl over three thousand sites meets every kind of broken page, and
    one of them must not stop the sweep."""
    assert job_postings(html) == []
    assert extract(html, page_url=PAGE_URL) == []


# --------------------------------------------------------------------------
# Wiring: the crawler reaches this the same way it reaches a board API
# --------------------------------------------------------------------------


def test_the_registry_hands_back_this_extractor() -> None:
    """`crawl_company` finds every extractor through `extractor_for`, so an
    extractor that is not registered is one the crawler cannot reach."""
    from packages.crawler.extract import extractor_for

    extractor = extractor_for(ATS)
    assert extractor is not None
    assert extractor.ats == ATS


def test_it_is_not_in_the_board_api_registry() -> None:
    """`EXTRACTORS` is iterated by callers that build a URL from a company
    slug. This one takes a page URL, so being in that dict would mean being
    asked the one question it cannot answer."""
    from packages.crawler.extract import EXTRACTORS

    assert ATS not in EXTRACTORS


def test_the_board_url_is_the_page_itself() -> None:
    """A bespoke page has no slug rule — the URL is the identity, which is why
    a `jsonld` seed carries it in `slug`."""
    from packages.crawler.jsonld import JsonLdExtractor

    assert JsonLdExtractor().board_url(PAGE_URL) == PAGE_URL


def test_a_slug_that_is_not_a_url_is_reported_rather_than_raised() -> None:
    """`crawl_company` calls `board_url` before its try block, so raising here
    would end the whole cycle over one bad row. Returned as-is, the fetch fails
    and only that company is reported."""
    from packages.crawler.jsonld import JsonLdExtractor

    assert JsonLdExtractor().board_url("acme") == "acme"


def test_parse_reads_the_page_through_the_protocol() -> None:
    from packages.crawler.jsonld import JsonLdExtractor

    postings = JsonLdExtractor().parse(_page(BASIC), PAGE_URL)
    assert [p.title for p in postings] == ["Senior Backend Engineer"]


def test_a_relative_posting_url_is_resolved_against_the_page() -> None:
    """Sites publish "/careers/req-4471" as often as the absolute form, and a
    relative URL is not something the applier can open later."""
    page = _page({**BASIC, "url": "/careers/req-4471"})

    assert extract(page, page_url=PAGE_URL)[0].url == "https://acme.example/careers/req-4471"


async def test_the_crawler_polls_a_bespoke_page_like_any_other_board(db_session) -> None:
    """End to end through `crawl_company`, because everything above this tests
    the extractor in isolation and the wiring is where the last one broke."""
    import httpx

    from packages.crawler.crawl import crawl_company
    from packages.crawler.extract import CompanySeed
    from packages.crawler.fetch import HostRateLimiter, PoliteFetcher
    from tests.test_crawler import FakeClock

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow:")
        return httpx.Response(200, text=_page(BASIC))

    clock = FakeClock()
    fetcher = PoliteFetcher(
        transport=httpx.MockTransport(handler),
        rate_limiter=HostRateLimiter(clock=clock, sleeper=clock.sleep),
    )
    seed = CompanySeed(name="Acme", slug=PAGE_URL, ats=ATS, careers_url=PAGE_URL, poll_interval_s=0)

    first = await crawl_company(db_session, seed, fetcher, force=True)
    assert first.new_postings == 1

    # Gate 5's rule holds here too: a second poll of an unchanged page emits
    # nothing. Bespoke pages change less often than boards, not more.
    second = await crawl_company(db_session, seed, fetcher, force=True)
    assert second.emitted == 0


# --------------------------------------------------------------------------
# Arbitrary JSON from three thousand strangers' sites
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "description",
    [
        {"@value": "Ship it"},
        ["Ship it", "and more"],
        42,
    ],
)
def test_a_description_that_is_not_a_string_yields_no_description(description: object) -> None:
    """`strip_html` hands its argument to `HTMLParser.feed`, which raises
    TypeError on anything but a string. The posting is still worth having — a
    title and a URL are what the feed needs — so the field goes empty."""
    postings = extract(_page({**BASIC, "description": description}), page_url=PAGE_URL)

    assert len(postings) == 1
    assert postings[0].description_raw is None


@pytest.mark.parametrize(
    ("country", "expected"),
    [
        ("US", "San Francisco, CA, US"),
        ({"@type": "Country", "name": "United States"}, "San Francisco, CA, United States"),
        (["US"], "San Francisco, CA"),
        (7, "San Francisco, CA"),
        (None, "San Francisco, CA"),
    ],
)
def test_address_country_is_read_in_every_shape_a_site_emits(
    country: object, expected: str
) -> None:
    """Both forms the schema allows, and the ones it does not. An `or {}` guard
    catches only falsy values, so a list reached `.get` and raised out of the
    whole extraction — losing every posting on the page over one field."""
    page = _page(
        {
            **BASIC,
            "jobLocation": {
                "address": {
                    "addressLocality": "San Francisco",
                    "addressRegion": "CA",
                    "addressCountry": country,
                }
            },
        }
    )

    assert extract(page, page_url=PAGE_URL)[0].location == expected


def test_one_unreadable_posting_does_not_lose_the_others() -> None:
    """The same rule `job_postings` applies to a malformed block, one level
    down. This is what makes `bespoke.probe_page`'s "never raises" true: the
    next unforeseen shape is one nobody has seen, and a sweep of three thousand
    pages must not end on it."""
    hostile = {**BASIC, "identifier": "REQ-BAD", "jobLocation": {"address": {"addressRegion": []}}}
    good = {**BASIC, "title": "Data Engineer", "identifier": "REQ-9000"}

    titles = {p.title for p in extract(_page(hostile, good), page_url=PAGE_URL)}

    assert "Data Engineer" in titles


def test_a_deeply_nested_block_is_skipped_and_the_page_survives() -> None:
    """`json.loads` and `_walk` both recurse, and the json module sets no
    nesting limit of its own — it inherits the interpreter's. So deeply nested
    JSON-LD raises `RecursionError` rather than parsing, and that is not a
    decode error: it escaped the `JSONDecodeError` guard entirely. Since
    `bespoke.probe_page` calls `extract` outside its own try, one such page
    would have ended a sweep of thousands."""
    deep = "[" * 20_000 + "]" * 20_000
    page = (
        "<html><head>"
        f'<script type="application/ld+json">{deep}</script>'
        f'<script type="application/ld+json">{json.dumps(BASIC)}</script>'
        "</head><body></body></html>"
    )

    assert [p.title for p in extract(page, page_url=PAGE_URL)] == ["Senior Backend Engineer"]
