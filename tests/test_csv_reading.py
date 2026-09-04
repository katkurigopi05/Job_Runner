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


def test_a_search_engine_name_elsewhere_in_the_url_is_not_a_search_link() -> None:
    """The test is on the hostname. Substring matching refused real careers pages.

    Three shapes, all of them legitimate and all of them dropped while
    `usable_url` matched against the whole URL string. The third is the
    expensive one: `usable_url` gates `company_csv.triage`, so a company whose
    own domain merely contains the word was silently filed as unusable.
    """
    keep = (
        "https://careers.acme.example/?ref=linkedin.com",
        "https://acme.example/jobs?utm_source=google.com",
        "https://notgoogle.example/careers",
        "https://boards.greenhouse.io/google",
    )
    for url in keep:
        assert usable_url(url) == url, url


def test_a_search_surface_is_refused_however_it_is_addressed() -> None:
    """Any label but the TLD, plus a `search.` host.

    Yahoo is a company that could legitimately appear in a Bay Area list, so
    the domain is kept and only its search surface refused. The TLD is excluded
    from matching so a hypothetical `.google` TLD on someone else's domain
    would not be read as Google's.
    """
    drop = (
        "https://www.google.com/search?q=site%3Aacme.com+careers",
        "https://google.co.uk/search?q=acme",
        "https://news.google.com/x",
        "https://linkedin.com/jobs/acme",
        "https://duckduckgo.com/?q=acme",
        "https://search.yahoo.com/search?p=acme",
    )
    for url in drop:
        assert usable_url(url) is None, url

    assert usable_url("https://yahoo.com/careers") == "https://yahoo.com/careers"


def test_something_that_is_not_a_url_is_not_usable() -> None:
    """A hostless string never reaches the label test, so it is checked here.

    A careers column holds free text as often as it holds a link — "email
    jobs@acme.com", "N/A", a bare domain with no scheme.
    """
    for value in (None, "", "   ", "N/A", "acme.com/careers", "mailto:jobs@acme.com", "https://"):
        assert usable_url(value) is None, value


def test_a_malformed_host_is_unusable_rather_than_an_exception() -> None:
    """`urlparse("https://[")` raises. `triage` catches nothing.

    Introduced by parsing the hostname — the substring version it replaced
    never parsed at all. `company_csv.triage` reads a company list row by row
    in a loop with no handler, so one malformed cell would abort the sweep
    over every row after it. On a 3,802-row sheet that is a truncated report
    that looks complete.
    """
    for value in ("https://[", "https://[::1", "http://[oops]/careers"):
        assert usable_url(value) is None, value

    # A well-formed IPv6 literal is still a URL, and is not a search surface.
    assert usable_url("http://[::1]/careers") == "http://[::1]/careers"
