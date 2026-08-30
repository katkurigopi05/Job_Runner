"""Telling the owner an application is waiting on them.

The rule these tests exist to hold is §2.5: this is the *human* half of the
CAPTCHA boundary, not a way around it. Nothing here defeats a challenge. When
a site blocks automation the application still fails as
`manual_completion_required`; the owner just finds out and finishes it by hand.
"""

from __future__ import annotations

import pytest

from packages.core.enums import ApplicationStatus, FailureReason
from packages.core.notify import (
    Notification,
    ParkReason,
    deliver,
    needs_owner,
)


def _note(reason: ParkReason = ParkReason.MANUAL) -> Notification:
    return Notification(
        application_id="abc",
        reason=reason,
        company="Acme",
        role="Backend Engineer",
        url="http://localhost:3001/applications/abc",
    )


# --------------------------------------------------------------------------
# Which states want a person
# --------------------------------------------------------------------------


def test_the_three_parked_states_are_recognised() -> None:
    assert needs_owner(ApplicationStatus.NEEDS_REVIEW.value) is ParkReason.REVIEW
    assert needs_owner(ApplicationStatus.NEEDS_OTP.value) is ParkReason.OTP
    assert (
        needs_owner(
            ApplicationStatus.FAILED.value,
            FailureReason.MANUAL_COMPLETION_REQUIRED.value,
        )
        is ParkReason.MANUAL
    )


@pytest.mark.parametrize(
    "status", [ApplicationStatus.QUEUED, ApplicationStatus.RUNNING, ApplicationStatus.SUBMITTED]
)
def test_states_that_need_nobody_are_quiet(status: ApplicationStatus) -> None:
    assert needs_owner(status.value) is None


@pytest.mark.parametrize(
    "reason",
    [
        FailureReason.JOB_CLOSED,
        FailureReason.UNSUPPORTED_SITE,
        FailureReason.REJECTED_AT_REVIEW,
        FailureReason.SITE_ERROR,
    ],
)
def test_only_one_kind_of_failure_wants_the_owner(reason: FailureReason) -> None:
    """A closed job is not a task. Telling the owner would be noise."""
    assert needs_owner(ApplicationStatus.FAILED.value, reason.value) is None


def test_the_manual_reason_says_what_happened_and_what_to_do() -> None:
    body = _note(ParkReason.MANUAL).body
    assert "by hand" in body
    assert "blocked automation" in body


# --------------------------------------------------------------------------
# What leaves the machine
# --------------------------------------------------------------------------


def test_the_payload_carries_no_application_material() -> None:
    """§2.8 permits one third-party upload and a notification is not it.

    An id, a status, the company and role, and a local link. Adding a field to
    `as_dict` sends it off-machine, which is why this asserts the exact keys
    rather than merely the absence of a résumé.
    """
    assert set(_note().as_dict()) == {
        "application_id",
        "reason",
        "company",
        "role",
        "url",
        "title",
        "body",
    }


def test_nothing_is_sent_when_nothing_is_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """The shipped default. A log line, and nowhere else."""
    monkeypatch.delenv("NOTIFY_BACKENDS", raising=False)
    from packages.core.config import get_settings

    get_settings.cache_clear()

    import asyncio

    assert asyncio.run(deliver(_note())) == []


def test_a_backend_that_fails_does_not_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    """An application that parked correctly must not fail because the
    doorbell broke."""
    import asyncio

    monkeypatch.setenv("NOTIFY_BACKENDS", "webhook")
    monkeypatch.setenv("NOTIFY_WEBHOOK_URL", "http://127.0.0.1:1/nope")
    from packages.core.config import get_settings

    get_settings.cache_clear()

    assert asyncio.run(deliver(_note())) == []


def test_an_unknown_backend_is_reported_not_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    monkeypatch.setenv("NOTIFY_BACKENDS", "carrier-pigeon")
    from packages.core.config import get_settings

    get_settings.cache_clear()

    assert asyncio.run(deliver(_note())) == []


def test_the_log_backend_reports_success(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    monkeypatch.setenv("NOTIFY_BACKENDS", "log")
    from packages.core.config import get_settings

    get_settings.cache_clear()

    assert asyncio.run(deliver(_note())) == ["log"]


def test_the_webhook_posts_the_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    """The one backend that leaves the machine, checked end to end."""
    import asyncio

    import httpx

    sent: dict = {}

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json, timeout):
            sent["url"] = url
            sent["json"] = json
            return httpx.Response(200, request=httpx.Request("POST", url))

    monkeypatch.setenv("NOTIFY_BACKENDS", "webhook")
    monkeypatch.setenv("NOTIFY_WEBHOOK_URL", "https://example.invalid/hook")
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: FakeClient())
    from packages.core.config import get_settings

    get_settings.cache_clear()

    assert asyncio.run(deliver(_note())) == ["webhook"]
    assert sent["url"] == "https://example.invalid/hook"
    assert sent["json"]["reason"] == "manual_completion_required"
    assert "résumé" not in str(sent["json"])


def test_the_title_survives_a_posting_with_no_company() -> None:
    bare = Notification(application_id="x", reason=ParkReason.REVIEW)
    assert bare.title == "Jobrunner"
    assert bare.body


# --------------------------------------------------------------------------
# Against a real database — the idempotency is the part that can misbehave
# --------------------------------------------------------------------------


async def test_a_parked_application_rings_once(
    db_session, application, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The queue is at-least-once, so a re-run must not tell the owner twice."""
    from packages.core.notify import notify_if_parked

    monkeypatch.setenv("NOTIFY_BACKENDS", "log")
    from packages.core.config import get_settings

    get_settings.cache_clear()

    application.status = ApplicationStatus.NEEDS_REVIEW.value
    await db_session.flush()

    assert await notify_if_parked(db_session, application.id) is ParkReason.REVIEW
    assert await notify_if_parked(db_session, application.id) is None, "rang twice"


async def test_parking_a_second_time_rings_again(
    db_session, application, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Approved, resumed, then blocked by the site — a second thing to do.

    Keyed on the reason rather than on the application, so this is a new
    request for the owner's attention and not a repeat of the old one.
    """
    from packages.core.notify import notify_if_parked

    monkeypatch.setenv("NOTIFY_BACKENDS", "log")
    from packages.core.config import get_settings

    get_settings.cache_clear()

    application.status = ApplicationStatus.NEEDS_REVIEW.value
    await db_session.flush()
    assert await notify_if_parked(db_session, application.id) is ParkReason.REVIEW

    application.status = ApplicationStatus.FAILED.value
    application.failure_reason = FailureReason.MANUAL_COMPLETION_REQUIRED.value
    await db_session.flush()
    assert await notify_if_parked(db_session, application.id) is ParkReason.MANUAL


async def test_an_application_nobody_needs_to_see_is_quiet(db_session, application) -> None:
    from packages.core.notify import notify_if_parked

    application.status = ApplicationStatus.SUBMITTED.value
    await db_session.flush()

    assert await notify_if_parked(db_session, application.id) is None


async def test_a_missing_application_is_not_an_error(db_session) -> None:
    import uuid as _uuid

    from packages.core.notify import notify_if_parked

    assert await notify_if_parked(db_session, _uuid.uuid4()) is None
