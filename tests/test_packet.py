"""The handoff packet — what the owner gets when the run stops short.

Every ATS this project supports mounts a captcha at the fill stage, and §2.5
rules out working around one. So the last step is the owner's, and the packet
is the whole difference between "here is a URL, good luck" and a form already
answered with a file ready to attach.

These pin the parts that would fail quietly: a missing tailored résumé
silently handing back nothing, and an unanswered required question silently
reporting the application as ready to send.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio


async def _application(client: AsyncClient, candidate: dict[str, str], url: str) -> str:
    created = await client.post(
        "/applications",
        json={
            "candidate_id": candidate["candidate_id"],
            "profile_id": candidate["profile_id"],
            "url": url,
        },
    )
    assert created.status_code == 201, created.text
    return str(created.json()["id"])


async def test_packet_falls_back_to_the_base_resume(
    client: AsyncClient, complete_candidate: dict[str, str]
) -> None:
    """Tailoring is allowed to fail. Handing back no file is not.

    `tailored_resume_id` is null until a run renders one, and on a machine
    without WeasyPrint's system libraries it stays null. The owner still needs
    something to upload, so the base résumé comes back with `is_tailored`
    false rather than the packet reporting no résumé at all.
    """
    application_id = await _application(
        client, complete_candidate, "https://boards.greenhouse.io/acme/jobs/1"
    )

    response = await client.get(f"/applications/{application_id}/packet")

    assert response.status_code == 200, response.text
    packet = response.json()
    assert packet["resume"] is not None
    assert packet["resume"]["is_tailored"] is False
    assert packet["resume"]["download_path"].endswith("/file")


async def test_packet_carries_the_apply_url_and_status(
    client: AsyncClient, complete_candidate: dict[str, str]
) -> None:
    url = "https://boards.greenhouse.io/acme/jobs/2"
    application_id = await _application(client, complete_candidate, url)

    packet = (await client.get(f"/applications/{application_id}/packet")).json()

    assert packet["apply_url"] == url
    assert packet["application_id"] == application_id
    assert packet["status"] == "queued"


async def test_a_required_unanswered_question_blocks_ready_to_submit(
    client: AsyncClient, complete_candidate: dict[str, str], worker_session
) -> None:
    """§2.4 — an unanswerable question parks the application.

    The packet must not describe such a run as ready to send. It is exactly
    the case where the owner has to read the employer's wording and decide,
    and a green light here would invite them to submit an incomplete form.
    """
    import uuid as _uuid

    from packages.core.models import Application

    application_id = await _application(
        client, complete_candidate, "https://boards.greenhouse.io/acme/jobs/3"
    )
    application = await worker_session.get(Application, _uuid.UUID(application_id))
    application.review_json = {
        "screenshot_ref": "receipts/missing.png",
        "filled": [{"key": "name", "label": "Full name", "kind": "text", "value": "Test Owner"}],
        "unanswered": [
            {
                "key": "why_us",
                "question": "Why do you want to work here?",
                "kind": "textarea",
                "required": True,
            }
        ],
    }
    await worker_session.commit()

    packet = (await client.get(f"/applications/{application_id}/packet")).json()

    assert packet["ready_to_submit"] is False
    # The employer's exact wording, not a paraphrase.
    assert packet["unanswered"][0]["question"] == "Why do you want to work here?"
    assert packet["answers"] == [{"question": "Full name", "value": "Test Owner"}]


async def test_redacted_answers_are_left_out(
    client: AsyncClient, complete_candidate: dict[str, str], worker_session
) -> None:
    """File uploads and redacted fields carry no value.

    Listing them would put blank rows on the handoff screen, which reads as
    "we failed to answer this" rather than "there is nothing to copy".
    """
    import uuid as _uuid

    from packages.core.models import Application

    application_id = await _application(
        client, complete_candidate, "https://boards.greenhouse.io/acme/jobs/4"
    )
    application = await worker_session.get(Application, _uuid.UUID(application_id))
    application.review_json = {
        "filled": [
            {"key": "resume", "label": "Résumé", "kind": "file", "value": None},
            {"key": "email", "label": "Email", "kind": "email", "value": "owner@example.com"},
        ],
        "unanswered": [],
    }
    await worker_session.commit()

    packet = (await client.get(f"/applications/{application_id}/packet")).json()

    assert packet["answers"] == [{"question": "Email", "value": "owner@example.com"}]


async def test_unknown_application_is_a_404(client: AsyncClient) -> None:
    import uuid as _uuid

    response = await client.get(f"/applications/{_uuid.uuid4()}/packet")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"
