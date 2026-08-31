"""Recording what a registry sweep found, in the registry.

Validation printed its verdict and stopped, so the answer lived in a terminal
scrollback and the file could not say which entries anyone had checked. That
is not cosmetic. The registry grew from 50 to 119 by import and the only 404
sweep ran against the original 50, leaving 90 entries whose silence is
unexplained — and a dead board yields zero postings, which is exactly what a
live board with nothing new yields.

The network half needs egress and is not exercised here; this is the write.
"""

from __future__ import annotations

import pytest
import yaml

from packages.crawler.extract import load_seed
from packages.crawler.validate import SeedState, SeedValidation, record

REGISTRY = {
    "companies": [
        {"name": "Alive", "slug": "alive", "ats": "greenhouse", "poll_interval_s": 21600},
        {"name": "Gone", "slug": "gone", "ats": "greenhouse", "poll_interval_s": 21600},
        {"name": "Untouched", "slug": "untouched", "ats": "greenhouse"},
    ]
}

RESULTS = [
    SeedValidation("Alive", "alive", "greenhouse", SeedState.API, api_status=200),
    SeedValidation(
        "Gone", "gone", "greenhouse", SeedState.MISSING, api_status=404, rendered_status=404
    ),
]


@pytest.fixture
def registry(tmp_path):
    path = tmp_path / "companies.yaml"
    path.write_text(yaml.safe_dump(REGISTRY, sort_keys=False))
    return path


def test_a_live_board_is_stamped_and_kept(registry) -> None:
    kept, retired = record(str(registry), RESULTS, today="2026-08-31")

    assert (kept, retired) == (2, 1)
    seeds = {s.slug: s for s in load_seed(str(registry))}
    assert seeds["alive"].checked == "2026-08-31"
    assert seeds["alive"].state == "api"


def test_a_dead_board_is_retired_with_its_evidence_not_deleted(registry) -> None:
    """CLAUDE.md claimed this since the first sweep; it never happened.

    A slug that 404s today may be a rename rather than a departure, and the
    statuses are what tell those apart a year later.
    """
    record(str(registry), RESULTS, today="2026-08-31")
    raw = yaml.safe_load(registry.read_text())

    assert [e["slug"] for e in raw["companies"]] == ["alive", "untouched"]
    assert len(raw["retired"]) == 1
    gone = raw["retired"][0]
    assert gone["slug"] == "gone"
    assert gone["api_status"] == 404
    assert gone["rendered_status"] == 404
    assert gone["checked"] == "2026-08-31"


def test_a_retired_board_is_not_polled(registry) -> None:
    """`load_seed` reads `companies:` only, so retiring stops the crawl."""
    record(str(registry), RESULTS, today="2026-08-31")

    assert "gone" not in {s.slug for s in load_seed(str(registry))}


def test_an_entry_not_in_this_run_keeps_what_it_had(registry) -> None:
    """A partial sweep must not stamp entries it never fetched.

    Otherwise a run over ten companies would mark the other hundred as checked
    today, which is the opposite of what this exists to record.
    """
    record(str(registry), RESULTS, today="2026-08-31")
    seeds = {s.slug: s for s in load_seed(str(registry))}

    assert seeds["untouched"].checked is None
    assert seeds["untouched"].state is None


def test_the_real_registry_reports_itself_as_unvalidated() -> None:
    """The number this whole change exists to make visible."""
    seeds = load_seed("seeds/companies.yaml")
    assert seeds, "the registry should not be empty"
    # No claim about how many — only that "never checked" is now answerable
    # from the file rather than from memory.
    assert all(isinstance(s.checked, str) or s.checked is None for s in seeds)
