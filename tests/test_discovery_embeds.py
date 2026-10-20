"""Job boards a careers page loads by script, and links that are not pages.

Found on 2026-10-19 by reading 21 careers pages discovery had recorded as
naming no board. Three (Censia, Thorn, Tigera) load Greenhouse's embed script,
`boards.greenhouse.io/embed/job_board/js?for=<slug>`: the board is named
outright, in the form the reader did not accept. Two more name a job system
only by its script host: Teamtailor's widget (`scripts.teamtailor-cdn.com`)
and Rippling's (`static-assets.ripplingcdn.com`). And one company's "careers
page" was a stylesheet, `.../wp-job-openings/assets/css/general.min.css`,
followed because its address contains "job-openings".
"""

from __future__ import annotations

from packages.crawler.find_boards import board_root, careers_links, from_url
from packages.crawler.job_systems import other_job_system
from tests.test_discovery_careers_pages import _site

# --- Greenhouse's embed script ------------------------------------------------------


def test_the_embed_script_names_the_board() -> None:
    assert board_root("https://boards.greenhouse.io/embed/job_board/js?for=censia") == (
        "greenhouse",
        "censia",
    )
    assert board_root("https://job-boards.greenhouse.io/embed/job_board/js?for=thorn") == (
        "greenhouse",
        "thorn",
    )


def test_the_embed_iframe_still_names_the_board() -> None:
    assert board_root("https://boards.greenhouse.io/embed/job_board?for=acme") == (
        "greenhouse",
        "acme",
    )


async def test_a_careers_page_loading_the_script_names_its_board() -> None:
    page = (
        '<div id="grnhse_app"></div>'
        '<script src="https://boards.greenhouse.io/embed/job_board/js?for=thorn"></script>'
    )

    reading = await from_url("https://thorn.org/careers", _site({"thorn.org/careers": page}))

    assert reading.board == ("greenhouse", "thorn")


async def test_a_script_added_by_code_names_its_board_too() -> None:
    """Tigera sets the script's address from a script of its own."""
    page = "<script>script.src = 'https://boards.greenhouse.io/embed/job_board/js?for=tigera';</script>"

    reading = await from_url("https://tigera.io/careers", _site({"tigera.io/careers": page}))

    assert reading.board == ("greenhouse", "tigera")


# --- job systems known by their script host -----------------------------------------


def test_teamtailors_widget_names_teamtailor() -> None:
    page = '<script src="https://scripts.teamtailor-cdn.com/widgets/eu-pink/jobs.js"></script>'

    assert other_job_system(page) == (
        "Teamtailor",
        "https://scripts.teamtailor-cdn.com/widgets/eu-pink/jobs.js",
    )


def test_ripplings_job_board_embed_names_rippling() -> None:
    page = '<script src="https://static-assets.ripplingcdn.com/ats/embeds/job-board.js"></script>'

    assert other_job_system(page) is not None
    assert other_job_system(page)[0] == "Rippling"


# --- a file is not a careers page ---------------------------------------------------


def test_a_stylesheet_is_not_a_careers_page() -> None:
    page = (
        '<link rel="stylesheet" href="/wp-content/plugins/wp-job-openings/assets/css/'
        'general.min.css?ver=4.0.4">'
        '<a href="/careers">Careers</a>'
    )

    assert careers_links(page, "https://ditoweb.com/") == ["https://ditoweb.com/careers"]


def test_scripts_images_and_documents_are_not_careers_pages_either() -> None:
    page = (
        '<script src="/assets/jobs-widget.js"></script>'
        '<img src="/img/careers-hero.webp">'
        '<a href="/files/careers-brochure.pdf">Brochure</a>'
    )

    assert careers_links(page, "https://acme.com/") == []
