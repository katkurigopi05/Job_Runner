"""Full-match demand evidence, beyond the five nearest keyword mentions."""

import pytest

from packages.matching.retrieve import retrieve
from tests.test_chat_title_words import _company, _posting


async def _post(session, company, text, *, location="San Francisco, CA", days=0):
    post = await _posting(session, company, "Platform Specialist", text, days_ago=days)
    post.location = location
    await session.flush()
    return post


async def test_required_list_reaches_past_the_keyword_pool(db_session):
    company = await _company(db_session, "Example Labs")
    for i in range(125):
        await _post(db_session, company, "Preferred\nKafka", days=i)
    direct = await _post(db_session, company, "Essential\nKafka experience", days=200)
    found = await retrieve(db_session, "any companies required skill of kafka list them")
    assert found.match_total == 126  # mentions remain a separate count
    assert [p.posting_id for p in found.passages] == [direct.id]
    assert found.required_skill.required == 1
    assert found.required_skill.companies == ("Example Labs",)
    assert found.required_skill.preferred == 125
    assert found.required_skill.unread == 0


async def test_required_list_keeps_strength_options_area_and_unknown_separate(db_session):
    company = await _company(db_session, "Example Labs")
    direct = await _post(db_session, company, "Requirements\nKafka experience")
    await _post(db_session, company, "Requirements\nKafka or Kinesis")
    await _post(db_session, company, "Preferred\nKafka")
    await _post(db_session, company, "We use Kafka.")
    unread = await _post(db_session, company, "Kafka role")
    unread.description_raw = None
    unread.title = "Kafka Specialist"
    await _post(db_session, company, "Requirements\nKafka", location="London, UK")
    await db_session.flush()
    found = await retrieve(db_session, "jobs that require Kafka")
    assert [p.posting_id for p in found.passages] == [direct.id]
    assert found.match_total == 5
    assert found.outside_area == 1
    count = found.required_skill
    assert (count.required, count.options, count.preferred, count.named, count.unread) == (
        1,
        1,
        1,
        1,
        1,
    )
    assert count.complete is False


@pytest.mark.parametrize("question", ["jobs using Kafka", "jobs that require Kafka and Python"])
async def test_ordinary_mentions_and_multiple_skills_keep_existing_search(db_session, question):
    company = await _company(db_session)
    post = await _post(db_session, company, "We use Kafka and Python.")
    found = await retrieve(db_session, question)
    assert found.required_skill is None
    assert post.id in {p.posting_id for p in found.passages}


async def test_negated_requirement_is_not_a_direct_demand(db_session):
    company = await _company(db_session)
    await _post(db_session, company, "Requirements\nKafka is not required")
    found = await retrieve(db_session, "jobs that require Kafka")
    assert not found.passages
    assert found.required_skill.required == 0


async def test_required_skill_counts_all_vocabulary_aliases(db_session):
    company = await _company(db_session)
    canonical = await _post(db_session, company, "Requirements\nKubernetes experience")
    alias = await _post(db_session, company, "Requirements\nK8s experience")
    found = await retrieve(db_session, "jobs that require Kubernetes")
    assert found.required_skill.required == 2
    assert {p.posting_id for p in found.passages} == {canonical.id, alias.id}
    assert found.required_skill.complete


@pytest.mark.parametrize(
    "text",
    [
        "No Kafka experience is required",
        "Kafka experience is not mandatory",
        "Kafka is never required",
    ],
)
async def test_denied_skill_requirement_is_not_counted(db_session, text):
    company = await _company(db_session)
    await _post(db_session, company, "Requirements\n" + text)
    found = await retrieve(db_session, "jobs that require Kafka")
    assert found.required_skill.required == 0
    assert not found.passages


@pytest.mark.parametrize(
    "question,skill,text",
    [
        ("jobs that require React", "React", "React experience"),
        ("jobs requiring Kafka", "Kafka", "Kafka experience"),
        ("jobs that require machine learning", "Machine learning", "machine learning experience"),
        ("jobs that require Python", "Python", "Python experience"),
    ],
)
async def test_explicit_demand_does_not_depend_on_ranking_terms(db_session, question, skill, text):
    from tests.test_chat_title_words import _common

    await _common(db_session, "python")
    company = await _company(db_session)
    await _post(db_session, company, "Requirements\n" + text)
    found = await retrieve(db_session, question)
    assert found.required_skill is not None
    assert found.required_skill.skill == skill
    assert found.required_skill.required == 1


async def test_mixed_negation_does_not_erase_direct_required_evidence(db_session):
    company = await _company(db_session)
    await _post(
        db_session,
        company,
        "Requirements\nKafka or Kinesis\nKafka is required but Python is not required",
    )
    found = await retrieve(db_session, "jobs that require Kafka")
    assert found.required_skill.required == 1
    assert found.required_skill.options == 0


@pytest.mark.parametrize("route", ["/chat", "/chat/stream"])
async def test_required_answers_share_scope_notices_without_generation(
    client, worker_session, monkeypatch, route
):
    import json

    from apps.api.routers import chat

    company = await _company(worker_session)
    post = await _post(worker_session, company, "Essential\nKafka experience")
    post.title = "Senior Platform Specialist"
    await worker_session.commit()

    def forbidden(*args, **kwargs):
        pytest.fail("Direct requirement facts must not build a provider")

    monkeypatch.setattr(chat.llm_router, "build_provider", forbidden)
    response = await client.post(
        route, json={"message": "senior jobs that require Kafka", "provider": "kaggle"}
    )
    assert response.status_code == 200
    reply = (
        json.loads(response.text.splitlines()[-1])["reply"]
        if route.endswith("stream")
        else response.json()
    )
    assert reply["provider"] == "facts"
    assert "1 postings directly require Kafka" in reply["reply"]
    assert "For level senior" in reply["reply"]
    assert "Search area:" in reply["reply"]
    assert "Kafka experience" in reply["reply"]
    required = next(f for f in reply["count_facts"] if f["key"] == "required_skill")
    assert required["value"] == 1
    assert required["coverage"] == "exact"
