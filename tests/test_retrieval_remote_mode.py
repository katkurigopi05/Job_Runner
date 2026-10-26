"""An explicit remote work-mode request filters evidence before pools/counts."""

from datetime import UTC, datetime

import pytest

from packages.core.config import get_settings
from packages.matching import retrieve as module
from tests.test_retrieval_area import _company, _posting


@pytest.fixture(autouse=True)
def settings(monkeypatch):
    monkeypatch.setenv("SEARCH_US_ONLY", "true")
    monkeypatch.setenv("SEARCH_REMOTE_OUTSIDE_CALIFORNIA", "true")
    monkeypatch.setattr(module, "get_reranker", lambda: None)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.mark.parametrize(
    "question",
    [
        "remote jobs",
        "remote Kafka jobs",
        "data engineer jobs that allow remote work",
        "remote data engineer jobs",
        "remote senior software engineer jobs",
        "jobs that are remote",
        "data engineer jobs, remote only",
        "WFH data engineer jobs",
        "jobs offering work from home",
    ],
)
def test_explicit_work_mode_is_recognized(question):
    assert module._remote_work_spans(question)


@pytest.mark.parametrize(
    "question",
    [
        "remote sensing engineer jobs",
        "jobs using remote procedure calls",
        "software engineer jobs using remote desktop tooling",
        "jobs involving remote access",
        "jobs using remote-only access",
        "not remote data engineer jobs",
        "non-remote jobs",
        "no remote jobs",
        "data engineer jobs",
        "right to work jobs",
    ],
)
def test_technical_negated_and_absent_mode_stay_lexical(question):
    assert module._remote_work_spans(question) == ()


async def test_role_counts_only_remote_work_evidence_and_keeps_area_rules(db_session):
    company = await _company(db_session)
    declared = await _posting(db_session, company, "Remote, US", "Data Engineer", "Build.")
    prose = await _posting(
        db_session, company, "Austin, TX", "Data Engineer", "This position is remote."
    )
    await _posting(db_session, company, "San Jose, CA", "Data Engineer", "Build pipelines.")
    await _posting(db_session, company, None, "Data Engineer", "Build pipelines.")
    await _posting(
        db_session, company, "San Jose, CA", "Data Engineer (Hybrid)", "This role is remote."
    )
    await _posting(db_session, company, "Remote, Canada", "Data Engineer", "This role is remote.")
    found = await module.retrieve(db_session, "remote data engineer jobs")
    assert found.work_mode == "remote"
    assert found.role_total == 2
    assert found.beyond_role == ()
    assert {p.posting_id for p in (*found.passages, *found.more)} == {declared.id, prose.id}
    assert found.outside_area == 1


async def test_mode_only_reads_past_pool_and_counts_more_than_120_matches(db_session):
    company = await _company(db_session)
    for index in range(130):
        await _posting(db_session, company, "Remote, US", f"Specialist {index}", "Build.")
    for index in range(130):
        await _posting(db_session, company, "San Jose, CA", f"Specialist {index}", "Build.")
    found = await module.retrieve(db_session, "remote jobs")
    assert found.work_mode == "remote"
    assert found.match_total == 130
    assert found.company_total == 1
    assert len(found.passages) == 5
    assert len(found.more) == 25
    assert {p.location for p in (*found.passages, *found.more)} == {"Remote, US"}


async def test_company_mode_only_keeps_all_remote_matches(db_session):
    company = await _company(db_session, "Acme")
    here = await _posting(db_session, company, "San Jose, CA", "Specialist", "Work remotely.")
    await _posting(db_session, company, "San Jose, CA", "Specialist", "Build.")
    found = await module.retrieve(db_session, "remote jobs at Acme")
    assert found.companies == ("Acme",)
    assert found.match_total == 1
    assert [p.posting_id for p in found.passages] == [here.id]


async def test_keyword_totals_recheck_remote_prose_per_row(db_session):
    company = await _company(db_session)
    here = await _posting(
        db_session, company, "San Jose, CA", "Specialist", "Kafka. This is a remote role."
    )
    await _posting(db_session, company, "San Jose, CA", "Specialist", "Kafka. Build services.")
    closed = await _posting(db_session, company, "Remote, US", "Specialist", "Kafka.")
    closed.closed_at = datetime.now(UTC)
    await db_session.flush()
    found = await module.retrieve(db_session, "remote jobs using Kafka")
    assert found.match_total == found.company_total == 1
    assert found.matched_term == "Kafka"
    assert [p.posting_id for p in found.passages] == [here.id]


async def test_bay_area_and_remote_are_both_constraints(db_session):
    company = await _company(db_session)
    here = await _posting(
        db_session, company, "San Francisco, CA", "Data Engineer", "This role is remote."
    )
    await _posting(db_session, company, "Remote, US", "Data Engineer", "Build.")
    await _posting(db_session, company, "San Jose, CA", "Data Engineer", "Build.")
    found = await module.retrieve(db_session, "remote data engineer jobs in the Bay Area")
    assert found.role_total == found.place_total == 1
    assert found.remote_or_unplaced_not_listed == 1
    assert [p.posting_id for p in found.passages] == [here.id]


async def test_no_mode_preserves_nonremote_roles(db_session):
    company = await _company(db_session)
    remote = await _posting(db_session, company, "Remote, US", "Data Engineer", "Build.")
    onsite = await _posting(db_session, company, "San Jose, CA", "Data Engineer", "Build.")
    found = await module.retrieve(db_session, "data engineer jobs")
    assert found.work_mode is None
    assert found.role_total == 2
    assert {p.posting_id for p in found.passages} == {remote.id, onsite.id}


async def test_percentage_denominator_is_not_filtered_by_remote_mode(db_session):
    company = await _company(db_session)
    await _posting(db_session, company, "Remote, US", "Data Engineer", "Build.")
    await _posting(db_session, company, "San Jose, CA", "Data Engineer", "Build.")
    found = await module.retrieve(db_session, "remote data engineer jobs what's the percentage")
    assert found.share.matching == 1
    assert found.share.open_total == 2
    assert found.share.percentage == 50


async def test_remote_required_skill_path_keeps_both_constraints(db_session):
    company = await _company(db_session)
    remote = await _posting(db_session, company, "Remote, US", "Specialist", "Requirements\nKafka")
    await _posting(db_session, company, "San Jose, CA", "Specialist", "Requirements\nKafka")
    found = await module.retrieve(db_session, "remote jobs that require Kafka")
    assert found.work_mode == "remote"
    assert found.required_skill.required == 1
    assert [p.posting_id for p in found.passages] == [remote.id]


@pytest.mark.parametrize("route", ["/chat", "/chat/stream"])
async def test_remote_count_answers_keep_mode_scope_on_both_routes(
    client, worker_session, monkeypatch, route
):
    import json

    from apps.api.routers import chat

    company = await _company(worker_session)
    await _posting(worker_session, company, "Remote, US", "Specialist", "Build.")
    await _posting(worker_session, company, "San Jose, CA", "Specialist", "Build.")
    await worker_session.commit()

    def forbidden(*args, **kwargs):
        pytest.fail("Remote counts must not build a provider")

    monkeypatch.setattr(chat.llm_router, "build_provider", forbidden)
    response = await client.post(
        route, json={"message": "how many remote jobs", "provider": "kaggle"}
    )
    assert response.status_code == 200
    reply = (
        json.loads(response.text.splitlines()[-1])["reply"]
        if route.endswith("stream")
        else response.json()
    )
    assert reply["postings_work_mode"] == "remote"
    assert "1 open postings in your search area with remote-work evidence" in reply["reply"]
    assert "postings at  " not in reply["reply"]
    count = next(f for f in reply["count_facts"] if f["key"] == "matched")
    assert count["predicate"] == "postings with remote-work evidence"
    assert count["scope"].endswith("with remote-work evidence")
