"""An explicitly named Bay Area narrows before pools and counts are cut."""

import pytest

from apps.api.routers.chat import _postings_section
from packages.core.config import get_settings
from packages.matching import retrieve as module
from packages.matching.retrieve import retrieve
from tests.test_retrieval_area import _company, _posting


@pytest.fixture(autouse=True)
def settings(monkeypatch):
    monkeypatch.setenv("SEARCH_US_ONLY", "true")
    monkeypatch.setenv("SEARCH_REMOTE_OUTSIDE_CALIFORNIA", "true")
    monkeypatch.setattr(module, "get_reranker", lambda: None)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def corpus(session, title="Data Engineer"):
    company = await _company(session)
    locations = (
        "San Francisco, CA",
        "San Jose, CA",
        "Los Angeles, CA",
        "Remote, US",
        "Austin, TX",
        "London, UK",
    )
    return [
        await _posting(session, company, place, title, "Build Kafka pipelines.")
        for place in locations
    ]


async def test_role_pool_and_counts_are_in_the_bay_area(db_session):
    posts = await corpus(db_session)
    found = await retrieve(db_session, "show me data engineer jobs in the bay area")
    assert {p.posting_id for p in (*found.passages, *found.more)} == {p.id for p in posts[:2]}
    assert found.role_total == 2
    assert sum(n for _, n in found.kinds) == 2
    assert found.outside_area == 2  # Not confused with the narrower place.
    assert found.place_total == 2
    assert found.search_area_total == 4
    assert found.remote_or_unplaced_not_listed == 1
    context = _postings_section(found)
    assert "in the Bay Area: 2" in context
    assert "1 remote or unplaced roles in your search area are not listed" in context


async def test_keyword_total_filters_the_full_match_set_before_the_cut(db_session):
    company = await _company(db_session)
    for index in range(130):
        await _posting(db_session, company, "Los Angeles, CA", f"Kafka Specialist {index}", "Kafka")
    here = await _posting(db_session, company, "San Mateo, CA", "Specialist", "Kafka")
    await _posting(db_session, company, "Remote, US", "Specialist", "Kafka")
    found = await retrieve(db_session, "jobs in the bay area using Kafka")
    assert [p.posting_id for p in found.passages] == [here.id]
    assert found.match_total == 1
    assert found.company_total == 1
    assert found.search_area_total == 132
    assert found.remote_or_unplaced_not_listed == 1


async def test_no_place_and_abroad_keep_their_existing_rules(db_session):
    posts = await corpus(db_session)
    found = await retrieve(db_session, "data engineer jobs")
    assert {p.posting_id for p in found.passages} == {p.id for p in posts[:4]}
    assert found.role_total == 4
    assert found.place_total is None
    abroad = await retrieve(db_session, "data engineer jobs in London")
    assert abroad.area_waived and abroad.role_total == 6


async def test_empty_place_reports_the_search_area_total(db_session):
    company = await _company(db_session)
    await _posting(db_session, company, "Remote, US", "Data Engineer", "Kafka")
    found = await retrieve(db_session, "data engineer jobs in the bay area")
    assert not found.passages and not found.more
    assert found.role_total == 0
    context = _postings_section(found)
    assert "none in the Bay Area" in context
    assert "1 matching postings in your search area" in context


async def test_named_city_keeps_the_region_and_ranks_the_city_first(db_session):
    posts = await corpus(db_session, "Software Engineer")
    found = await retrieve(db_session, "software engineer jobs in San Francisco")
    assert found.passages[0].posting_id == posts[0].id
    assert {p.posting_id for p in (*found.passages, *found.more)} == {p.id for p in posts[:2]}


async def test_multiple_offices_and_remote_prose_use_the_shared_place_reading(db_session):
    company = await _company(db_session)
    here = await _posting(
        db_session, company, "Toronto, ON; San Francisco, CA", "Engineer", "Kafka"
    )
    await _posting(db_session, company, "Austin, TX", "Engineer", "Kafka. A fully remote role.")
    await _posting(
        db_session, company, "Los Angeles, CA", "Engineer", "Kafka. A fully remote role."
    )
    found = await retrieve(db_session, "Kafka jobs in the bay area")
    assert [p.posting_id for p in found.passages] == [here.id]
    assert found.match_total == 1 and found.search_area_total == 3
    assert found.remote_or_unplaced_not_listed == 2


async def test_company_only_total_is_narrowed(db_session):
    await corpus(db_session, "Specialist")
    found = await retrieve(db_session, "jobs at Acme in the bay area")
    assert found.match_total == 2
    assert found.remote_or_unplaced_not_listed == 1


async def test_company_with_only_us_remote_jobs_reports_none_in_the_bay_area(db_session):
    company = await _company(db_session, "Acme")
    await _posting(db_session, company, "Remote, US", "Specialist", "Kafka")
    found = await retrieve(db_session, "jobs at Acme in the Bay Area")
    assert found.companies == ("Acme",)
    assert not found.passages and found.match_total == 0
    assert found.remote_or_unplaced_not_listed == 1
    context = _postings_section(found)
    assert "none in the Bay Area" in context
    assert "1 remote or unplaced roles in your search area are not listed" in context


@pytest.mark.parametrize("location", [None, "", "Uncatalogued Town", "Hybrid"])
async def test_unplaced_roles_are_counted_when_the_place_filter_leaves_them_out(
    db_session, location
):
    company = await _company(db_session)
    here = await _posting(db_session, company, "San Jose, CA", "Specialist", "Kafka")
    await _posting(db_session, company, location, "Specialist", "Kafka")
    ordinary = await retrieve(db_session, "Kafka jobs")
    assert ordinary.match_total == 2
    found = await retrieve(db_session, "Kafka jobs in the bay area")
    assert [p.posting_id for p in found.passages] == [here.id]
    assert found.match_total == 1
    assert found.search_area_total == 2
    assert found.remote_or_unplaced_not_listed == 1
    assert "remote or unplaced roles" in _postings_section(found)


async def test_place_alone_is_a_real_search(db_session):
    posts = await corpus(db_session, "Specialist")
    found = await retrieve(db_session, "jobs in the bay area")
    assert {p.posting_id for p in found.passages} == {p.id for p in posts[:2]}
    assert found.place_total == 2 and found.search_area_total == 4
    assert found.place_counts_complete


async def test_title_pool_respects_the_place(db_session):
    from tests.test_chat_title_words import _common

    await _common(db_session, "security")
    posts = await corpus(db_session, "Security Specialist")
    found = await retrieve(db_session, "security jobs in the bay area")
    assert {p.posting_id for p in found.passages} == {p.id for p in posts[:2]}
    assert found.title_total == 2 and found.place_total == 2


async def test_vector_pool_filters_before_the_cut_without_claiming_a_full_count(db_session):
    from tests.test_chat_title_words import _common

    await _common(db_session, "kafka", "san", "francisco")
    company = await _company(db_session)
    here = await _posting(db_session, company, "San Jose, CA", "Specialist", "Kafka", embedded=True)
    await _posting(db_session, company, "Remote, US", "Specialist", "Kafka", embedded=True)
    found = await retrieve(db_session, "Kafka jobs in San Francisco")
    assert [p.posting_id for p in found.passages] == [here.id]
    assert found.remote_or_unplaced_not_listed == 1
    assert not found.place_counts_complete
    assert "among retrieved candidates" in _postings_section(found)


async def test_level_breakdown_is_of_the_filtered_region(db_session):
    company = await _company(db_session)
    await _posting(db_session, company, "San Jose, CA", "Junior Data Engineer", "Kafka")
    await _posting(db_session, company, "San Jose, CA", "Senior Data Engineer", "Kafka")
    await _posting(db_session, company, "Remote, US", "Junior Data Engineer", "Kafka")
    found = await retrieve(db_session, "junior data engineer jobs in the Bay Area")
    assert found.role_total == 1
    assert found.level.at_level == 1 and found.level.other == 1
    assert found.remote_or_unplaced_not_listed == 1


async def test_chat_exposes_the_filter_and_omissions(client, worker_session, monkeypatch):
    from tests.test_chat_skill_gaps import _ask

    company = await _company(worker_session)
    await _posting(worker_session, company, "San Jose, CA", "Data Engineer", "Kafka")
    await _posting(worker_session, company, None, "Data Engineer", "Kafka")
    await _posting(worker_session, company, "Remote, US", "Data Engineer", "Kafka")
    await worker_session.commit()
    reply, seen = await _ask(client, monkeypatch, "data engineer jobs in the Bay Area")
    assert reply["place_filter"] == "Bay Area"
    assert reply["place_matched_total"] == reply["postings_matched_total"] == 1
    assert reply["place_counts_complete"] is True
    assert reply["search_area_matched_total"] == 3
    assert reply["remote_or_unplaced_not_listed"] == 2
    assert "2 remote or unplaced roles" in seen["user"]
