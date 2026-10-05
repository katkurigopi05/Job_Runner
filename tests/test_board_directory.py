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
    DirectoryFormatError,
    domain_stem,
    normalize_name,
)

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
