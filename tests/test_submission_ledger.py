"""A database rollback cannot undo the employer's Submit click."""

from __future__ import annotations

import asyncio
import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from apps.worker import apply_job
from packages.ats.base import ManualCompletionRequired, Receipt, SiteError
from packages.core.enums import ApplicationStatus, EventType, FailureReason
from packages.core.models import (
    Application,
    ApplicationEvent,
    AppliedPosting,
    Candidate,
    Profile,
    QueueTask,
    User,
)
from packages.core.queue import ClaimedTask
from packages.core.state import transition


class WorkerDied(BaseException):
    """Like process loss: do not let an ordinary exception handler recover."""


class Employer:
    def __init__(
        self,
        *,
        die: bool = False,
        confirmed: bool = True,
        raise_before_click: Exception | None = None,
    ):
        self.clicks = 0
        self.die = die
        self.confirmed = confirmed
        self.raise_before_click = raise_before_click

    async def submit(self, page):
        if self.raise_before_click is not None:
            raise self.raise_before_click
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


async def test_a_click_the_site_did_not_confirm_is_fenced_uncertain(committing_sessionmaker):
    """The adapter's half and the ledger's half, joined. Each was tested
    alone, and a real adapter reported every click as sent, so a rejected
    click fenced the job `submitted` and no test spanned the two."""
    from apps.worker.browser import ephemeral_page
    from packages.ats.greenhouse import GreenhouseAdapter
    from tests.test_adapter_fill import _GREENHOUSE, _REJECTED

    maker = committing_sessionmaker
    app_id = await _application(maker)
    async with ephemeral_page() as page:
        await page.set_content(_GREENHOUSE + _REJECTED)
        async with maker() as session:
            app = await session.get(Application, app_id)
            await apply_job._submit(session, app, GreenhouseAdapter(), page)

    async with maker() as session:
        app = await session.get(Application, app_id)
        fence = (
            await session.scalars(
                select(AppliedPosting).where(AppliedPosting.application_id == app_id)
            )
        ).one()
        assert app.status == ApplicationStatus.NEEDS_REVIEW.value
        assert app.review_json["submission_uncertain"] is True
        assert fence.state == "uncertain"


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


async def test_submit_form_raises_before_click_on_captcha_and_missing_button():
    from packages.ats.form import submit_form

    class FakeButton:
        def __init__(self, count: int):
            self._count = count
            self.clicks = 0

        async def count(self):
            return self._count

        async def click(self, **kwargs):
            self.clicks += 1

    class FakePage:
        def __init__(self, button: FakeButton):
            self.button = button
            self.url = "https://example.test/job"

        def locator(self, selector: str):
            class _Locator:
                def __init__(loc_self, btn):
                    loc_self.first = btn

            return _Locator(self.button)

    class FakeAdapter:
        name = "greenhouse"

        def __init__(self, *, blocked: bool):
            self.blocked = blocked

        async def _guard_automation_blocks(self, page):
            if self.blocked:
                raise ManualCompletionRequired("captcha detected")

    # 1. Captcha raises before click
    btn = FakeButton(count=1)
    page = FakePage(btn)
    adapter = FakeAdapter(blocked=True)
    with pytest.raises(ManualCompletionRequired, match="captcha detected"):
        await submit_form(
            adapter,
            page,
            selectors={"submit_button": "button[type=submit]", "confirmation": ".confirmed"},
        )
    assert btn.clicks == 0

    # 2. Missing button raises SiteError before click
    btn_missing = FakeButton(count=0)
    page_missing = FakePage(btn_missing)
    adapter_ok = FakeAdapter(blocked=False)
    with pytest.raises(SiteError, match="no submit button found"):
        await submit_form(
            adapter_ok,
            page_missing,
            selectors={"submit_button": "button[type=submit]", "confirmation": ".confirmed"},
        )
    assert btn_missing.clicks == 0


async def test_preclick_captcha_in_submit_removes_attempt_fence_and_raises(committing_sessionmaker):
    maker = committing_sessionmaker
    app_id = await _application(maker)
    employer = Employer(raise_before_click=ManualCompletionRequired("captcha"))
    async with maker() as session:
        app = await session.get(Application, app_id)
        with pytest.raises(ManualCompletionRequired):
            await apply_job._submit(session, app, employer, None)
    assert employer.clicks == 0
    async with maker() as session:
        fence = await session.get(AppliedPosting, app_id)
        assert fence is None


async def test_preclick_captcha_in_handle_apply_fails_without_uncertain_fence(
    committing_sessionmaker, monkeypatch
):
    maker = committing_sessionmaker
    app_id = await _application(maker)
    employer = Employer(raise_before_click=ManualCompletionRequired("captcha"))

    async def fake_pipeline(session, application, candidate, profile):
        await apply_job._submit(session, application, employer, None)

    monkeypatch.setattr(apply_job, "_run_pipeline", fake_pipeline)

    task = QueueTask(kind="apply", payload_json={"application_id": str(app_id)}, attempts=1)
    claimed = ClaimedTask(task=task, reclaimed=False, previous_owner=None)
    async with maker() as session:
        await apply_job.handle_apply(session, claimed)
        await session.commit()

    assert employer.clicks == 0
    async with maker() as session:
        app = await session.get(Application, app_id)
        assert app.status == "failed"
        assert app.failure_reason == FailureReason.MANUAL_COMPLETION_REQUIRED.value
        assert not (app.review_json or {}).get("submission_uncertain")
        fence = await session.get(AppliedPosting, app_id)
        assert fence is None


async def test_preclick_captcha_on_released_fence_restores_released_state(
    committing_sessionmaker, monkeypatch
):
    maker = committing_sessionmaker
    app_id = await _application(maker)
    # Put fence into 'released' state
    async with maker() as session:
        app = await session.get(Application, app_id)
        from packages.core import submissions

        fence = await submissions.record(session, app, state="released", source="owner")
        await session.commit()

    employer = Employer(raise_before_click=ManualCompletionRequired("captcha"))

    async def fake_pipeline(session, application, candidate, profile):
        await apply_job._submit(session, application, employer, None)

    monkeypatch.setattr(apply_job, "_run_pipeline", fake_pipeline)

    task = QueueTask(kind="apply", payload_json={"application_id": str(app_id)}, attempts=1)
    claimed = ClaimedTask(task=task, reclaimed=False, previous_owner=None)
    async with maker() as session:
        await apply_job.handle_apply(session, claimed)
        await session.commit()

    assert employer.clicks == 0
    async with maker() as session:
        app = await session.get(Application, app_id)
        assert app.status == "failed"
        assert app.failure_reason == FailureReason.MANUAL_COMPLETION_REQUIRED.value
        fence = await session.get(AppliedPosting, app_id)
        assert fence is not None
        assert fence.state == "released"


async def test_preclick_site_error_in_handle_apply_fails_without_uncertain_fence(
    committing_sessionmaker, monkeypatch
):
    maker = committing_sessionmaker
    app_id = await _application(maker)
    employer = Employer(raise_before_click=SiteError("no submit button"))

    async def fake_pipeline(session, application, candidate, profile):
        await apply_job._submit(session, application, employer, None)

    monkeypatch.setattr(apply_job, "_run_pipeline", fake_pipeline)

    task = QueueTask(kind="apply", payload_json={"application_id": str(app_id)}, attempts=1)
    claimed = ClaimedTask(task=task, reclaimed=False, previous_owner=None)
    async with maker() as session:
        await apply_job.handle_apply(session, claimed)
        await session.commit()

    assert employer.clicks == 0
    async with maker() as session:
        app = await session.get(Application, app_id)
        assert app.status == "failed"
        assert app.failure_reason == FailureReason.SITE_ERROR.value
        assert not (app.review_json or {}).get("submission_uncertain")
        fence = await session.get(AppliedPosting, app_id)
        assert fence is None


async def test_uncertain_keys_cleared_on_retry_parking_for_required_question(
    client: AsyncClient, committing_sessionmaker, monkeypatch
):
    from contextlib import asynccontextmanager

    from packages.ats.base import FillReport, QuestionKind, UnansweredQuestion

    maker = committing_sessionmaker
    app_id = await _application(maker)
    # 1. Park with submission_uncertain via unconfirmed attempt
    async with maker() as session:
        app = await session.get(Application, app_id)
        await apply_job._submit(session, app, Employer(confirmed=False), None)
        await session.commit()

    polled = (await client.get(f"/applications/{app_id}")).json()
    assert polled["review"]["submission_uncertain"] is True
    stale_reason = polled["review"]["reason"]

    # 2. Approve via API
    approved = await client.post(f"/applications/{app_id}/review", json={"approve": True})
    assert approved.status_code == 200

    # 3. Simulate retry running the pipeline and parking for required question
    class IncompleteAdapter:
        name = "greenhouse"

        async def wait_for_posting(self, page):
            pass

        async def parse_posting(self, page):
            from packages.ats.base import ParsedPosting

            return ParsedPosting(title="Eng", description_raw="desc", closed=False)

        @staticmethod
        def matches(url: str) -> bool:
            return True

        @staticmethod
        def application_url(url: str) -> str:
            return url

        @staticmethod
        def external_id(url: str) -> str:
            return "123"

        async def wait_for_form(self, page):
            pass

        async def enumerate_fields(self, page):
            return []

        async def fill(self, page, answers):
            return FillReport(
                filled=[],
                skipped=[],
                unanswered=[
                    UnansweredQuestion(
                        key="q1",
                        question="Required question?",
                        kind=QuestionKind.TEXT,
                        required=True,
                    )
                ],
                screenshot_ref=None,
            )

    @asynccontextmanager
    async def fake_page(ats: str, **kwargs):
        class Page:
            url = "https://boards.greenhouse.io/acme/jobs/123"

            async def goto(self, url, **kw):
                return None

        yield Page()

    monkeypatch.setattr("apps.worker.apply_job.browser_page", fake_page)
    monkeypatch.setattr("apps.worker.apply_job.adapter_for", lambda url: IncompleteAdapter())

    async def noop_async(*args, **kwargs):
        return None

    monkeypatch.setattr("apps.worker.apply_job._tailor", noop_async)
    monkeypatch.setattr("apps.worker.apply_job._readiness", noop_async)

    task = QueueTask(kind="apply", payload_json={"application_id": str(app_id)}, attempts=1)
    claimed = ClaimedTask(task=task, reclaimed=False, previous_owner=None)
    async with maker() as session:
        await apply_job.handle_apply(session, claimed)
        await session.commit()

    # 4. Check GET /applications/{id}
    res = await client.get(f"/applications/{app_id}")
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "needs_review"
    review = data["review"]
    assert "submission_uncertain" not in review or not review["submission_uncertain"]
    assert "attempted_at" not in review or not review["attempted_at"]
    assert review.get("reason") != stale_reason
    assert review.get("owner_approved") is True


async def test_approve_refused_on_conflict_park(client: AsyncClient, committing_sessionmaker):
    maker = committing_sessionmaker
    app1_id = await _application(maker, url="https://boards.greenhouse.io/acme/jobs/555")
    employer = Employer()
    async with maker() as session:
        app1 = await session.get(Application, app1_id)
        await apply_job._submit(session, app1, employer, None)
        await session.commit()

    # Create app2 for the same candidate and job
    async with maker() as session:
        app1 = await session.get(Application, app1_id)
        app2 = Application(
            candidate_id=app1.candidate_id,
            profile_id=app1.profile_id,
            url="https://job-boards.greenhouse.io/acme/jobs/555?utm_source=email",
        )
        session.add(app2)
        await session.flush()
        await transition(session, app2, ApplicationStatus.RUNNING)
        await session.commit()
        app2_id = app2.id

    # Run _submit on app2 -> should park with conflict
    async with maker() as session:
        app2 = await session.get(Application, app2_id)
        await apply_job._submit(session, app2, employer, None)
        await session.commit()

    async with maker() as session:
        app2 = await session.get(Application, app2_id)
        assert app2.status == "needs_review"
        assert app2.review_json.get("submission_conflict_application_id") == str(app1_id)

    # Approve app2 via API -> must return 409 invalid_state
    resp = await client.post(f"/applications/{app2_id}/review", json={"approve": True})
    assert resp.status_code == 409, resp.text
    err = resp.json()["error"]
    assert err["code"] == "invalid_state"
    assert str(app1_id) in err["message"]
    assert "reject" in err["message"].lower()
    assert "mark submitted" in err["message"].lower()

    # Verify status unchanged and no queue task enqueued
    async with maker() as session:
        app2 = await session.get(Application, app2_id)
        assert app2.status == "needs_review"
        tasks = (
            await session.scalars(
                select(QueueTask).where(
                    QueueTask.payload_json["application_id"].as_string() == str(app2_id)
                )
            )
        ).all()
        assert len(tasks) == 0


async def test_approve_refused_on_canonical_conflict_mentions_splitting_group(
    client: AsyncClient, committing_sessionmaker
):
    from packages.core.models import Company, Posting
    from packages.core.models_jobs import CanonicalJob

    maker = committing_sessionmaker
    async with maker() as session:
        co = Company(name="Acme", domain="acme.test")
        session.add(co)
        await session.flush()
        cjob = CanonicalJob(company_id=co.id, title_key="engineer")
        session.add(cjob)
        await session.flush()
        canonical_id = cjob.id
        p1 = Posting(
            company_id=co.id,
            url="https://boards.greenhouse.io/acme/jobs/111",
            canonical_job_id=canonical_id,
            canonical_locked=False,
        )
        p2 = Posting(
            company_id=co.id,
            url="https://boards.greenhouse.io/acme/jobs/222",
            canonical_job_id=canonical_id,
            canonical_locked=False,
        )
        session.add_all([p1, p2])
        await session.commit()

    app1_id = await _application(maker, url="https://boards.greenhouse.io/acme/jobs/111")
    employer = Employer()
    async with maker() as session:
        app1 = await session.get(Application, app1_id)
        await apply_job._submit(session, app1, employer, None)
        await session.commit()

    async with maker() as session:
        app1 = await session.get(Application, app1_id)
        app2 = Application(
            candidate_id=app1.candidate_id,
            profile_id=app1.profile_id,
            url="https://boards.greenhouse.io/acme/jobs/222",
        )
        session.add(app2)
        await session.flush()
        await transition(session, app2, ApplicationStatus.RUNNING)
        await session.commit()
        app2_id = app2.id

    async with maker() as session:
        app2 = await session.get(Application, app2_id)
        await apply_job._submit(session, app2, employer, None)
        await session.commit()

    resp = await client.post(f"/applications/{app2_id}/review", json={"approve": True})
    assert resp.status_code == 409, resp.text
    err = resp.json()["error"]
    assert err["code"] == "invalid_state"
    assert "split" in err["message"].lower()


async def test_worker_whose_own_fence_is_submitted_parks_at_needs_review(
    committing_sessionmaker,
):
    maker = committing_sessionmaker
    app_id = await _application(maker)
    employer = Employer()
    # Pre-record fence as submitted (e.g. from manual submit)
    async with maker() as session:
        app = await session.get(Application, app_id)
        from packages.core import submissions

        await submissions.record(session, app, state="submitted", source="owner")
        await session.commit()

    async with maker() as session:
        app = await session.get(Application, app_id)
        assert app.status == "running"
        await apply_job._submit(session, app, employer, None)
        await session.commit()

    assert employer.clicks == 0
    async with maker() as session:
        app = await session.get(Application, app_id)
        assert app.status == "needs_review"
        reason = app.review_json.get("reason", "")
        assert "already recorded as submitted" in reason.lower()
        assert str(app_id) in reason or "owner" in reason


async def test_record_manual_on_second_app_fences_job_and_updates_source_owner(
    client: AsyncClient, committing_sessionmaker
):
    maker = committing_sessionmaker
    app1_id = await _application(maker, url="https://boards.greenhouse.io/acme/jobs/333")
    # 1. App 1 has an uncertain fence with source='worker'
    async with maker() as session:
        app1 = await session.get(Application, app1_id)
        from packages.core import submissions

        await submissions.record(session, app1, state="uncertain", source="worker")
        await session.commit()

    # Create App 2 for same candidate and job alias
    async with maker() as session:
        app1 = await session.get(Application, app1_id)
        app2 = Application(
            candidate_id=app1.candidate_id,
            profile_id=app1.profile_id,
            url="https://job-boards.greenhouse.io/acme/jobs/333?utm_campaign=xyz",
        )
        session.add(app2)
        await session.flush()
        await transition(session, app2, ApplicationStatus.RUNNING)
        await session.commit()
        app2_id = app2.id

    # 2. Mark App 2 submitted by hand
    resp = await client.post(f"/applications/{app2_id}/submitted", json={"note": "sent by hand"})
    assert resp.status_code == 200

    # Assert fence row: state == 'submitted', source == 'owner'
    async with maker() as session:
        fence = await session.get(AppliedPosting, app1_id)
        assert fence is not None
        assert fence.state == "submitted"
        assert fence.source == "owner"

        # Check App 2's ApplicationEvent
        events = (
            await session.scalars(
                select(ApplicationEvent)
                .where(
                    ApplicationEvent.application_id == app2_id,
                    ApplicationEvent.type == EventType.TRANSITION.value,
                )
                .order_by(ApplicationEvent.at.desc())
            )
        ).all()
        assert len(events) > 0
        latest_event = events[0]
        assert latest_event.payload_json.get("confirmed_fence_application_id") == str(app1_id)

    # 3. Next run of App 1 never clicks and parks with Issue 4 message
    employer = Employer()
    async with maker() as session:
        app1 = await session.get(Application, app1_id)
        await apply_job._submit(session, app1, employer, None)
        await session.commit()

    assert employer.clicks == 0
    async with maker() as session:
        app1 = await session.get(Application, app1_id)
        assert app1.status == "needs_review"
        assert "already recorded as submitted" in app1.review_json.get("reason", "").lower()


async def test_record_manual_while_other_fence_is_released(
    client: AsyncClient, committing_sessionmaker
):
    maker = committing_sessionmaker
    app1_id = await _application(maker, url="https://boards.greenhouse.io/acme/jobs/444")
    # 1. App 1 has a released fence
    async with maker() as session:
        app1 = await session.get(Application, app1_id)
        from packages.core import submissions

        await submissions.record(session, app1, state="released", source="worker")
        await session.commit()

    # Create App 2 for same candidate and job alias
    async with maker() as session:
        app1 = await session.get(Application, app1_id)
        app2 = Application(
            candidate_id=app1.candidate_id,
            profile_id=app1.profile_id,
            url="https://job-boards.greenhouse.io/acme/jobs/444?utm_source=direct",
        )
        session.add(app2)
        await session.flush()
        await transition(session, app2, ApplicationStatus.RUNNING)
        await session.commit()
        app2_id = app2.id

    # 2. Mark App 2 submitted by hand
    resp = await client.post(f"/applications/{app2_id}/submitted", json={})
    assert resp.status_code == 200

    # Assert fence row: state == 'submitted', source == 'owner'
    async with maker() as session:
        fence = await session.get(AppliedPosting, app1_id)
        assert fence is not None
        assert fence.state == "submitted"
        assert fence.source == "owner"

    # 3. Next run of App 1 never clicks and parks with Issue 4 message
    employer = Employer()
    async with maker() as session:
        app1 = await session.get(Application, app1_id)
        await apply_job._submit(session, app1, employer, None)
        await session.commit()

    assert employer.clicks == 0
    async with maker() as session:
        app1 = await session.get(Application, app1_id)
        assert app1.status == "needs_review"
        assert "already recorded as submitted" in app1.review_json.get("reason", "").lower()


async def _apply_tasks_for(maker, application_id) -> int:
    async with maker() as session:
        tasks = (await session.scalars(select(QueueTask))).all()
        return sum(
            1 for t in tasks if (t.payload_json or {}).get("application_id") == str(application_id)
        )


async def _canonical_pair(maker):
    """Two listings of one job, grouped, and an application to each.

    The first is sent; the second parks naming it. Returns both application
    ids and the second listing's posting id.
    """
    from packages.core.models import Company, Posting
    from packages.core.models_jobs import CanonicalJob

    async with maker() as session:
        co = Company(name="Acme", domain="acme.test")
        session.add(co)
        await session.flush()
        group = CanonicalJob(company_id=co.id, title_key="engineer")
        session.add(group)
        await session.flush()
        first = Posting(
            company_id=co.id,
            url="https://boards.greenhouse.io/acme/jobs/111",
            canonical_job_id=group.id,
        )
        second = Posting(
            company_id=co.id,
            url="https://boards.greenhouse.io/acme/jobs/222",
            canonical_job_id=group.id,
        )
        session.add_all([first, second])
        await session.commit()
        second_posting_id = second.id

    app1_id = await _application(maker, url="https://boards.greenhouse.io/acme/jobs/111")
    employer = Employer()
    async with maker() as session:
        await apply_job._submit(session, await session.get(Application, app1_id), employer, None)
        await session.commit()
    async with maker() as session:
        app1 = await session.get(Application, app1_id)
        app2 = Application(
            candidate_id=app1.candidate_id,
            profile_id=app1.profile_id,
            url="https://boards.greenhouse.io/acme/jobs/222",
        )
        session.add(app2)
        await session.flush()
        await transition(session, app2, ApplicationStatus.RUNNING)
        await session.commit()
        app2_id = app2.id
    async with maker() as session:
        await apply_job._submit(session, await session.get(Application, app2_id), employer, None)
        await session.commit()
    assert employer.clicks == 1
    return app1_id, app2_id, second_posting_id


async def test_splitting_the_posting_out_of_the_group_lets_approval_through(
    client: AsyncClient, committing_sessionmaker
):
    """The conflict message offers the split as a way out, so it has to be one.

    The refusal read the conflict saved on the application, which only a new
    run clears, and a new run needs the approval being refused.
    """
    maker = committing_sessionmaker
    app1_id, app2_id, second_posting_id = await _canonical_pair(maker)

    refused = await client.post(f"/applications/{app2_id}/review", json={"approve": True})
    assert refused.status_code == 409, refused.text
    assert "split" in refused.json()["error"]["message"].lower()

    split = await client.post(f"/postings/{second_posting_id}/split")
    assert split.status_code == 200, split.text

    approved = await client.post(f"/applications/{app2_id}/review", json={"approve": True})
    assert approved.status_code == 200, approved.text
    async with maker() as session:
        assert (await session.get(Application, app2_id)).status == "running"
        # The first application's record is untouched by the second's approval.
        assert (await session.get(AppliedPosting, app1_id)).state == "submitted"
        assert await session.get(AppliedPosting, app2_id) is None
    assert await _apply_tasks_for(maker, app2_id) == 1


async def test_approval_reads_the_ledger_not_what_the_park_saved(
    client: AsyncClient, committing_sessionmaker
):
    """A job sent through another application after this one parked still blocks."""
    maker = committing_sessionmaker
    app1_id = await _application(maker, url="https://boards.greenhouse.io/acme/jobs/555")
    async with maker() as session:
        app1 = await session.get(Application, app1_id)
        await transition(
            session,
            app1,
            ApplicationStatus.NEEDS_REVIEW,
            payload={"reason": "required questions could not be answered"},
        )
        app2 = Application(
            candidate_id=app1.candidate_id,
            profile_id=app1.profile_id,
            url="https://job-boards.greenhouse.io/acme/jobs/555?utm_source=mail",
        )
        session.add(app2)
        await session.flush()
        await transition(session, app2, ApplicationStatus.RUNNING)
        await session.commit()
        app2_id = app2.id
    employer = Employer()
    async with maker() as session:
        await apply_job._submit(session, await session.get(Application, app2_id), employer, None)
        await session.commit()
    assert employer.clicks == 1

    resp = await client.post(f"/applications/{app1_id}/review", json={"approve": True})
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "invalid_state"
    assert str(app2_id) in resp.json()["error"]["message"]
    async with maker() as session:
        assert (await session.get(Application, app1_id)).status == "needs_review"
    assert await _apply_tasks_for(maker, app1_id) == 0


async def test_already_submitted_park_names_the_application_sent_and_cannot_be_approved(
    client: AsyncClient, committing_sessionmaker
):
    """Marking B sent by hand confirms A's ledger row; A must say B, and stop.

    It named A itself, and approving it ran the whole pipeline again only to
    park the same way: an approval that can never send anything.
    """
    maker = committing_sessionmaker
    app1_id = await _application(maker, url="https://boards.greenhouse.io/acme/jobs/444")
    async with maker() as session:
        from packages.core import submissions

        app1 = await session.get(Application, app1_id)
        await submissions.record(session, app1, state="released", source="worker")
        app2 = Application(
            candidate_id=app1.candidate_id,
            profile_id=app1.profile_id,
            url="https://job-boards.greenhouse.io/acme/jobs/444?utm_source=direct",
        )
        session.add(app2)
        await session.flush()
        await transition(session, app2, ApplicationStatus.RUNNING)
        await session.commit()
        app2_id = app2.id
    assert (await client.post(f"/applications/{app2_id}/submitted", json={})).status_code == 200

    employer = Employer()
    async with maker() as session:
        await apply_job._submit(session, await session.get(Application, app1_id), employer, None)
        await session.commit()
    assert employer.clicks == 0
    async with maker() as session:
        app1 = await session.get(Application, app1_id)
        assert app1.status == "needs_review"
        reason = app1.review_json["reason"]
        assert "already recorded as submitted" in reason.lower()
        assert str(app2_id) in reason
        assert str(app1_id) not in reason
        assert app1.review_json["submission_recorded_application_id"] == str(app2_id)

    resp = await client.post(f"/applications/{app1_id}/review", json={"approve": True})
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "invalid_state"
    assert str(app2_id) in resp.json()["error"]["message"]
    async with maker() as session:
        assert (await session.get(Application, app1_id)).status == "needs_review"
    assert await _apply_tasks_for(maker, app1_id) == 0


async def test_another_applications_record_outranks_an_older_one_of_its_own(
    client: AsyncClient, committing_sessionmaker
):
    """Two listings of one grouped job: A's own uncertain row is older than B's sent one.

    The ledger was read earliest-first, so A found its own row, approval
    released it, and A's next run would click for a job already sent as B.
    """
    from packages.core import submissions
    from packages.core.models import Company, Posting
    from packages.core.models_jobs import CanonicalJob

    maker = committing_sessionmaker
    async with maker() as session:
        co = Company(name="Acme", domain="acme.test")
        session.add(co)
        await session.flush()
        group = CanonicalJob(company_id=co.id, title_key="engineer")
        session.add(group)
        await session.flush()
        session.add_all(
            [
                Posting(
                    company_id=co.id,
                    url="https://boards.greenhouse.io/acme/jobs/777",
                    canonical_job_id=group.id,
                ),
                Posting(
                    company_id=co.id,
                    url="https://boards.greenhouse.io/acme/jobs/888",
                    canonical_job_id=group.id,
                ),
            ]
        )
        await session.commit()

    app1_id = await _application(maker, url="https://boards.greenhouse.io/acme/jobs/777")
    async with maker() as session:
        app1 = await session.get(Application, app1_id)
        await submissions.record(session, app1, state="uncertain", source="worker")
        await transition(
            session, app1, ApplicationStatus.NEEDS_REVIEW, payload={"reason": "uncertain"}
        )
        app2 = Application(
            candidate_id=app1.candidate_id,
            profile_id=app1.profile_id,
            url="https://boards.greenhouse.io/acme/jobs/888",
        )
        session.add(app2)
        await session.flush()
        await submissions.record(session, app2, state="submitted", source="worker")
        await session.commit()
        app2_id = app2.id

    resp = await client.post(f"/applications/{app1_id}/review", json={"approve": True})
    assert resp.status_code == 409, resp.text
    assert str(app2_id) in resp.json()["error"]["message"]
    async with maker() as session:
        assert (await session.get(AppliedPosting, app1_id)).state == "uncertain"

    # And a run that reaches the submit step anyway does not click.
    async with maker() as session:
        app1 = await session.get(Application, app1_id)
        await transition(session, app1, ApplicationStatus.RUNNING)
        await session.commit()
    employer = Employer()
    async with maker() as session:
        await apply_job._submit(session, await session.get(Application, app1_id), employer, None)
        await session.commit()
    assert employer.clicks == 0
    async with maker() as session:
        app1 = await session.get(Application, app1_id)
        assert app1.status == "needs_review"
        assert app1.review_json["submission_conflict_application_id"] == str(app2_id)
