"""Importing a career-ops portal list into our registry.

Their list is maintained, so this is a re-runnable import rather than a paste.
That makes the interesting tests the ones about *not* doing things: not
importing what we cannot crawl, not touching what the owner wrote, and not
quietly merging two companies that happen to share a name.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from packages.crawler.extract import CompanySeed, load_seed
from scripts.import_portals import convert, identify, read_portals


def _portals(tmp_path: Path, entries: list[dict]) -> Path:
    path = tmp_path / "portals.yml"
    path.write_text(yaml.safe_dump({"tracked_companies": entries}))
    return path


# --------------------------------------------------------------------------
# Reading an ATS off a URL
# --------------------------------------------------------------------------


def test_every_supported_board_shape_is_recognised() -> None:
    assert identify("https://boards-api.greenhouse.io/v1/boards/acme/jobs") == (
        "greenhouse",
        "acme",
    )
    assert identify("https://job-boards.greenhouse.io/acme") == ("greenhouse", "acme")
    assert identify("https://job-boards.eu.greenhouse.io/acme") == ("greenhouse", "acme")
    assert identify("https://jobs.lever.co/acme") == ("lever", "acme")
    assert identify("https://jobs.ashbyhq.com/acme") == ("ashby", "acme")


def test_a_bespoke_careers_page_is_not_identified() -> None:
    """We have no extractor for a company's own site, so importing it would
    mean a crawl that fetches and parses nothing every hour forever."""
    assert identify("https://www.twilio.com/en-us/company/jobs") is None
    assert identify("https://careers.salesforce.com") is None


def test_the_api_url_is_preferred_over_the_careers_url() -> None:
    assert identify("https://boards-api.greenhouse.io/v1/boards/real/jobs", "https://acme.com") == (
        "greenhouse",
        "real",
    )


# --------------------------------------------------------------------------
# What gets imported
# --------------------------------------------------------------------------


def test_a_recognised_company_is_imported() -> None:
    additions, _ = convert(
        [{"name": "Acme", "careers_url": "https://jobs.ashbyhq.com/acme", "enabled": True}], []
    )

    assert [(s.name, s.ats, s.slug) for s in additions] == [("Acme", "ashby", "acme")]


def test_a_company_already_in_the_registry_is_left_alone() -> None:
    """Deciding their 'Acme' and our 'Acme' are the same row is a judgement
    this script has no business making silently."""
    existing = [CompanySeed(name="Acme", slug="acme-corp", ats="greenhouse")]

    additions, skipped = convert(
        [{"name": "Acme", "careers_url": "https://jobs.lever.co/acme", "enabled": True}], existing
    )

    assert additions == []
    assert "already in the registry" in skipped[0].reason


def test_a_duplicate_slug_under_a_new_name_is_skipped() -> None:
    existing = [CompanySeed(name="Acme Corporation", slug="acme", ats="ashby")]

    additions, skipped = convert(
        [{"name": "Acme Inc", "careers_url": "https://jobs.ashbyhq.com/acme", "enabled": True}],
        existing,
    )

    assert additions == []
    assert "ashby:acme" in skipped[0].reason


def test_disabled_entries_are_honoured() -> None:
    """Their `enabled: false` is a considered decision by someone who has
    been through the list."""
    additions, skipped = convert(
        [{"name": "Acme", "careers_url": "https://jobs.lever.co/acme", "enabled": False}], []
    )

    assert additions == []
    assert "disabled" in skipped[0].reason


def test_two_source_entries_sharing_a_slug_import_once() -> None:
    additions, _ = convert(
        [
            {"name": "Acme", "careers_url": "https://jobs.lever.co/acme", "enabled": True},
            {"name": "Acme Labs", "careers_url": "https://jobs.lever.co/acme", "enabled": True},
        ],
        [],
    )

    assert len(additions) == 1


def test_reading_a_portals_file(tmp_path: Path) -> None:
    path = _portals(
        tmp_path, [{"name": "Acme", "careers_url": "https://jobs.lever.co/acme", "enabled": True}]
    )

    assert read_portals(path)[0]["name"] == "Acme"


def test_the_import_is_idempotent(tmp_path: Path) -> None:
    """Re-running against an updated list must not duplicate what it added."""
    seeds = tmp_path / "companies.yaml"
    seeds.write_text("companies:\n  - name: Stripe\n    slug: stripe\n    ats: greenhouse\n")

    entries = [{"name": "Acme", "careers_url": "https://jobs.ashbyhq.com/acme", "enabled": True}]

    from scripts.import_portals import append_to_registry

    first, _ = convert(entries, load_seed(str(seeds)))
    append_to_registry(first, seeds)

    second, _ = convert(entries, load_seed(str(seeds)))

    assert len(first) == 1
    assert second == []
    assert "stripe" in seeds.read_text()
