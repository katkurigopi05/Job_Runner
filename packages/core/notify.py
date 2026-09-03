"""Tell the owner when an application is waiting on them.

An application parks in three ways and, until this existed, all three were
silent. `needs_review` waits for approval, `needs_otp` waits for a code, and
`failed[manual_completion_required]` waits for the owner to finish the form by
hand. The state was recorded, the dashboard showed it, and nothing said so —
which on a queue whose whole promise is "nothing submits without you" means
the owner has to remember to go and look.

## This is not a CAPTCHA workaround

§2.5 makes captcha-solving, bot-detection evasion and proxy rotation a hard
scope boundary, and this does not touch it. Nothing here defeats a challenge or
makes the browser look human. When a site blocks automation the application
still fails as `manual_completion_required`; the only change is that the owner
finds out promptly and finishes it themselves, in their own browser, as a
person. The work moves to a human rather than around a control.

That distinction is the whole design, and it is why the notification carries a
link to the local dashboard rather than anything resembling an automated
retry.

## What leaves the machine, and when

Nothing, by default. `NOTIFY_BACKENDS` is unset and the only record is a log
line — the same shipped-default-costs-nothing rule as the LLM providers.

- **`log`** — structlog, always available, goes nowhere.
- **`desktop`** — `notify-send` or `osascript`, whichever the OS has. Local,
  free, no dependency, and silently unavailable over SSH, which is reported
  rather than raised.
- **`webhook`** — a URL the owner supplies. This one *does* leave the machine,
  so it is opt-in by naming it and the payload is deliberately thin: the
  application id, its status, the company and role, and a localhost link.
  Never the résumé, the answers, the posting body, or the screenshot. §2.8
  permits one third-party upload and a notification is not it.

A webhook is also how the owner gets a phone alert without this project taking
a dependency on anyone's service: point it at ntfy, a Telegram bot, a Slack
hook, whatever they already run. That choice stays theirs, and none of it ends
up in `requirements.txt`.

## Failure is never the application's problem

Every backend is wrapped. A webhook that times out, a desktop daemon that is
not running, a malformed URL — all are logged and swallowed. An application
that parked correctly must not then be marked failed because the doorbell
broke.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from enum import StrEnum

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.enums import ApplicationStatus, EventType, FailureReason

log = structlog.get_logger(__name__)

__all__ = [
    "Notification",
    "ParkReason",
    "deliver",
    "needs_owner",
]

#: How long a webhook may take before it is abandoned. Short on purpose: the
#: worker is holding a browser open and the next application is waiting.
WEBHOOK_TIMEOUT_S = 5.0


class ParkReason(StrEnum):
    """Why the owner is needed. Each wants a different action from them."""

    #: The form is filled and waiting for approve, edit, or reject.
    REVIEW = "review"
    #: The site sent a one-time code that only the owner can read.
    OTP = "otp"
    #: The site blocked automation. §2.5 — the owner finishes it by hand.
    MANUAL = "manual_completion_required"


#: What each reason asks the owner to actually do.
_ASKS: dict[ParkReason, str] = {
    ParkReason.REVIEW: "review and approve it",
    ParkReason.OTP: "enter the one-time code",
    ParkReason.MANUAL: "finish this one by hand — the site blocked automation",
}


def needs_owner(status: str, failure_reason: str | None = None) -> ParkReason | None:
    """Whether this state is waiting on a person, and for what.

    A pure function of the two recorded fields, so the answer cannot drift from
    what the state machine wrote. `failed` is only interesting for one reason:
    every other failure is the pipeline's to own, and telling the owner that a
    job closed is noise they cannot act on.
    """
    if status == ApplicationStatus.NEEDS_REVIEW.value:
        return ParkReason.REVIEW
    if status == ApplicationStatus.NEEDS_OTP.value:
        return ParkReason.OTP
    if (
        status == ApplicationStatus.FAILED.value
        and failure_reason == FailureReason.MANUAL_COMPLETION_REQUIRED.value
    ):
        return ParkReason.MANUAL
    return None


@dataclass(frozen=True)
class Notification:
    """One thing the owner needs to know about.

    Deliberately narrow. Everything here is either an identifier or something
    the owner already told the system; none of it is the material §2.8
    protects, and a backend that sends this off-machine sends only this.
    """

    application_id: str
    reason: ParkReason
    company: str = ""
    role: str = ""
    #: Where to go and do something about it.
    url: str = ""

    @property
    def title(self) -> str:
        """Notification title including company and role."""
        where = " — ".join(part for part in (self.company, self.role) if part)
        return f"Jobrunner: {where}" if where else "Jobrunner"

    @property
    def body(self) -> str:
        """Notification body text explaining what action is needed."""
        return _ASKS[self.reason]

    def as_dict(self) -> dict[str, str]:
        """The webhook payload. Adding a field here sends it off-machine."""
        return {
            "application_id": self.application_id,
            "reason": self.reason.value,
            "company": self.company,
            "role": self.role,
            "url": self.url,
            "title": self.title,
            "body": self.body,
        }


def _configured_backends() -> list[str]:
    from packages.core.config import get_settings

    raw = get_settings().notify_backends or ""
    return [name.strip().lower() for name in raw.split(",") if name.strip()]


def _desktop(notification: Notification) -> None:
    """A local desktop notification, if this machine has a way to show one."""
    if shutil.which("notify-send"):
        command = ["notify-send", notification.title, notification.body]
    elif shutil.which("osascript"):
        script = f"display notification {notification.body!r} with title {notification.title!r}"
        command = ["osascript", "-e", script]
    else:
        log.info("notify_desktop_unavailable", reason="no notify-send or osascript")
        return
    subprocess.run(command, check=False, capture_output=True, timeout=WEBHOOK_TIMEOUT_S)


async def _webhook(notification: Notification) -> None:
    """POST the thin payload to whatever the owner pointed this at."""
    import httpx

    from packages.core.config import get_settings

    url = get_settings().notify_webhook_url
    if not url:
        log.warning("notify_webhook_unconfigured", hint="set NOTIFY_WEBHOOK_URL")
        return
    async with httpx.AsyncClient() as client:
        response = await client.post(url, json=notification.as_dict(), timeout=WEBHOOK_TIMEOUT_S)
        response.raise_for_status()


async def deliver(notification: Notification) -> list[str]:
    """Send `notification` to every configured backend. Never raises.

    Returns the backends that succeeded, which is what the tests assert against
    and what the log line reports. A backend that fails is logged by name — a
    notification system that fails silently is worse than none, because the
    owner then trusts a doorbell that does not ring.
    """
    log.info(
        "application_needs_owner",
        application_id=notification.application_id,
        reason=notification.reason.value,
    )

    delivered: list[str] = []
    for backend in _configured_backends():
        try:
            if backend == "log":
                delivered.append(backend)
            elif backend == "desktop":
                await asyncio.to_thread(_desktop, notification)
                delivered.append(backend)
            elif backend == "webhook":
                await _webhook(notification)
                delivered.append(backend)
            else:
                log.warning("notify_backend_unknown", backend=backend)
        except Exception as exc:  # noqa: BLE001 — the doorbell must not fail the run
            # Never the exception body: a webhook error can echo the URL, and
            # the URL can carry a token.
            log.warning("notify_backend_failed", backend=backend, error=type(exc).__name__)
    return delivered


async def notify_if_parked(session: AsyncSession, application_id: uuid.UUID) -> ParkReason | None:
    """Tell the owner if this application is now waiting on them.

    Call **after** the transaction that parked it has committed. Firing inside
    `state.transition()` would be the obvious place — it is the one function
    that changes a status — but it deliberately does not commit, so a
    notification sent there announces an application that may still roll back.
    A doorbell that rings for work that did not happen is worse than a late one.

    Idempotent, because the queue is at-least-once: a `notified` event is
    written after delivery and a second run for the same status finds it and
    stays quiet. Keyed on the status, not merely on the application, so an
    application that parks, resumes and parks again does ring twice — that is
    a second thing to do, not a repeat of the first.
    """
    from sqlalchemy import select

    from packages.core.models import Application, ApplicationEvent, Company, Posting

    application = await session.get(Application, application_id)
    if application is None:
        return None

    reason = needs_owner(application.status, application.failure_reason)
    if reason is None:
        return None

    already = await session.scalar(
        select(ApplicationEvent.id)
        .where(
            ApplicationEvent.application_id == application.id,
            ApplicationEvent.type == EventType.NOTIFIED.value,
            ApplicationEvent.payload_json["reason"].astext == reason.value,
        )
        .limit(1)
    )
    if already is not None:
        return None

    company = role = ""
    if application.posting_id is not None:
        posting = await session.get(Posting, application.posting_id)
        if posting is not None:
            role = posting.title or ""
            if posting.company_id is not None:
                record = await session.get(Company, posting.company_id)
                company = (record.name if record else "") or ""

    from packages.core.config import get_settings

    base = get_settings().dashboard_url.rstrip("/")
    delivered = await deliver(
        Notification(
            application_id=str(application.id),
            reason=reason,
            company=company,
            role=role,
            url=f"{base}/applications/{application.id}",
        )
    )

    session.add(
        ApplicationEvent(
            application_id=application.id,
            type=EventType.NOTIFIED.value,
            payload_json={"reason": reason.value, "delivered": delivered},
        )
    )
    await session.commit()
    return reason
