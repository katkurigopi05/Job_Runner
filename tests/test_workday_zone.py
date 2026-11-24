"""Every Workday company is paced and backed off as one site.

Workday serves each company at its own host, so the limiter gave each one its
own counter. On 2026-11-23 a pool reached about 210 Workday boards within a
second, and Cloudflare, in front of all of them, answered 429 to every board,
`robots.txt` included, while the floor was kept per company. These tests hold
the pacing and the shared backoff; they do not claim Workday reads again.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select

from packages.core.enums import QueueTaskStatus, SourceStatus
from packages.core.models import Company, QueueTask
from packages.core.queue import TaskDeferred, claim_task, enqueue
from packages.crawler import defer
from packages.crawler.dispatch import CRAWL_COMPANY_TASK_KIND
from packages.crawler.fetch import PoliteFetcher
from packages.crawler.ratelimit import MIN_DELAY_SECONDS, HostRateLimiter, budget_key

ZONE = "myworkdayjobs.com"
ACME = "https://acme.wd5.myworkdayjobs.com/wday/cxs/acme/External/jobs"
GLOBEX = "https://globex.wd1.myworkdayjobs.com/wday/cxs/globex/Careers/jobs"
GREENHOUSE = "https://boards-api.greenhouse.io/v1/boards/acme/jobs"


def _fetcher(*, board_status: int = 200) -> tuple[PoliteFetcher, HostRateLimiter]:
    from tests.test_crawler import FakeClock

    clock = FakeClock()
    limiter = HostRateLimiter(clock=clock, sleeper=clock.sleep)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow:")
        return httpx.Response(board_status, text='{"total": 0, "jobPostings": []}')

    fetcher = PoliteFetcher(
        transport=httpx.MockTransport(handler), rate_limiter=limiter, record_metrics=False
    )
    return fetcher, limiter


@pytest.mark.parametrize(
    ("host", "key"),
    [
        ("acme.wd5.myworkdayjobs.com", ZONE),
        ("globex.wd1.myworkdayjobs.com", ZONE),
        ("myworkdayjobs.com", ZONE),
        ("boards-api.greenhouse.io", "boards-api.greenhouse.io"),
        # Resembling the zone is not being in it.
        ("myworkdayjobs.com.example.net", "myworkdayjobs.com.example.net"),
        ("notmyworkdayjobs.com", "notmyworkdayjobs.com"),
    ],
)
def test_only_hosts_under_the_zone_share_its_counter(host: str, key: str) -> None:
    assert budget_key(host) == key


@pytest.mark.asyncio
async def test_a_second_workday_company_waits_for_the_first() -> None:
    # Arrange
    fetcher, _ = _fetcher()

    # Act
    await fetcher.fetch(ACME, method="POST", json={})
    second = await fetcher.fetch(GLOBEX, method="POST", json={})

    # Assert
    assert second.waited >= MIN_DELAY_SECONDS
    await fetcher.aclose()


@pytest.mark.asyncio
async def test_other_hosts_keep_their_own_counters() -> None:
    fetcher, _ = _fetcher()

    await fetcher.fetch(ACME, method="POST", json={})
    other = await fetcher.fetch(GREENHOUSE)

    assert other.waited == 0
    await fetcher.aclose()


@pytest.mark.asyncio
async def test_a_429_from_one_company_backs_off_every_workday_company() -> None:
    # Arrange
    fetcher, limiter = _fetcher(board_status=429)

    # Act
    await fetcher.fetch(ACME, method="POST", json={})

    # Assert: the penalty is the zone's, so the next company waits it out.
    assert await limiter.wait_for(budget_key("globex.wd1.myworkdayjobs.com")) >= 59
    await fetcher.aclose()


async def _workday_tasks(sessions, count: int) -> list[str]:
    async with sessions() as session:
        ids = []
        for n in range(count):
            company = Company(
                name=f"Tenant {n}",
                slug=f"t{n}.wd5.myworkdayjobs.com|t{n}|External",
                ats_type="workday",
                poll_interval_s=0,
                source_status=SourceStatus.VERIFIED.value,
            )
            session.add(company)
            await session.flush()
            await enqueue(
                session,
                CRAWL_COMPANY_TASK_KIND,
                {"company_id": str(company.id)},
                run_after=datetime.now(UTC) - timedelta(seconds=60 - n),
            )
            ids.append(str(company.id))
        await session.commit()
        return ids


async def _claim(sessions, worker_id: str):
    async with sessions() as session:
        claimed = await claim_task(session, worker_id=worker_id, kinds=[CRAWL_COMPANY_TASK_KIND])
        await session.commit()
        return claimed


@pytest.mark.asyncio
async def test_a_third_workday_board_waits_while_two_are_read(committing_sessionmaker) -> None:
    # Arrange: two Workday boards claimed by other workers.
    await _workday_tasks(committing_sessionmaker, 3)
    for worker_id in ("one", "two"):
        assert await _claim(committing_sessionmaker, worker_id) is not None

    # Act: the third is claimed and checked in one session, as a worker does.
    async with committing_sessionmaker() as session:
        third = await claim_task(session, worker_id="three", kinds=[CRAWL_COMPANY_TASK_KIND])
        assert third is not None
        with pytest.raises(TaskDeferred):
            await defer.defer_if_zone_full(session, third, ACME, ats_type="workday")
        await session.commit()

    # Assert: handed back for the recheck, with its attempt given back.
    async with committing_sessionmaker() as session:
        task = await session.scalar(select(QueueTask).where(QueueTask.id == third.task_id))
        assert task.status == QueueTaskStatus.PENDING.value
        assert task.attempts == 0
        assert task.run_after > datetime.now(UTC) + timedelta(seconds=defer.ZONE_RECHECK_S - 10)


@pytest.mark.asyncio
async def test_a_board_outside_the_zone_is_never_held_by_it(committing_sessionmaker) -> None:
    await _workday_tasks(committing_sessionmaker, 3)
    for worker_id in ("one", "two"):
        await _claim(committing_sessionmaker, worker_id)
    third = await _claim(committing_sessionmaker, "three")

    async with committing_sessionmaker() as session:
        await defer.defer_if_zone_full(session, third, GREENHOUSE, ats_type="greenhouse")


@pytest.mark.asyncio
async def test_a_lost_worker_does_not_hold_a_place(committing_sessionmaker) -> None:
    # Arrange: two claims whose leases have run out, as a crashed worker leaves them.
    await _workday_tasks(committing_sessionmaker, 3)
    for worker_id in ("one", "two"):
        await _claim(committing_sessionmaker, worker_id)
    async with committing_sessionmaker() as session:
        for task in await session.scalars(
            select(QueueTask).where(QueueTask.status == QueueTaskStatus.RUNNING.value)
        ):
            task.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()
    third = await _claim(committing_sessionmaker, "three")

    # Act / Assert: nothing live is reading, so the third goes ahead.
    async with committing_sessionmaker() as session:
        await defer.defer_if_zone_full(session, third, ACME, ats_type="workday")
