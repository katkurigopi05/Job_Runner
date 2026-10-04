"""Saying "run crawler" or "find new jobs" to the assistant starts a crawl.

Recognised in code, like the §2.2 refusal: a command is not a question, so no
model is asked and nothing is sent anywhere, whichever provider is selected.
The crawl goes through `request_crawl`, the same door `make crawl` uses, so a
crawl already waiting is not doubled. The reply says whether a worker is alive
to run it, because a queued crawl nobody drains looks exactly like a quiet
registry.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select

from apps.api.routers.chat import asks_to_crawl
from apps.worker.crawl_job import CRAWL_TASK_KIND
from packages.core.models import QueueTask
from packages.core.models_ops import WorkerHeartbeat


@pytest.mark.parametrize(
    "message",
    [
        "run crawler",
        "Run the crawler",
        "please run the crawler now",
        "can you run the crawler?",
        "start a crawl",
        "kick off a crawl",
        "crawl",
        "crawl now!",
        "find new jobs",
        "Find new jobs for me",
        "look for new jobs",
        "check for new postings",
        "fetch fresh roles",
        "refresh the postings",
        "update the job board",
    ],
)
def test_commands_to_crawl_are_recognised(message: str) -> None:
    assert asks_to_crawl(message), message


@pytest.mark.parametrize(
    "message",
    [
        "Which new jobs mention Kafka?",
        "Any new jobs at Stripe?",
        "Did the crawler run?",
        "When did the crawl last finish?",
        "What's the crawler status?",
        "find Kafka jobs",
        "What needs me?",
        "Show me jobs about crawling robots",
    ],
)
def test_questions_are_not_commands(message: str) -> None:
    """A question about jobs, or about the crawler, is searched or answered."""
    assert not asks_to_crawl(message), message


def _never_ask_a_model(monkeypatch) -> None:
    import apps.api.routers.chat as chat_module

    def refuse(name=None):
        raise AssertionError("a crawl command must not reach a model")

    monkeypatch.setattr(chat_module.llm_router, "build_provider", refuse)


async def _crawls(session) -> int:
    return await session.scalar(
        select(func.count()).select_from(QueueTask).where(QueueTask.kind == CRAWL_TASK_KIND)
    )


async def test_the_command_queues_a_crawl_and_asks_no_model(
    client: AsyncClient, worker_session, monkeypatch
) -> None:
    _never_ask_a_model(monkeypatch)

    answered = await client.post("/chat", json={"message": "run the crawler"})

    assert answered.status_code == 200
    body = answered.json()
    assert body["provider"] == "crawler"
    assert body["local"] is True
    assert await _crawls(worker_session) == 1


async def test_without_a_live_worker_the_reply_says_nothing_will_happen_yet(
    client: AsyncClient, monkeypatch
) -> None:
    _never_ask_a_model(monkeypatch)

    body = (await client.post("/chat", json={"message": "find new jobs"})).json()

    assert "make worker" in body["reply"]


async def test_with_a_live_worker_the_reply_says_it_is_under_way(
    client: AsyncClient, worker_session, monkeypatch
) -> None:
    _never_ask_a_model(monkeypatch)
    now = datetime.now(UTC)
    worker_session.add(
        WorkerHeartbeat(worker_id="w1", hostname="h", pid=1, started_at=now, last_seen_at=now)
    )
    await worker_session.commit()

    body = (await client.post("/chat", json={"message": "find new jobs"})).json()

    assert "make worker" not in body["reply"]
    assert "running" in body["reply"]


async def test_a_second_command_while_one_waits_starts_nothing_new(
    client: AsyncClient, worker_session, monkeypatch
) -> None:
    _never_ask_a_model(monkeypatch)

    await client.post("/chat", json={"message": "run crawler"})
    body = (await client.post("/chat", json={"message": "find new jobs"})).json()

    assert "already" in body["reply"]
    assert await _crawls(worker_session) == 1


async def test_a_remote_provider_selected_changes_nothing(
    client: AsyncClient, worker_session, monkeypatch
) -> None:
    """The command never reaches the provider, so it sends nothing anywhere."""
    _never_ask_a_model(monkeypatch)

    body = (
        await client.post("/chat", json={"message": "run crawler", "provider": "openrouter"})
    ).json()

    assert body["provider"] == "crawler"
    assert body["local"] is True
    assert await _crawls(worker_session) == 1
