"""Exact totals only for one keyword or an employer with no extra subject."""

from datetime import UTC, datetime

import pytest

from packages.core.config import get_settings
from packages.matching.retrieve import retrieve
from tests.test_chat_skill_gaps import _ask
from tests.test_chat_title_words import _company, _posting


@pytest.fixture(autouse=True)
def area(monkeypatch):
    monkeypatch.setenv("SEARCH_US_ONLY", "true")
    monkeypatch.setenv("SEARCH_REMOTE_OUTSIDE_CALIFORNIA", "true")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def _corpus(session):
    company = await _company(session, "Cloudflare")
    other = await _company(session, "Acme")
    posts = []
    for index in range(7):
        p = await _posting(
            session,
            company if index < 6 else other,
            f"Platform Specialist {index}",
            "Kafka and Rust.",
        )
        p.location = "San Francisco, CA"
        posts.append(p)
    for location in ("London, UK", "Austin, TX"):
        p = await _posting(session, company, "Platform Specialist", "Kafka.")
        p.location = location
    p = await _posting(session, other, "Platform Specialist", "Kafkaverse.")
    p.location = "San Francisco, CA"
    p = await _posting(session, company, "Platform Specialist", "Kafka.")
    p.closed_at = datetime.now(UTC)
    await session.flush()
    return posts


async def test_one_term_counts_all_matches_and_employers(db_session):
    posts = await _corpus(db_session)
    found = await retrieve(db_session, "jobs using Kafka")
    assert found.match_total == 7
    assert found.company_total == 2
    assert found.matched_term.lower() == "kafka"
    assert len(found.passages) == 5
    assert {p.posting_id for p in (*found.passages, *found.more)} == {p.id for p in posts}


@pytest.mark.parametrize("area_enabled", [True, False])
async def test_total_is_not_capped_by_the_ranked_pool(db_session, monkeypatch, area_enabled):
    monkeypatch.setenv("SEARCH_US_ONLY", str(area_enabled).lower())
    get_settings.cache_clear()
    company = await _company(db_session, "Acme")
    for i in range(160):
        p = await _posting(db_session, company, f"Platform Specialist {i}", "Kafka.")
        p.location = "San Francisco, CA"
    await db_session.flush()
    found = await retrieve(db_session, "jobs using Kafka")
    assert found.match_total == 160
    assert found.company_total == 1
    assert len(found.passages) == 5
    # Counting the full set does not expand the existing ranked list.
    assert len(found.more) == 25


async def test_totals_read_bodies_only_for_on_site_out_of_state_matches(db_session, monkeypatch):
    from sqlalchemy import select

    from packages.core.models import Posting
    from packages.matching import retrieve as module

    await _corpus(db_session)
    expected = set(
        (await db_session.scalars(select(Posting.id).where(Posting.location == "Austin, TX"))).all()
    )
    original = module._matching_totals
    seen = []
    body_ids = []

    async def count(*args, **kwargs):
        execute = db_session.execute

        async def metadata_only(statement, *values, **options):
            columns = {column.name for column in statement.selected_columns}
            seen.append(columns)
            assert "url" not in columns
            if "description_raw" in columns:
                params = statement.compile().params
                body_ids.extend(
                    value
                    for values in params.values()
                    if isinstance(values, list)
                    for value in values
                )
            return await execute(statement, *values, **options)

        with monkeypatch.context() as patch:
            patch.setattr(db_session, "execute", metadata_only)
            return await original(*args, **kwargs)

    monkeypatch.setattr(module, "_matching_totals", count)
    found = await retrieve(db_session, "jobs using Kafka")
    assert found.match_total == 7
    assert seen
    assert set(body_ids) == expected


async def test_remote_body_keeps_an_out_of_state_posting_in_the_total(db_session):
    company = await _company(db_session, "Acme")
    p = await _posting(
        db_session,
        company,
        "Platform Specialist",
        "Kafka. This is a fully remote position; work from anywhere in the United States.",
    )
    p.location = "Austin, TX"
    await db_session.flush()
    found = await retrieve(db_session, "jobs using Kafka")
    assert found.match_total == 1
    assert found.outside_area == 0


async def test_same_metadata_can_have_different_remote_body_verdicts(db_session):
    company = await _company(db_session, "Acme")
    for description in (
        "Kafka. This is a fully remote position; work from anywhere in the United States.",
        "Kafka. Work in our office every weekday.",
    ):
        p = await _posting(db_session, company, "Platform Specialist", description)
        p.location = "Austin, TX"
    await db_session.flush()
    found = await retrieve(db_session, "jobs using Kafka")
    assert found.match_total == 1
    assert found.company_total == 1
    assert found.outside_area == 1


async def test_remote_body_does_not_override_an_explicit_on_site_title(db_session):
    company = await _company(db_session, "Acme")
    p = await _posting(
        db_session,
        company,
        "Platform Specialist (On-site)",
        "Kafka. This is a fully remote position; work from anywhere in the United States.",
    )
    p.location = "Austin, TX"
    await db_session.flush()
    found = await retrieve(db_session, "jobs using Kafka")
    assert found.match_total == 0
    assert found.outside_area == 1


async def test_remote_body_count_agrees_with_ranked_sources(client, worker_session, monkeypatch):
    company = await _company(worker_session, "Acme")
    p = await _posting(
        worker_session,
        company,
        "Platform Specialist",
        "Kafka. This is a fully remote position; work from anywhere in the United States.",
    )
    p.location = "Austin, TX"
    await worker_session.commit()
    body, seen = await _ask(client, monkeypatch, "jobs using Kafka")
    assert len(body["sources"]) == 1
    assert body["postings_matched_total"] == 1
    assert "closest 1 of 0" not in seen["user"]
    assert "outside it left out" not in seen["user"]
    assert "1 postings at 1 companies" in seen["user"]


async def test_company_only_counts_in_area_open_postings(db_session):
    await _corpus(db_session)
    found = await retrieve(db_session, "What jobs does Cloudflare have open?")
    assert found.match_total == 6
    assert found.outside_area == 2
    assert found.matched_term is None
    assert len(found.more) == 1


async def test_right_now_is_a_phrase_not_an_extra_search_subject(db_session):
    await _corpus(db_session)
    found = await retrieve(db_session, "What jobs does Cloudflare have open right now?")
    ordinary = await retrieve(db_session, "What jobs does Cloudflare have open?")
    assert found.match_total == 6
    assert [p.posting_id for p in (*found.passages, *found.more)] == [
        p.posting_id for p in (*ordinary.passages, *ordinary.more)
    ]


async def test_right_to_work_is_still_searched_for(db_session):
    from tests.test_chat_title_words import _common

    await _common(db_session, "work")
    company = await _company(db_session, "Acme")
    wanted = await _posting(
        db_session, company, "Platform Specialist", "Must have the right to work."
    )
    await _posting(db_session, company, "Platform Specialist", "Interesting work.")
    found = await retrieve(db_session, "jobs with right to work")
    assert [p.posting_id for p in found.passages] == [wanted.id]


@pytest.mark.parametrize("question", ["jobs using Kafka Rust", "jobs at Cloudflare using Kafka"])
async def test_other_searches_have_no_total(db_session, question):
    await _corpus(db_session)
    found = await retrieve(db_session, question)
    assert found.match_total is None
    assert found.company_total is None


async def test_missing_word_gets_zero_not_vector_fallback(db_session):
    await _corpus(db_session)
    found = await retrieve(db_session, "jobs using Ziggurat")
    assert found.match_total == 0 and found.company_total == 0
    assert not found.passages and not found.more


async def test_route_exposes_totals_and_scope(client, worker_session, monkeypatch):
    await _corpus(worker_session)
    await worker_session.commit()
    body, seen = await _ask(client, monkeypatch, "jobs using Kafka")
    assert body["postings_matched_total"] == 7
    assert body["companies_matched_total"] == 2
    assert body["matched_term"].lower() == "kafka"
    assert len(body["more_sources"]) == 2
    assert "7 postings at 2 companies" in seen["user"]
    assert "closest 5 retrieved postings" in seen["user"]
    assert "mentions, not requirements" in seen["user"]
    assert "give no total" not in seen["user"]


async def test_company_total_reaches_context_and_reply(client, worker_session, monkeypatch):
    await _corpus(worker_session)
    await worker_session.commit()
    body, seen = await _ask(client, monkeypatch, "jobs at Cloudflare")
    assert body["postings_matched_total"] == 6
    assert body["matched_companies"] == ["Cloudflare"]
    assert "Cloudflare's 6 open postings in the search area" in seen["user"]


async def test_multi_term_context_still_refuses_a_total(client, worker_session, monkeypatch):
    await _corpus(worker_session)
    await worker_session.commit()
    _, seen = await _ask(client, monkeypatch, "jobs using Kafka Rust")
    assert "give no total" in seen["user"]


async def test_counts_follow_the_disabled_search_area(db_session, monkeypatch):
    await _corpus(db_session)
    monkeypatch.setenv("SEARCH_US_ONLY", "false")
    get_settings.cache_clear()
    found = await retrieve(db_session, "jobs using Kafka")
    assert found.match_total == 9
    assert found.outside_area == 0


async def test_total_uses_foreign_place_in_title_when_location_is_silent(db_session):
    company = await _company(db_session, "Acme")
    for title in ("Platform Specialist, India", "Platform Specialist"):
        p = await _posting(db_session, company, title, "Kafka.")
        p.location = "Remote"
    await db_session.flush()
    found = await retrieve(db_session, "jobs using Kafka")
    assert found.match_total == 1
    assert found.outside_area == 1


def test_dock_reads_and_labels_term_and_company_totals():
    from pathlib import Path

    dock = Path("apps/web/src/components/assistant.tsx").read_text()
    assert "body.companies_matched_total" in dock
    assert "body.matched_term" in dock and "body.matched_companies" in dock
    labels = Path("apps/web/src/lib/chat-counts.ts").read_text()
    assert "countedMatches(turn)" in dock
    assert "mentions do not necessarily mean required skills" in labels
