"""The feed can be asked for one kind of role, by a dropdown rather than a guess.

`roles.py` has known since it was written that `SDE II` and `Member of
Technical Staff` are the same job as `Software Engineer`. The only way to use
that from the feed was to type the role into the keywords box, where it is one
term among several and matches a posting whose *description* merely mentions
it — a Product Manager posting that says "work with software engineers" is a
hit. After the 3,521-company run on 2026-10-05 the feed held 3,150 postings,
500 of them with a title the alias table recognises, and no way to ask for
those by role.

`role` is that question. It reads the title only, through the same table the
keyword path and the scorer use, and a title naming no recognised role is not
a match for any of them (§16: an unknown is never satisfied).
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models import Match, Posting
from packages.core.schemas_search import FILTER_KEYS
from packages.matching.roles import ROLE_ALIASES
from packages.matching.search import ROLE_FILTERS, SearchFilters, matches

NOW = datetime(2026, 10, 5, tzinfo=UTC)
WEB = Path(__file__).resolve().parents[1] / "apps/web/src/app/matches"


def _posting(title: str, description: str = "") -> Posting:
    return Posting(
        url="https://example.test/job",
        title=title,
        location=None,
        description_raw=description,
        first_seen_at=NOW,
        closed_at=None,
    )


def _verdict(title: str, description: str = "", **filters: object):
    return matches(_posting(title, description), SearchFilters(**filters))


# --- the question -----------------------------------------------------------


@pytest.mark.parametrize(
    "title",
    [
        "Software Engineer",
        "Senior Software Engineer, Payments",
        "SDE II",
        "Member of Technical Staff",
    ],
)
def test_one_role_under_its_several_titles_is_kept(title: str) -> None:
    assert _verdict(title, role="software_engineer").kept


def test_a_neighbouring_role_is_not_the_role_asked_for() -> None:
    """The line `roles.py` exists to hold: an embedding reads these as one."""
    verdict = _verdict("Data Scientist", role="data_engineer")

    assert not verdict.kept
    assert "title names a different role (data scientist)" in verdict.reasons


@pytest.mark.parametrize("title", ["Product Manager", "Account Executive", ""])
def test_a_title_naming_no_recognised_role_is_not_a_match(title: str) -> None:
    """An unknown is never satisfied (§16), and the hidden posting says why."""
    verdict = _verdict(title, role="software_engineer")

    assert not verdict.kept
    assert "title names no recognised role" in verdict.reasons


def test_the_title_decides_not_the_description() -> None:
    """What the keywords box cannot do: this posting *mentions* the role."""
    described = "You will work closely with every software engineer on the team."

    assert _verdict("Product Manager", described, keywords=("software engineer",)).kept
    assert not _verdict("Product Manager", described, role="software_engineer").kept


def test_no_role_asked_for_keeps_every_title() -> None:
    assert _verdict("Product Manager").kept
    assert _verdict("").kept


def test_the_filter_describes_itself() -> None:
    assert "role: AI / ML engineer" in SearchFilters(role="machine_learning_engineer").describe()


# --- one vocabulary, in three places ---------------------------------------


def test_the_vocabulary_is_the_alias_table() -> None:
    """Not a second list: a role added to `roles.py` is askable the same day."""
    assert tuple(ROLE_ALIASES) == ROLE_FILTERS


def test_the_dropdown_offers_exactly_the_roles_the_filter_knows() -> None:
    """A hard-coded option list is a third copy, and the one that would drift.

    An option the API refuses is a 400 on a click; a role the table knows and
    the dropdown omits is a filter that exists and cannot be asked for — the
    defect §15 records for `citizenship_status` and `target_seniority`.
    """
    bar = (WEB / "filter-bar.tsx").read_text()
    block = re.search(r"const ROLES[^=]*=\s*\[(.*?)\];", bar, re.S)
    assert block is not None, "the filter bar no longer has a ROLES list to read"

    offered = set(re.findall(r'value:\s*"([a-z_]+)"', block.group(1)))

    assert offered == set(ROLE_ALIASES)


def test_the_control_reaches_the_api() -> None:
    """The bar writes the key, the page forwards it, a saved search may hold it."""
    bar = (WEB / "filter-bar.tsx").read_text()
    page = (WEB / "page.tsx").read_text()
    forwarded = re.search(r"const FILTER_KEYS = \[(.*?)\] as const;", page, re.S)
    assert forwarded is not None

    assert re.search(r'\bset\(\s*"role"', bar), "the role control is not in the filter bar"
    assert '"role"' in forwarded.group(1), "the page does not forward role to the API"
    assert "role" in FILTER_KEYS, "a saved search would refuse role"


# --- through the API --------------------------------------------------------


@pytest.fixture
async def feed(client: AsyncClient, worker_session: AsyncSession, complete_candidate) -> str:
    profile_id = uuid.UUID(complete_candidate["profile_id"])
    titles = ("SDE II", "Data Scientist", "Product Manager")
    postings = [
        Posting(url=f"https://boards.greenhouse.io/acme/jobs/{i}", title=title)
        for i, title in enumerate(titles)
    ]
    worker_session.add_all(postings)
    await worker_session.flush()
    worker_session.add_all(
        Match(
            profile_id=profile_id,
            posting_id=posting.id,
            score=0.5,
            reasons_json={"excluded_by": []},
        )
        for posting in postings
    )
    await worker_session.commit()
    return str(profile_id)


async def test_the_feed_answers_by_role(client: AsyncClient, feed: str) -> None:
    rows = (
        await client.get(
            "/matches",
            params={"profile_id": feed, "role": "software_engineer", "us_only": "false"},
        )
    ).json()

    assert [row["title"] for row in rows] == ["SDE II"]


async def test_an_unknown_role_is_refused_rather_than_ignored(
    client: AsyncClient, feed: str
) -> None:
    """A vocabulary, not free text: a typo must not read as "no preference"."""
    response = await client.get("/matches", params={"profile_id": feed, "role": "wizard"})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"
    assert "software_engineer" in response.json()["error"]["message"]
