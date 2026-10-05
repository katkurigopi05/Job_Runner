"""Public board directories: a company's board proposed without guessing.

Measured on the 200-company trial (docs/ATS_DISCOVERY_RESEARCH.md): the two
directories proposed the exact board for 23 of the 33 the trial found, with no
request at all, while name-guessing found none of the 188 sheet companies. They
also proposed wrong boards — Xpansiv for Ashby's `jobs` — so a proposal is a
candidate to probe, never a verdict, and some shapes are never proposed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from packages.crawler.board_directory import (
    BoardDirectory,
    Candidate,
    DirectoryEntry,
    DirectoryFormatError,
    confirms,
    domain_stem,
    normalize_name,
    registrable_domain,
)
from packages.crawler.extract import ExtractedPosting

FIGSHARE = (
    "ats_vendor,company_name,board_slug,last_crawled\n"
    'ashby,"Acme Labs",acmelabs,2026-07-12\n'
    "greenhouse,Black Forest Labs,blackforestlabs,2026-07-30\n"
    "ashby,Jobs Co,jobs,2026-07-01\n"
)
JOB_BOARD_DIRECTORY = (
    "platform,token,company,board_url,api_url,open_roles,count_basis,region\n"
    "workday,wd1/widget/External,widget,https://widget.wd1.myworkdayjobs.com/External,x,9,vendor,\n"
    "lever,widgetco,Widget Co,https://jobs.lever.co/widgetco,x,5,observed,\n"
    "greenhouse,acme,Acme,https://job-boards.greenhouse.io/acme,x,3,observed,\n"
)


@pytest.fixture
def directory(tmp_path: Path) -> BoardDirectory:
    (tmp_path / "figshare.csv").write_text(FIGSHARE, encoding="utf-8")
    (tmp_path / "jbd.csv").write_text(JOB_BOARD_DIRECTORY, encoding="utf-8")
    return BoardDirectory.from_files(sorted(tmp_path.glob("*.csv")))


@pytest.mark.parametrize(
    ("url", "stem"),
    [
        ("https://acme.com", "acme"),
        ("https://www.acme.ai/careers", "acme"),
        ("https://jobs.xpansiv.com/open-roles", "xpansiv"),
        ("acme.co.uk", "acme"),
        ("", ""),
    ],
)
def test_the_domain_stem_is_the_registrable_label(url: str, stem: str) -> None:
    """Not the first label: `jobs.xpansiv.com` is Xpansiv, not a board named `jobs`."""
    assert domain_stem(url) == stem


def test_corporate_suffixes_are_not_part_of_a_name() -> None:
    assert normalize_name("Acme Labs, Inc.") == normalize_name("ACME") == "acme"
    assert normalize_name("Black Forest Labs") == "blackforest"


def test_a_slug_matching_the_website_comes_first(directory: BoardDirectory) -> None:
    found = directory.candidates("Acme Labs", "https://acme.com")

    assert [(c.vendor, c.slug) for c in found][0] == ("greenhouse", "acme")
    assert found[0].why == "slug matches website"
    assert ("ashby", "acmelabs") in [(c.vendor, c.slug) for c in found], "same name, second"


def test_a_same_name_entry_is_proposed(directory: BoardDirectory) -> None:
    found = directory.candidates("Black Forest Labs", "https://bfl.ai")

    assert [(c.vendor, c.slug, c.why) for c in found] == [
        ("greenhouse", "blackforestlabs", "same name in directory")
    ]


def test_a_generic_slug_is_never_proposed(directory: BoardDirectory) -> None:
    """Xpansiv was proposed Ashby's `jobs` in the trial join. No board named
    `jobs`, `careers` or the like identifies one company."""
    assert directory.candidates("Jobs Co", "https://jobs.example.com") == []
    assert directory.candidates("Xpansiv", "https://jobs.jobs.com") == []


def test_platforms_we_do_not_read_are_left_out(directory: BoardDirectory) -> None:
    """The Workday row for `widget` never loads; the Lever `Widget Co` does."""
    found = directory.candidates("Widget", "https://widget.com")

    assert [(c.vendor, c.slug) for c in found] == [("lever", "widgetco")]
    assert len(directory) == 5


def test_no_match_proposes_nothing(directory: BoardDirectory) -> None:
    assert directory.candidates("Unknown Startup", "https://unknown.io") == []


def test_an_unknown_file_format_is_refused_by_name(tmp_path: Path) -> None:
    bad = tmp_path / "other.csv"
    bad.write_text("name,url\nAcme,https://acme.com\n", encoding="utf-8")

    with pytest.raises(DirectoryFormatError, match="other.csv"):
        BoardDirectory.from_files([bad])


# --- Identity ------------------------------------------------------------------
#
# The 200-company benchmark on 2026-10-05 verified 33 boards from the
# directories, and reading their postings showed 4 were namesakes: Artemis
# (artemispower.com) got an AI-security startup's board, Axle (axlepayments.com)
# Axle Informatics', Arena (arena.im) LMArena's, Armory (armory.io) a healthcare
# startup's. A name is shared and a domain is not, so a directory board counts
# only when a domain says it is the company's.


def _posting(text: str, url: str = "https://jobs.ashbyhq.com/x/1") -> ExtractedPosting:
    return ExtractedPosting(external_id="1", url=url, description_raw=text)


@pytest.mark.parametrize(
    ("url", "domain"),
    [
        ("https://arena.im", "arena.im"),
        ("https://usa.baidu.com/about", "baidu.com"),
        ("jobs.acme.co.uk", "acme.co.uk"),
        ("https://www.apptronik.com", "apptronik.com"),
        (None, ""),
    ],
)
def test_registrable_domain(url: str | None, domain: str) -> None:
    assert registrable_domain(url) == domain


def test_a_slug_that_is_the_dot_com_label_confirms_itself() -> None:
    """Apptronik's postings never print apptronik.com; its .com label is the evidence."""
    candidate = Candidate(vendor="greenhouse", slug="apptronik", why="slug matches website")

    assert confirms(candidate, "https://apptronik.com", [_posting("We build robots.")])


def test_a_slug_matching_another_tld_needs_the_board_to_name_the_site() -> None:
    """arena.im is not arena.ai: the label alone was LMArena's board."""
    candidate = Candidate(vendor="ashby", slug="arena", why="slug matches website")
    lmarena = _posting("About Arena Intelligence. Arena is the platform for evaluating AI.")

    assert confirms(candidate, "https://arena.im", [lmarena]) is None


def test_a_namesake_board_is_not_confirmed() -> None:
    candidate = Candidate(
        vendor="greenhouse", slug="axle", why="same name in directory", company="Axle"
    )
    informatics = _posting("Axle Informatics is a bioscience and information technology company.")

    assert confirms(candidate, "https://axlepayments.com", [informatics]) is None


def test_a_board_that_names_the_website_is_confirmed() -> None:
    candidate = Candidate(vendor="greenhouse", slug="apolloio", why="same name in directory")
    postings = [_posting("Apply at careers.apollo.io or learn more at https://www.apollo.io/about")]

    assert confirms(candidate, "https://apollo.io", postings)


def test_a_posting_url_on_the_company_domain_confirms() -> None:
    """Greenhouse returns the employer's own careers URL when one is configured."""
    candidate = Candidate(vendor="greenhouse", slug="bevicareers", why="same name in directory")
    postings = [_posting("Hydration, reimagined.", url="https://bevi.co/careers?gh_jid=12")]

    assert confirms(candidate, "https://bevi.co", postings)


@pytest.mark.parametrize("text", ["see axlepayments.company", "visit maxlepayments.com"])
def test_a_longer_domain_is_not_the_company_domain(text: str) -> None:
    candidate = Candidate(vendor="greenhouse", slug="axle", why="same name in directory")

    assert confirms(candidate, "https://axlepayments.com", [_posting(text)]) is None


def test_with_no_website_a_name_match_stands_as_a_guess_would() -> None:
    """Nothing to check against; the resolver accepts a name guess on the same terms."""
    candidate = Candidate(vendor="ashby", slug="acme", why="same name in directory")

    assert confirms(candidate, None, [_posting("Acme makes anvils.")])


# A name match was found by name, so the website's own label is an independent
# witness to it. From the same benchmark: artemispower, axlepayments and
# beamsolutions each extend the name with a word, and all three boards were
# namesakes; tryascend, trybadge, arkham and better agree, and all four were
# right. A slug match was found by the label, so the label cannot vouch for it.


@pytest.mark.parametrize(
    ("name", "website"),
    [
        ("Artemis", "https://artemispower.com"),
        ("Axle", "https://axlepayments.com"),
        ("Beam", "https://beamsolutions.com"),
    ],
)
def test_a_website_naming_a_longer_company_contradicts_a_name_match(
    name: str, website: str
) -> None:
    candidate = Candidate(
        vendor="ashby", slug=name.lower(), why="same name in directory", company=name
    )
    namesake = _posting(f"{name} is building something else entirely.")

    assert confirms(candidate, website, [namesake]) is None


@pytest.mark.parametrize(
    ("website", "slug"),
    [
        ("https://tryascend.com", "ascend21"),
        ("https://trybadge.com", "badge-group"),
        ("https://arkham.tech", "arkham-technologies"),
        ("https://ascendapp.com", "ascend21"),
    ],
)
def test_a_website_that_agrees_with_the_name_lets_a_name_match_stand(
    website: str, slug: str
) -> None:
    name = "Arkham" if "arkham" in website else ("Badge" if "badge" in website else "Ascend")
    candidate = Candidate(
        vendor="greenhouse", slug=slug, why="same name in directory", company=name
    )

    assert confirms(candidate, website, [_posting(f"About {name}.")]) == (
        "website agrees with the name"
    )


def test_a_website_shortening_the_name_agrees_with_it() -> None:
    """better.com is Better Mortgage's own site."""
    candidate = Candidate(
        vendor="ashby",
        slug="better-mortgage",
        why="same name in directory",
        company="Better Mortgage",
    )

    assert confirms(candidate, "https://better.com", [_posting("Homeownership, faster.")])


def test_the_label_cannot_vouch_for_a_match_it_made() -> None:
    """arena.im: the slug was found by the label, so agreement proves nothing."""
    candidate = Candidate(vendor="ashby", slug="arena", why="slug matches website")

    assert confirms(candidate, "https://arena.im", [_posting("About Arena Intelligence.")]) is None


def test_a_candidate_carries_the_directory_name_it_matched_on() -> None:
    directory = BoardDirectory([DirectoryEntry(vendor="ashby", slug="acme9", company="Acme Inc")])

    (candidate,) = directory.candidates("Acme", "https://acme.dev")

    assert candidate.company == "Acme Inc"
