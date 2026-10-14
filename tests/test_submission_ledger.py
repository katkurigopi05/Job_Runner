"""A database rollback cannot undo the employer's Submit click."""

from __future__ import annotations

import asyncio
import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from apps.worker import apply_job
from packages.ats.base import Receipt
from packages.core.enums import ApplicationStatus, FailureReason
from packages.core.models import (
    Application,
    ApplicationEvent,
    AppliedPosting,
    Candidate,
    Profile,
    User,
)
from packages.core.state import transition


class WorkerDied(BaseException):
    """Like process loss: do not let an ordinary exception handler recover."""


class Employer:
    def __init__(self, *, die: bool = False, confirmed: bool = True):
        self.clicks = 0
        self.die = die
        self.confirmed = confirmed

    async def submit(self, page):
        self.clicks += 1
        if self.die:
            raise WorkerDied()
        return Receipt(
            submitted=self.confirmed,
            ats="greenhouse",
            url="https://example.test/job",
            confirmation_text="Application received",
        )


async def _application(maker, *, url="https://boards.greenhouse.io/acme/jobs/123"):
    async with maker() as session:
        uid = uuid.uuid4().hex
        user = User(email=f"{uid}@example.test")
        session.add(user)
        await session.flush()
        person = Candidate(user_id=user.id, name="Fixture", email=user.email)
        session.add(person)
        await session.flush()
        profile = Profile(candidate_id=person.id, label="fixture")
        session.add(profile)
        await session.flush()
        app = Application(candidate_id=person.id, profile_id=profile.id, url=url)
        session.add(app)
        await session.flush()
        await transition(session, app, ApplicationStatus.RUNNING)
        await session.commit()
        return app.id


async def test_crash_after_click_never_clicks_again(committing_sessionmaker):
    maker = committing_sessionmaker
    app_id = await _application(maker)
    employer = Employer(die=True)
    async with maker() as session:
        app = await session.get(Application, app_id)
        with pytest.raises(WorkerDied):
            await apply_job._submit(session, app, employer, None)
        await session.rollback()
    employer.die = False
    async with maker() as session:
        app = await session.get(Application, app_id)
        await apply_job._submit(session, app, employer, None)
        await session.commit()
        assert app.status == "needs_review"
        assert app.review_json["submission_uncertain"] is True
    assert employer.clicks == 1


async def test_confirmed_receipt_survives_screenshot_crash(committing_sessionmaker, monkeypatch):
    maker = committing_sessionmaker
    app_id = await _application(maker)
    employer = Employer()

    async def crash(*args):
        raise WorkerDied()

    monkeypatch.setattr(apply_job, "_capture", crash)
    async with maker() as session:
        app = await session.get(Application, app_id)
        with pytest.raises(WorkerDied):
            await apply_job._submit(session, app, employer, None)
        await session.rollback()
    async with maker() as session:
        app = await session.get(Application, app_id)
        assert app.status == "submitted"
        assert app.receipt_json["submitted"] is True
    assert employer.clicks == 1


async def test_two_workers_only_click_once(committing_sessionmaker):
    maker = committing_sessionmaker
    app_id = await _application(maker)
    employer = Employer()

    async def attempt():
        async with maker() as session:
            app = await session.get(Application, app_id)
            await apply_job._submit(session, app, employer, None)
            await session.commit()

    await asyncio.gather(attempt(), attempt())
    assert employer.clicks == 1
    async with maker() as session:
        app = await session.get(Application, app_id)
        assert app.status == "submitted"
        events = (
            await session.scalars(
                select(ApplicationEvent).where(ApplicationEvent.application_id == app_id)
            )
        ).all()
        submitted_events = [e for e in events if (e.payload_json or {}).get("to") == "submitted"]
        assert len(submitted_events) == 1


async def test_unconfirmed_receipt_is_not_success(committing_sessionmaker):
    maker = committing_sessionmaker
    app_id = await _application(maker)
    async with maker() as session:
        app = await session.get(Application, app_id)
        await apply_job._submit(session, app, Employer(confirmed=False), None)
        await session.commit()
        assert app.status == "needs_review"


async def test_ledger_is_durable_before_employer_is_called(committing_sessionmaker):
    maker = committing_sessionmaker
    app_id = await _application(maker)

    class ObservingEmployer(Employer):
        async def submit(self, page):
            async with maker() as independent:
                row = await independent.scalar(
                    select(AppliedPosting).where(AppliedPosting.application_id == app_id)
                )
                assert row is not None and row.state == "attempting"
            return await super().submit(page)

    async with maker() as session:
        app = await session.get(Application, app_id)
        await apply_job._submit(session, app, ObservingEmployer(), None)
        await session.commit()


@pytest.mark.parametrize(
    "other",
    [
        "https://job-boards.greenhouse.io/acme/jobs/123?utm_source=mail#application",
        "https://boards.greenhouse.io/acme/jobs/123?gh_src=tracker",
    ],
)
async def test_tracking_urls_cannot_submit_same_job_again(committing_sessionmaker, other):
    maker = committing_sessionmaker
    app_id = await _application(maker)
    employer = Employer()
    async with maker() as session:
        app = await session.get(Application, app_id)
        await apply_job._submit(session, app, employer, None)
        await session.commit()
        second = Application(candidate_id=app.candidate_id, profile_id=app.profile_id, url=other)
        session.add(second)
        await session.flush()
        await transition(session, second, ApplicationStatus.RUNNING)
        await session.commit()
        await apply_job._submit(session, second, employer, None)
        await session.commit()
        assert second.status == "needs_review"
    assert employer.clicks == 1


async def test_real_worker_path_base_exception_retry(committing_sessionmaker, monkeypatch):
    """Real worker path: stub adapter raises BaseException after counting click.
    Reclaim task like lease expiry, run handler again, assert 1 click and needs_review with message.
    """
    from apps.worker import run as worker_run
    from packages.ats.base import FillReport
    from packages.core.queue import enqueue, expire_leases

    maker = committing_sessionmaker
    app_id = await _application(maker)
    employer = Employer(die=True)

    async def _stub_pipeline(session, application, candidate, profile):
        await apply_job._decide(session, application, profile, FillReport(), employer, None)

    monkeypatch.setattr(apply_job, "_run_pipeline", _stub_pipeline)

    async with maker() as session:
        app = await session.get(Application, app_id)
        app.review_json = {"owner_approved": True}
        await enqueue(session, "apply", {"application_id": str(app_id)})
        await session.commit()

    with pytest.raises(WorkerDied):
        await worker_run.run_once(worker_id="worker-test")

    assert employer.clicks == 1
    employer.die = False

    async with maker() as session:
        await expire_leases(session)
        await session.commit()

    ran = await worker_run.run_once(worker_id="worker-test")
    assert ran is True
    assert employer.clicks == 1

    async with maker() as session:
        app = await session.get(Application, app_id)
        assert app.status == "needs_review"
        assert app.review_json["submission_uncertain"] is True
        reason = app.review_json.get("reason", "")
        assert "earlier attempt" in reason or "previous submission" in reason


async def test_approval_after_uncertain_park_releases_and_submits(
    client: AsyncClient, committing_sessionmaker
):
    """Approval after uncertain park: approve through API route releases fence."""
    maker = committing_sessionmaker
    app_id = await _application(maker)
    employer = Employer()

    async with maker() as session:
        app = await session.get(Application, app_id)
        await apply_job._submit(session, app, Employer(confirmed=False), None)
        await session.commit()
        assert app.status == "needs_review"
        fence = await session.get(AppliedPosting, app_id)
        assert fence is not None and fence.state == "uncertain"

    resp = await client.post(f"/applications/{app_id}/review", json={"approve": True})
    assert resp.status_code == 200

    async with maker() as session:
        fence = await session.get(AppliedPosting, app_id)
        assert fence is not None and fence.state == "released"
        app = await session.get(Application, app_id)
        assert app.status == "running"

        await apply_job._submit(session, app, employer, None)
        await session.commit()
        assert app.status == "submitted"
        assert fence.state == "submitted"
    assert employer.clicks == 1


async def test_second_application_same_job_parks_and_names_first(committing_sessionmaker):
    maker = committing_sessionmaker
    app_id = await _application(maker, url="https://boards.greenhouse.io/acme/jobs/456")
    employer = Employer()

    async with maker() as session:
        app = await session.get(Application, app_id)
        await apply_job._submit(session, app, employer, None)
        await session.commit()
        assert app.status == "submitted"

        second = Application(
            candidate_id=app.candidate_id,
            profile_id=app.profile_id,
            url="https://job-boards.greenhouse.io/acme/jobs/456?gh_src=other",
        )
        session.add(second)
        await session.flush()
        await transition(session, second, ApplicationStatus.RUNNING)
        await session.commit()
        await apply_job._submit(session, second, employer, None)
        await session.commit()

        assert second.status == "needs_review"
        assert str(app_id) in second.review_json["reason"]
        assert app.url in second.review_json["reason"]
        assert "submitted" in second.review_json["reason"]
    assert employer.clicks == 1


async def test_manual_submit_fences_the_job(client: AsyncClient, committing_sessionmaker):
    maker = committing_sessionmaker
    app_id = await _application(maker, url="https://boards.greenhouse.io/acme/jobs/789")

    resp = await client.post(
        f"/applications/{app_id}/submitted", json={"note": "submitted by hand"}
    )
    assert resp.status_code == 200

    async with maker() as session:
        fence = await session.get(AppliedPosting, app_id)
        assert fence is not None
        assert fence.state == "submitted"
        assert fence.source == "owner"
        app = await session.get(Application, app_id)

        second = Application(
            candidate_id=app.candidate_id,
            profile_id=app.profile_id,
            url="https://job-boards.greenhouse.io/acme/jobs/789?utm_campaign=xyz",
        )
        session.add(second)
        await session.flush()
        await transition(session, second, ApplicationStatus.RUNNING)
        await session.commit()
        employer = Employer()
        await apply_job._submit(session, second, employer, None)
        await session.commit()
        assert second.status == "needs_review"
        assert employer.clicks == 0
        assert str(app_id) in second.review_json["reason"]


async def test_auto_submit_path_at_most_one_click_and_uncertain(
    committing_sessionmaker, monkeypatch
):
    """Auto-submit path: at most one click and uncertain behavior on failure/crash."""
    from apps.worker import run as worker_run
    from packages.ats.base import FillReport
    from packages.core.config import get_settings
    from packages.core.queue import enqueue, expire_leases

    maker = committing_sessionmaker
    app_id = await _application(maker)
    employer = Employer(die=True)

    monkeypatch.setenv("AUTO_SUBMIT", "true")
    get_settings.cache_clear()

    async def _stub_pipeline(session, application, candidate, profile):
        await apply_job._decide(session, application, profile, FillReport(), employer, None)

    monkeypatch.setattr(apply_job, "_run_pipeline", _stub_pipeline)

    async with maker() as session:
        app = await session.get(Application, app_id)
        profile = await session.get(Profile, app.profile_id)
        profile.auto_submit = True
        profile.min_match_score = 0.5

        async def _mock_score(*args):
            return 0.95

        monkeypatch.setattr(apply_job, "_match_score", _mock_score)

        await enqueue(session, "apply", {"application_id": str(app_id)})
        await session.commit()

    with pytest.raises(WorkerDied):
        await worker_run.run_once(worker_id="worker-test")

    assert employer.clicks == 1
    employer.die = False

    async with maker() as session:
        await expire_leases(session)
        await session.commit()

    ran = await worker_run.run_once(worker_id="worker-test")
    assert ran is True
    assert employer.clicks == 1

    async with maker() as session:
        app = await session.get(Application, app_id)
        assert app.status == "needs_review"
        assert app.review_json["submission_uncertain"] is True


async def test_happy_path_submit_stores_receipt_and_screenshot(
    committing_sessionmaker, monkeypatch
):
    maker = committing_sessionmaker
    app_id = await _application(maker)
    employer = Employer()
    screenshot_taken = False

    async def _mock_capture(page, app, filename):
        nonlocal screenshot_taken
        async with maker() as check_session:
            row = await check_session.get(Application, app_id)
            assert row.status == "submitted"
            fence = await check_session.get(AppliedPosting, app_id)
            assert fence.state == "submitted"
        screenshot_taken = True
        return "screenshots/submitted.png"

    monkeypatch.setattr(apply_job, "_capture", _mock_capture)

    async with maker() as session:
        app = await session.get(Application, app_id)
        await apply_job._submit(session, app, employer, None)
        await session.commit()

    assert employer.clicks == 1
    assert screenshot_taken is True
    async with maker() as session:
        app = await session.get(Application, app_id)
        assert app.status == "submitted"
        assert app.receipt_json["submitted"] is True
        assert app.receipt_json["screenshot_ref"] == "screenshots/submitted.png"
        fence = await session.get(AppliedPosting, app_id)
        assert fence.state == "submitted"
        assert fence.confirmed_at is not None


async def test_api_payload_surfaces_park_message_and_uncertain_flag(
    client: AsyncClient, committing_sessionmaker
):
    maker = committing_sessionmaker
    app_id = await _application(maker)
    async with maker() as session:
        app = await session.get(Application, app_id)
        await apply_job._submit(session, app, Employer(confirmed=False), None)
        await session.commit()

    resp = await client.get(f"/applications/{app_id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "needs_review"
    assert data["review"] is not None
    assert data["review"]["submission_uncertain"] is True
    assert "attempted_at" in data["review"]
    reason = data["review"]["reason"]
    assert "earlier attempt" in reason or "previous submission" in reason


async def test_stale_worker_cannot_submit_a_rejected_application(committing_sessionmaker):
    maker = committing_sessionmaker
    app_id = await _application(maker)
    employer = Employer()
    async with maker() as stale:
        app = await stale.get(Application, app_id)
        async with maker() as owner:
            current = await owner.get(Application, app_id)
            await transition(owner, current, ApplicationStatus.NEEDS_REVIEW)
            await transition(
                owner,
                current,
                ApplicationStatus.FAILED,
                failure_reason=FailureReason.REJECTED_AT_REVIEW,
            )
            await owner.commit()
        await apply_job._submit(stale, app, employer, None)
    assert employer.clicks == 0
    async with maker() as check:
        assert (await check.get(Application, app_id)).status == "failed"


async def test_failed_fence_commit_never_contacts_employer(committing_sessionmaker, monkeypatch):
    maker = committing_sessionmaker
    app_id = await _application(maker)
    employer = Employer()
    async with maker() as session:
        app = await session.get(Application, app_id)

        async def fail_commit():
            raise RuntimeError("database unavailable")

        monkeypatch.setattr(session, "commit", fail_commit)
        with pytest.raises(RuntimeError, match="database unavailable"):
            await apply_job._submit(session, app, employer, None)
    assert employer.clicks == 0


async def test_api_rejects_alias_of_submitted_job(client, complete_candidate, worker_session):
    body = {
        **complete_candidate,
        "url": "https://boards.greenhouse.io/acme/jobs/987",
    }
    created = await client.post("/applications", json=body)
    assert created.status_code == 201, created.text
    app_id = uuid.UUID(created.json()["id"])
    app = await worker_session.get(Application, app_id)
    await transition(worker_session, app, ApplicationStatus.RUNNING)
    await worker_session.commit()
    marked = await client.post(f"/applications/{app_id}/submitted", json={})
    assert marked.status_code == 200, marked.text
    body["url"] = "https://job-boards.greenhouse.io/acme/jobs/987?utm_source=mail"
    duplicate = await client.post("/applications", json=body)
    assert duplicate.status_code == 409, duplicate.text


async def test_retry_while_first_click_is_in_flight(committing_sessionmaker):
    maker = committing_sessionmaker
    app_id = await _application(maker)
    clicked = asyncio.Event()
    finish = asyncio.Event()

    class SlowEmployer(Employer):
        async def submit(self, page):
            receipt = await super().submit(page)
            clicked.set()
            await asyncio.wait_for(finish.wait(), timeout=5)
            return receipt

    employer = SlowEmployer()

    async def first_worker():
        async with maker() as session:
            app = await session.get(Application, app_id)
            await apply_job._submit(session, app, employer, None)

    first = asyncio.create_task(first_worker())
    try:
        await asyncio.wait_for(clicked.wait(), timeout=5)
        async with maker() as retry:
            app = await retry.get(Application, app_id)
            await apply_job._submit(retry, app, employer, None)
            assert app.status == "needs_review"
            assert employer.clicks == 1
    finally:
        finish.set()
        await first
    async with maker() as session:
        assert (await session.get(Application, app_id)).status == "submitted"
        assert (await session.get(AppliedPosting, app_id)).state == "submitted"


@pytest.mark.parametrize(
    ("left", "right", "same"),
    [
        ("https://jobs.lever.co/acme/123", "https://jobs.lever.co/acme/123/apply", True),
        (
            "https://jobs.ashbyhq.com/acme/123",
            "https://jobs.ashbyhq.com/acme/123/application",
            True,
        ),
        ("https://example.test/job?id=1", "https://example.test/job?id=2", False),
        ("https://jobs.lever.co/acme/123", "https://jobs.lever.co/acme/124", False),
        ("https://jobs.lever.co/acme/123", "https://jobs.lever.co/other/123", False),
    ],
)
def test_submission_identity_does_not_merge_different_jobs(left, right, same):
    from packages.core.submission_identity import submission_key_v1

    assert (submission_key_v1(left) == submission_key_v1(right)) is same
