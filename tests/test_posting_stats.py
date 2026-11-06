"""Counts and shares of open postings by role, title, place and work mode.

`posting_stats` is what a model asks for a number (the MCP tool, and later the
assistant), so every figure it returns is a count of rows. These tests pin
what each filter reads: California is an office in California, a role is the
role table, a title is its role words, remote is `locality.reads_as_remote`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from packages.core.config import get_settings
from packages.core.models import Company, Posting
from packages.matching import posting_stats as stats_module
from packages.matching.locality import remote_by_metadata
from packages.matching.posting_stats import posting_stats
from packages.matching.retrieve import _open_area_total
from packages.matching.roles import display_name
from packages.matching.stats_schema import MAX_ROLE_NAMES, GroupBy, Place, StatsRequest, WorkMode

REMOTE_BODY = "This role is fully remote."


async def posting(
    session,
    title: str,
    location: str | None,
    *,
    body: str = "A role.",
    closed: bool = False,
    company: Company | None = None,
) -> Posting:
    if company is None:
        company = Company(name="ExampleCo " + uuid.uuid4().hex[:8], ats_type="greenhouse")
        session.add(company)
        await session.flush()
    row = Posting(
        company_id=company.id,
        title=title,
        location=location,
        url="https://jobs.example.test/" + uuid.uuid4().hex,
        description_raw=body,
        content_hash=uuid.uuid4().hex,
        closed_at=datetime.now(UTC) if closed else None,
    )
    session.add(row)
    await session.flush()
    return row


async def california_mix(session) -> None:
    """Three California jobs, and four that must not count as California."""
    await posting(session, "Data Engineer", "San Francisco, CA")
    await posting(session, "Senior Data Engineer", "Los Angeles, CA")
    await posting(session, "Software Engineer", "San Diego, CA")
    await posting(session, "Data Engineer", "Austin, TX")
    await posting(session, "Data Engineer", "Remote")
    await posting(session, "Data Engineer", "Toronto, ON, CA")
    await posting(session, "Data Engineer", "Oakland, CA", closed=True)


# --------------------------------------------------------------------------
# Place
# --------------------------------------------------------------------------


async def test_share_of_a_role_among_california_jobs(db_session):
    # Arrange
    await california_mix(db_session)

    # Act
    stats = await posting_stats(
        db_session, StatsRequest(place=Place.CALIFORNIA, share_roles=("data engineer",))
    )

    # Assert
    assert stats.total == 3
    assert stats.matching == 2
    assert stats.percentage == 66.67
    assert "California" in stats.scope


async def test_count_of_a_role_in_california(db_session):
    await california_mix(db_session)

    stats = await posting_stats(
        db_session, StatsRequest(place=Place.CALIFORNIA, roles=("data engineer",))
    )

    assert stats.total == 2
    assert stats.matching is None
    assert stats.percentage is None


async def test_any_california_office_places_a_multi_office_posting(db_session):
    await posting(db_session, "Data Engineer", "Austin, TX; San Jose, CA")
    await posting(db_session, "Data Engineer", "Austin, TX; Denver, CO")

    stats = await posting_stats(db_session, StatsRequest(place=Place.CALIFORNIA))

    assert stats.total == 1


async def test_title_places_a_job_only_when_the_location_names_no_place(db_session):
    await posting(db_session, "Data Engineer (San Francisco)", "Hybrid")
    await posting(db_session, "Data Engineer (San Francisco)", "Austin, TX")

    stats = await posting_stats(db_session, StatsRequest(place=Place.CALIFORNIA))

    assert stats.total == 1


async def test_everywhere_counts_every_open_posting(db_session):
    await california_mix(db_session)

    stats = await posting_stats(db_session, StatsRequest(place=Place.EVERYWHERE))

    assert stats.total == 6


async def test_search_area_keeps_remote_and_california_and_drops_the_rest(db_session):
    await california_mix(db_session)

    stats = await posting_stats(db_session, StatsRequest())

    # San Francisco, Los Angeles, San Diego and Remote. Not Austin on-site, not Toronto.
    assert stats.total == 4
    assert "search area" in stats.scope


async def test_search_area_total_agrees_with_the_assistant(db_session):
    """One area rule: the count the assistant divides by is the count here."""
    # Arrange: Denver is on-site by its location and remote by its description.
    await california_mix(db_session)
    await posting(db_session, "Data Engineer", "Denver, CO", body=REMOTE_BODY)
    await posting(db_session, "Analyst", "Denver, CO")

    # Act
    stats = await posting_stats(db_session, StatsRequest())

    # Assert
    assert stats.total == await _open_area_total(db_session, area=True) == 5


@pytest.mark.parametrize(
    ("env", "total", "words"),
    [
        ({"SEARCH_US_ONLY": "false"}, 6, "no location limit"),
        ({"SEARCH_US_ONLY": "true", "SEARCH_REMOTE_OUTSIDE_CALIFORNIA": "false"}, 5, "United"),
    ],
)
async def test_search_area_follows_the_owner_settings(db_session, monkeypatch, env, total, words):
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    try:
        await california_mix(db_session)

        stats = await posting_stats(db_session, StatsRequest())

        # Off: Austin and Toronto too. US only, on-site anywhere: Austin, not Toronto.
        assert stats.total == total
        assert words in stats.scope
    finally:
        get_settings.cache_clear()


async def test_a_bay_area_city_counts_only_its_own_offices(db_session):
    await posting(db_session, "Data Engineer", "San Francisco, CA")
    await posting(db_session, "Data Engineer", "Oakland, CA")
    await posting(db_session, "Data Engineer", "Los Angeles, CA")

    stats = await posting_stats(
        db_session, StatsRequest(place=Place.BAY_AREA, cities=("San Francisco",))
    )
    whole_area = await posting_stats(db_session, StatsRequest(place=Place.BAY_AREA))

    assert stats.total == 1
    assert "San Francisco" in stats.scope
    assert whole_area.total == 2


# --------------------------------------------------------------------------
# Work mode
# --------------------------------------------------------------------------


@pytest.mark.parametrize("batch", [1, stats_module.DESCRIPTION_BATCH])
async def test_remote_share_of_a_role_reads_the_description_when_needed(
    db_session, monkeypatch, batch
):
    # Arrange: two remote data engineer jobs, one only by its description.
    monkeypatch.setattr(stats_module, "DESCRIPTION_BATCH", batch)
    await posting(db_session, "Data Engineer", "San Francisco, CA")
    await posting(db_session, "Data Engineer", "Remote")
    await posting(db_session, "Data Engineer", "Denver, CO", body=REMOTE_BODY)
    await posting(db_session, "Data Engineer", "Austin, TX")
    await posting(db_session, "Software Engineer", "Remote")

    # Act
    stats = await posting_stats(
        db_session,
        StatsRequest(roles=("data engineer",), share_work_mode=WorkMode.REMOTE),
    )

    # Assert: Austin is on-site in another state, so outside the search area.
    assert stats.total == 3
    assert stats.matching == 2
    assert stats.percentage == 66.67


async def test_not_remote_counts_the_rest(db_session):
    await posting(db_session, "Data Engineer", "San Francisco, CA")
    await posting(db_session, "Data Engineer", "Hybrid - Oakland, CA")
    await posting(db_session, "Data Engineer", "Remote")

    stats = await posting_stats(
        db_session, StatsRequest(place=Place.EVERYWHERE, work_mode=WorkMode.NOT_REMOTE)
    )

    assert stats.total == 2


def test_metadata_decides_remote_only_where_it_says_so():
    assert remote_by_metadata(title="Data Engineer", location="Remote") is True
    assert remote_by_metadata(title="Data Engineer", location="Hybrid - San Jose") is False
    assert remote_by_metadata(title="Data Engineer (Not Remote)", location="Austin, TX") is False
    assert remote_by_metadata(title="Data Engineer", location="San Francisco, CA") is None


# --------------------------------------------------------------------------
# Titles, and breakdowns
# --------------------------------------------------------------------------


async def test_title_words_filter_jobs_the_role_table_does_not_know(db_session):
    await posting(db_session, "Senior Product Manager, Payments", "San Francisco, CA")
    await posting(db_session, "Group Product Manager", "Los Angeles, CA")
    await posting(db_session, "Product Marketing Manager", "San Diego, CA")
    await posting(db_session, "Account Executive", "San Diego, CA")

    stats = await posting_stats(
        db_session, StatsRequest(place=Place.CALIFORNIA, share_title="Product Manager")
    )

    assert stats.total == 4
    assert stats.matching == 2
    assert stats.percentage == 50.0


async def test_breakdown_by_role_counts_titles_the_table_does_not_know(db_session):
    await california_mix(db_session)
    await posting(db_session, "Barista", "Fresno, CA")

    stats = await posting_stats(
        db_session, StatsRequest(place=Place.CALIFORNIA, group_by=GroupBy.ROLE)
    )

    assert stats.total == 4
    assert [(group.label, group.count, group.percentage) for group in stats.groups] == [
        (display_name("data_engineer"), 2, 50.0),
        (display_name("software_engineer"), 1, 25.0),
    ]
    assert stats.unclassified == 1
    assert stats.other == 0


async def test_breakdown_by_title_reads_every_title(db_session):
    await posting(db_session, "Senior Account Executive", "San Francisco, CA")
    await posting(db_session, "Account Executive II", "San Diego, CA")
    await posting(db_session, "Barista", "Fresno, CA")

    stats = await posting_stats(
        db_session, StatsRequest(place=Place.CALIFORNIA, group_by=GroupBy.TITLE)
    )

    assert [(group.label, group.count) for group in stats.groups] == [
        ("account executive", 2),
        ("barista", 1),
    ]
    assert stats.unclassified == 0


async def test_breakdown_keeps_the_top_groups_and_counts_the_rest(db_session):
    companies = []
    for name in ("Alpha", "Beta", "Gamma"):
        company = Company(name=f"{name} {uuid.uuid4().hex[:6]}", ats_type="greenhouse")
        db_session.add(company)
        companies.append(company)
    await db_session.flush()
    for company, count in zip(companies, (3, 2, 1), strict=True):
        for _ in range(count):
            await posting(db_session, "Data Engineer", "San Francisco, CA", company=company)

    stats = await posting_stats(
        db_session, StatsRequest(place=Place.CALIFORNIA, group_by=GroupBy.COMPANY, top=2)
    )

    assert [group.count for group in stats.groups] == [3, 2]
    assert stats.groups[0].label == companies[0].name
    assert stats.other == 1


async def test_no_postings_gives_no_percentage(db_session):
    stats = await posting_stats(
        db_session, StatsRequest(place=Place.CALIFORNIA, share_roles=("data engineer",))
    )

    assert stats.total == 0
    assert stats.matching == 0
    assert stats.percentage is None


# --------------------------------------------------------------------------
# What a request may say
# --------------------------------------------------------------------------


def test_role_names_resolve_to_the_role_table():
    request = StatsRequest(roles=("ML engineer", "data_engineer", "Data Engineer"))

    assert request.roles == ("machine_learning_engineer", "data_engineer")


@pytest.mark.parametrize(
    "fields",
    [
        {"roles": ("underwater basket weaver",)},
        {"share_roles": ("chef",)},
        {"cities": ("Fresno",), "place": Place.BAY_AREA},
        {"cities": ("San Francisco",)},
        {"title": "Senior"},
        {"top": 0},
        {"top": 51},
        {"place": "texas"},
        {"unknown_field": 1},
        {"roles": (123,)},
        {"roles": ("data engineer",) * (MAX_ROLE_NAMES + 1)},
        {"roles": ("data engineer " + "x" * 100,)},
        {"cities": ("San Jose",) * 11, "place": Place.BAY_AREA},
        {"roles": ("data engineer",), "share_roles": ("software engineer",)},
    ],
)
def test_a_request_the_function_cannot_answer_is_refused(fields):
    with pytest.raises(ValidationError):
        StatsRequest(**fields)
