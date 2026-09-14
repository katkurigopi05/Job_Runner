"""The setup and recovery page: every problem with the step that fixes it.

Doctor output is replaced by a fixed report in most tests, so these assert
what the page makes of a verdict rather than whether this machine has Pango.
The secrets test uses the real vault and inbox checks, because "never display
a secret" is only worth testing against the code that would leak one.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select

from packages.core import doctor
from packages.core.config import get_settings
from packages.core.doctor import Check, Health, Report
from packages.core.enums import SourceStatus
from packages.core.heartbeat import record
from packages.core.models import Company, CrawlRun
from packages.core.queue import enqueue


def _report(*extra: Check) -> Report:
    return Report(
        checks=[
            Check("postgres", Health.OK, "reachable"),
            Check("migrations", Health.OK, "at head"),
            Check("vault", Health.OK, "key present and parseable"),
            Check(
                "inbox",
                Health.FAIL,
                "no mailbox configured",
                fix="set IMAP_HOST, IMAP_USERNAME and IMAP_PASSWORD in .env",
                required=False,
            ),
            Check("storage", Health.OK, "writable"),
            *extra,
        ]
    )


@pytest.fixture(autouse=True)
def fixed_doctor(monkeypatch):
    async def fake(*, include_optional: bool = True) -> Report:
        return _report()

    monkeypatch.setattr(doctor, "run", fake)


@pytest.fixture(autouse=True)
def registry_file(monkeypatch, tmp_path):
    """An isolated registry, so the real 181-board file does not decide a test."""
    from packages.crawler import extract

    path = tmp_path / "companies.yaml"
    path.write_text("companies: []\n")
    monkeypatch.setattr(extract, "default_seed_path", lambda: path)
    return path


async def _status(client: AsyncClient) -> dict:
    response = await client.get("/setup/status")
    assert response.status_code == 200, response.text
    return response.json()


def _item(status: dict, key: str) -> dict:
    return next(item for item in status["items"] if item["key"] == key)


async def test_every_area_is_reported(client: AsyncClient) -> None:
    status = await _status(client)

    keys = {item["key"] for item in status["items"]}
    assert {
        "api",
        "postgres",
        "migrations",
        "worker",
        "vault",
        "inbox",
        "registry",
        "crawler",
    } <= keys
    assert _item(status, "api")["state"] == "ok"


async def test_an_optional_failure_is_attention_with_its_fix_as_a_step(client: AsyncClient) -> None:
    inbox = _item(await _status(client), "inbox")

    assert inbox["state"] == "attention"
    assert inbox["steps"] == ["set IMAP_HOST, IMAP_USERNAME and IMAP_PASSWORD in .env"]
    assert inbox["facts"]["messages_ingested"] == 0


async def test_no_heartbeat_ever_names_the_command(client: AsyncClient) -> None:
    worker = _item(await _status(client), "worker")

    assert worker["state"] == "attention"
    assert any("make worker" in step for step in worker["steps"])


async def test_a_fresh_heartbeat_is_ok(client: AsyncClient, worker_session) -> None:
    now = datetime.now(UTC)
    await record(worker_session, worker_id="w-fresh", started_at=now, task_kind="crawl", now=now)
    await worker_session.commit()

    worker = _item(await _status(client), "worker")

    assert worker["state"] == "ok"
    assert "w-fresh alive, running crawl" in worker["detail"]


async def test_a_silent_worker_with_queued_work_is_blocked(
    client: AsyncClient, worker_session
) -> None:
    then = datetime.now(UTC) - timedelta(minutes=10)
    await record(worker_session, worker_id="w-dead", started_at=then, now=then)
    await enqueue(worker_session, "apply", {"application_id": "x"})
    await worker_session.commit()

    worker = _item(await _status(client), "worker")

    assert worker["state"] == "blocked"
    assert "did not stop cleanly" in worker["detail"]
    assert worker["steps"][0].startswith("Look at the worker's terminal")


async def test_registry_drift_offers_a_preview_first(client: AsyncClient, registry_file) -> None:
    registry_file.write_text("companies:\n- name: Acme\n  slug: acmeco\n  checked: '2026-09-07'\n")

    registry = _item(await _status(client), "registry")

    assert registry["state"] == "blocked", "nothing is fetchable"
    assert registry["steps"][0].startswith("make registry-sync dry=1")
    assert registry["actions"] == ["registry_sync"]
    assert "Acme" in registry["detail"]


async def test_no_fetchable_board_blocks_the_crawler_and_points_at_the_registry(
    client: AsyncClient,
) -> None:
    crawler = _item(await _status(client), "crawler")

    assert crawler["state"] == "blocked"
    assert "registry" in crawler["steps"][0].lower()


async def test_a_recent_crawl_over_fetchable_boards_is_fresh(
    client: AsyncClient, worker_session
) -> None:
    worker_session.add(
        Company(
            name="Acme",
            slug="acmeco",
            ats_type="greenhouse",
            source_status=SourceStatus.VERIFIED.value,
        )
    )
    worker_session.add(
        CrawlRun(
            trigger="manual", status="completed", finished_at=datetime.now(UTC) - timedelta(hours=1)
        )
    )
    await worker_session.commit()

    crawler = _item(await _status(client), "crawler")

    assert crawler["state"] == "ok", crawler["detail"]
    assert crawler["facts"]["fetchable_boards"] == 1


async def test_an_old_crawl_with_nothing_scheduled_asks_for_one(
    client: AsyncClient, worker_session
) -> None:
    worker_session.add(
        Company(name="Acme", slug="acmeco", ats_type="greenhouse", source_status="verified")
    )
    worker_session.add(
        CrawlRun(
            trigger="manual", status="completed", finished_at=datetime.now(UTC) - timedelta(days=3)
        )
    )
    await worker_session.commit()

    crawler = _item(await _status(client), "crawler")

    assert crawler["state"] == "attention"
    assert any(step.startswith("make crawl") for step in crawler["steps"])


async def test_the_overall_state_is_the_worst_item(client: AsyncClient) -> None:
    status = await _status(client)

    assert status["overall"] == "blocked", "the empty registry leaves nothing fetchable"


async def test_no_secret_value_reaches_the_page(client: AsyncClient, monkeypatch, tmp_path) -> None:
    """Real vault and inbox checks, with sentinel secrets that must not appear."""
    key = "SENTINELvaultKEY" * 5
    password = "SENTINELimapPASSWORD"
    monkeypatch.setenv("VAULT_KEY", key)
    monkeypatch.setenv("VAULT_ROOT", str(tmp_path))
    monkeypatch.setenv("IMAP_HOST", "imap.gmail.com")
    monkeypatch.setenv("IMAP_USERNAME", "owner@gmail.com")
    monkeypatch.setenv("IMAP_PASSWORD", password)
    get_settings.cache_clear()

    async def real_secret_checks(*, include_optional: bool = True) -> Report:
        return Report(checks=[doctor.check_vault_key(), doctor.check_inbox()])

    monkeypatch.setattr(doctor, "run", real_secret_checks)
    try:
        body = (await client.get("/setup/status")).text
    finally:
        get_settings.cache_clear()

    assert "SENTINELvaultKEY" not in body
    assert password not in body
    assert "vault" in body and "inbox" in body


async def test_the_registry_preview_writes_nothing_and_apply_does(
    client: AsyncClient, registry_file, committing_sessionmaker
) -> None:
    registry_file.write_text("companies:\n- name: Acme\n  slug: acmeco\n  checked: '2026-09-07'\n")

    async def rows() -> int:
        async with committing_sessionmaker() as session:
            return int(await session.scalar(select(func.count()).select_from(Company)) or 0)

    preview = (await client.post("/setup/registry-sync", json={"dry_run": True})).json()
    assert (preview["dry_run"], preview["created"]) == (True, 1)
    assert await rows() == 0

    applied = (await client.post("/setup/registry-sync", json={"dry_run": False})).json()
    assert (applied["dry_run"], applied["created"]) == (False, 1)
    assert await rows() == 1

    registry = _item(await _status(client), "registry")
    assert registry["state"] == "ok"
