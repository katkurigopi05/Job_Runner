"""Sorting a CSV of companies into what the crawler can already reach.

`scripts/import_portals.py` states the constraint this exists to answer:

    A company whose careers page is its own site is reported and skipped: we
    have no extractor for a bespoke page, so adding it to the registry would
    mean a crawl cycle that fetches and parses nothing every hour, forever.

Given a sheet of ~3,000 companies, the question that decides the work is how
many are *actually* bespoke — careers URLs are very often already a board we
support. This is offline so the answer does not depend on which sites are up.
"""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from packages.crawler.company_csv import classify_url, read_rows, triage, write_bespoke

SHEET = """company_name,careers_url,sector
Acme,https://boards.greenhouse.io/acme,fintech
Globex,https://jobs.lever.co/globex,saas
Initech,https://jobs.ashbyhq.com/initech,devtools
Umbrella,https://apply.workable.com/umbrella,biotech
Soylent,https://www.soylent.com/careers,food
Nothing,,none
"""


@pytest.fixture
def sheet(tmp_path: Path) -> Path:
    path = tmp_path / "companies.csv"
    path.write_text(SHEET, encoding="utf-8")
    return path


# --------------------------------------------------------------------------
# Classification
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://boards.greenhouse.io/acme", ("greenhouse", "acme")),
        ("https://job-boards.greenhouse.io/acme", ("greenhouse", "acme")),
        ("https://jobs.lever.co/globex", ("lever", "globex")),
        ("https://jobs.eu.lever.co/globex", ("lever", "globex")),
        ("https://jobs.ashbyhq.com/initech", ("ashby", "initech")),
        ("https://apply.workable.com/umbrella", ("workable", "umbrella")),
    ],
)
def test_a_board_url_is_recognised(url: str, expected: tuple[str, str]) -> None:
    assert classify_url(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "https://boards.greenhouse.io/stark/jobs/4023419",
        "https://jobs.lever.co/globex/8f2c1a90-1234-4d5e",
        "https://jobs.ashbyhq.com/initech/abc-123",
    ],
)
def test_a_link_to_one_posting_still_yields_the_board(url: str) -> None:
    """A hand-assembled sheet is full of deep links.

    `board_root` anchors to the board's front page on purpose; throwing away a
    row because someone pasted the job they were looking at would count a
    company we can crawl as bespoke, which is the one number this is for.
    """
    found = classify_url(url)
    assert found is not None
    assert found[1] in {"stark", "globex", "initech"}


@pytest.mark.parametrize(
    "url",
    [
        "https://www.soylent.com/careers",
        "https://hooli.com/en/careers/openings",
        "https://example.org/join-us",
    ],
)
def test_a_companys_own_site_is_not_a_board(url: str) -> None:
    assert classify_url(url) is None


# --------------------------------------------------------------------------
# Reading the sheet
# --------------------------------------------------------------------------


def test_the_columns_are_detected_not_assumed(tmp_path: Path) -> None:
    path = tmp_path / "other.csv"
    path.write_text("employer,jobs_url\nAcme,https://jobs.lever.co/acme\n", encoding="utf-8")

    rows, header = read_rows(path)

    assert header == ["employer", "jobs_url"]
    assert rows[0].name == "Acme"
    assert rows[0].url == "https://jobs.lever.co/acme"


def test_a_sheet_with_no_url_column_says_which_columns_it_has(tmp_path: Path) -> None:
    """On somebody else's 3,000-row export, "which column did you mean" is the
    question. Guessing would classify every row as unusable and read as a
    broken importer rather than a mismatched sheet."""
    path = tmp_path / "wrong.csv"
    path.write_text("company,ticker\nAcme,ACME\n", encoding="utf-8")

    with pytest.raises(ValueError) as caught:
        read_rows(path)

    assert "ticker" in str(caught.value)


def test_a_missing_name_falls_back_to_the_host(tmp_path: Path) -> None:
    path = tmp_path / "urls.csv"
    path.write_text("careers_url\nhttps://www.soylent.com/careers\n", encoding="utf-8")

    rows, _ = read_rows(path)

    assert rows[0].name == "Soylent"


# --------------------------------------------------------------------------
# Triage
# --------------------------------------------------------------------------


def test_the_sheet_splits_three_ways(sheet: Path) -> None:
    rows, _ = read_rows(sheet)
    report = triage(rows)

    assert report.total == 6
    assert len(report.promotable) == 4
    assert len(report.bespoke) == 1
    assert len(report.unusable) == 1
    assert dict(report.by_vendor) == {
        "greenhouse": 1,
        "lever": 1,
        "ashby": 1,
        "workable": 1,
    }


def test_a_repeated_url_is_collapsed(tmp_path: Path) -> None:
    """3,000 rows assembled from several sources will repeat companies, and a
    duplicate counted as a second promotable row overstates the coverage."""
    path = tmp_path / "dupes.csv"
    path.write_text(
        "company,careers_url\n"
        "Globex,https://jobs.lever.co/globex\n"
        "Globex Inc,https://jobs.lever.co/globex/\n",
        encoding="utf-8",
    )
    rows, _ = read_rows(path)

    report = triage(rows)

    assert report.duplicates == 1
    assert len(report.promotable) == 1


def test_no_url_and_a_bad_url_are_reported_apart(tmp_path: Path) -> None:
    """ "You have no URL for this company" and "we cannot read this page yet"
    need different fixes, so they are never one bucket."""
    path = tmp_path / "bad.csv"
    path.write_text("company,careers_url\nNoUrl,\nFtp,ftp://example.org/jobs\n", encoding="utf-8")
    rows, _ = read_rows(path)

    report = triage(rows)

    reasons = {entry.reason for entry in report.unusable}
    assert reasons == {"no URL", "not an http(s) URL"}
    assert not report.bespoke


def test_the_summary_names_the_vendors(sheet: Path) -> None:
    rows, _ = read_rows(sheet)

    summary = triage(rows).summary()

    assert "greenhouse" in summary
    assert "bespoke" in summary


# --------------------------------------------------------------------------
# The work queue
# --------------------------------------------------------------------------


def test_the_bespoke_list_keeps_every_original_column(sheet: Path, tmp_path: Path) -> None:
    """A sheet carrying a sector or a headcount should not lose it here — the
    extractor that reads this next may want the largest companies first."""
    rows, header = read_rows(sheet)
    report = triage(rows)

    out = write_bespoke(report.bespoke, tmp_path / "bespoke.csv", header)

    with out.open(newline="", encoding="utf-8") as handle:
        written = list(csv.DictReader(handle))

    assert [row["company_name"] for row in written] == ["Soylent"]
    assert written[0]["sector"] == "food"
    assert written[0]["careers_url"] == "https://www.soylent.com/careers"


def test_this_module_never_writes_to_the_registry(sheet: Path) -> None:
    """One writer. `scripts/import_companies` appends through
    `import_portals.append_to_registry`, so the registry has a single door and
    a dry run cannot half-modify it."""
    import ast

    tree = ast.parse(Path("packages/crawler/company_csv.py").read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)

    # Checked against the AST, not the source text: the module docstring names
    # `append_to_registry` to say it is somebody else's job, and a grep would
    # read that explanation as the thing it forbids.
    assert "append_to_registry" not in imported
    assert "default_seed_path" not in imported
    assert not any(name.startswith("scripts") for name in imported)


def test_three_thousand_rows_are_sorted_without_the_network(tmp_path: Path) -> None:
    """The size this was written for, and the reason it is offline.

    A network probe per row would make the answer depend on which sites happen
    to be up, and turn a question you want to re-ask while cleaning the sheet
    into an hours-long crawl.
    """
    lines = ["company,careers_url"]
    for index in range(1000):
        lines.append(f"Board{index},https://boards.greenhouse.io/board{index}")
        lines.append(f"Own{index},https://own{index}.example/careers")
        lines.append(f"Lever{index},https://jobs.lever.co/lever{index}")
    path = tmp_path / "big.csv"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    rows, _ = read_rows(path)
    report = triage(rows)

    assert report.total == 3000
    assert len(report.promotable) == 2000
    assert len(report.bespoke) == 1000
    assert dict(report.by_vendor) == {"greenhouse": 1000, "lever": 1000}


# --------------------------------------------------------------------------
# The portal importer shares this matcher
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    ["https://apply.workable.com/acme", "https://jobs.workable.com/acme"],
)
def test_the_portal_importer_recognises_workable(url: str) -> None:
    """It did not, for as long as `packages/ats/workable.py` has existed.

    `import_portals.identify` carried its own three-pattern list — Greenhouse,
    Lever, Ashby — so every Workable company in a portal list was filed as "a
    bespoke careers page we have no extractor for". A company we can both
    crawl and apply to, skipped for no reason. It delegates here now.
    """
    from scripts.import_portals import identify

    assert identify(url) == ("workable", "acme")


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://boards-api.greenhouse.io/v1/boards/stripe/jobs", ("greenhouse", "stripe")),
        ("https://api.lever.co/v0/postings/globex", ("lever", "globex")),
    ],
)
def test_the_api_endpoint_forms_still_resolve(url: str, expected: tuple[str, str]) -> None:
    """A portal list carries API endpoints, which a careers-page matcher has
    no reason to know. Delegating must not lose them."""
    from scripts.import_portals import identify

    assert identify(url) == expected


def test_a_bespoke_page_is_still_unidentified() -> None:
    from scripts.import_portals import identify

    assert identify("https://twilio.com/careers") is None
