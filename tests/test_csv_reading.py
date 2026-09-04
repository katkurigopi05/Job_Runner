"""Reading a company list someone else wrote.

Both bugs pinned here came from a real 3,869-row Bay Area list, and both were
silent — the tool ran, reported confidently, and was wrong.
"""

from __future__ import annotations

from pathlib import Path

from scripts.find_boards import read_companies, usable_url


def _csv(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "companies.csv"
    path.write_text(text, encoding="utf-8")
    return path


def test_a_spreadsheet_header_is_recognised(tmp_path: Path) -> None:
    """`Company Name` is what a person writes. `company_name` is not.

    Matching headers literally meant a real file fell through to the
    headerless path, which parsed the header row itself as a company — and
    then went looking for a job board belonging to "Company Name".
    """
    path = _csv(tmp_path, "Company Name,Website URL\nAcme,https://acme.com\n")

    companies = read_companies(path)

    assert companies == [("Acme", "https://acme.com")]


def test_search_links_are_not_treated_as_evidence(tmp_path: Path) -> None:
    """A real list had google.com search links in 3,864 of 3,869 careers cells.

    Someone had generated `site:acme.com careers jobs` per row rather than
    finding the page. Following those would be a robots violation that learned
    nothing.
    """
    assert usable_url("https://www.google.com/search?q=site%3Aacme.com+careers") is None
    assert usable_url("https://www.linkedin.com/company/acme/jobs") is None
    assert usable_url("https://jobs.ashbyhq.com/acme") == "https://jobs.ashbyhq.com/acme"


def test_a_company_url_with_a_linkedin_query_parameter_is_usable() -> None:
    url = "https://acme.com/careers?source=linkedin.com"

    assert usable_url(url) == url


def test_the_url_column_is_chosen_by_what_it_holds(tmp_path: Path) -> None:
    """The best-named column is not always the useful one.

    `Jobs/Careers URL` sounds authoritative and held search links; `Website
    URL` sounded generic and held the real hosts. Scoring by usable content
    picks the one that can actually name a board.
    """
    path = _csv(
        tmp_path,
        "Company Name,Website URL,Jobs/Careers URL\n"
        "Acme,https://acme.com,https://www.google.com/search?q=acme+jobs\n"
        "Beta,https://beta.com,https://www.google.com/search?q=beta+jobs\n",
    )

    companies = read_companies(path)

    assert [url for _, url in companies] == ["https://acme.com", "https://beta.com"]


def test_a_headerless_list_keeps_its_first_row(tmp_path: Path) -> None:
    path = _csv(tmp_path, "Acme\nBeta\n")

    assert read_companies(path) == [("Acme", None), ("Beta", None)]


def test_an_empty_file_is_not_an_error(tmp_path: Path) -> None:
    assert read_companies(_csv(tmp_path, "")) == []
