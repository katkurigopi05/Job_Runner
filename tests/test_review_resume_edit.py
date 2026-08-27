"""Editing, on the review screen, the résumé that is about to be sent.

The screen showed the attached document and could not change it. So a tailored
bullet that read wrong left two options — reject the application, or send it
anyway — and editing the base on the résumés page did not help, because a
résumé already tailored for this posting is not the base.

What makes this more than a second edit form is that the edit has to *survive
approval*. Approving resumes the pipeline from the top, and every path in
`_tailor` assigns `tailored_resume_id`. Without a pin the owner's edit is
visible on this screen and absent from the file the employer receives, which is
the CLAUDE.md §15 defect exactly: a review screen describing a document other
than the one being uploaded.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient

from packages.core.enums import ApplicationStatus

APPLY_URL = "https://boards.greenhouse.io/acme/jobs/4242"

EDIT = {
    "contact": {"name": "Ada Lovelace", "email": "ada@example.com"},
    "sections": {"experience": ["Staff Engineer, Analytical Engines Ltd — led the rewrite."]},
}


async def _parked(
    client: AsyncClient, candidate: dict[str, str], worker_session, *, url: str = APPLY_URL
) -> str:
    """An application sitting at `needs_review`, which is where this screen lives."""
    from packages.core.models import Application

    created = await client.post("/applications", json={**candidate, "url": url})
    assert created.status_code == 201, created.text
    application_id = created.json()["id"]

    application = await worker_session.get(Application, uuid.UUID(application_id))
    application.status = ApplicationStatus.NEEDS_REVIEW.value
    await worker_session.commit()
    return str(application_id)


# --------------------------------------------------------------------------
# The route
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_edit_becomes_the_document_this_application_sends(
    client: AsyncClient, complete_candidate: dict[str, str], worker_session
) -> None:
    """`tailored_resume_id` is what `_resume_path` uploads."""
    application_id = await _parked(client, complete_candidate, worker_session)

    edited = await client.post(f"/applications/{application_id}/resume/edit", json=EDIT)

    assert edited.status_code == 200, edited.text
    attached = edited.json()["tailored_resume_id"]
    assert attached is not None

    parsed = await client.get(f"/resumes/{attached}/parsed")
    assert "led the rewrite" in str(parsed.json()["parsed"])


@pytest.mark.asyncio
async def test_it_does_not_move_the_profile_base(
    client: AsyncClient, complete_candidate: dict[str, str], worker_session
) -> None:
    """The subject of this screen is one employer.

    A résumé tailored for one posting is a poor starting point for the next,
    and adopting it silently would make every future application inherit this
    job's phrasing from a screen that never mentioned them.
    """
    application_id = await _parked(client, complete_candidate, worker_session)
    before = (await client.get("/profiles")).json()
    base_before = next(
        p["base_resume_id"] for p in before if p["id"] == complete_candidate["profile_id"]
    )

    await client.post(f"/applications/{application_id}/resume/edit", json=EDIT)

    after = (await client.get("/profiles")).json()
    base_after = next(
        p["base_resume_id"] for p in after if p["id"] == complete_candidate["profile_id"]
    )
    assert base_after == base_before


@pytest.mark.asyncio
async def test_adopt_opts_in_to_the_wider_change(
    client: AsyncClient, complete_candidate: dict[str, str], worker_session
) -> None:
    """A typo or a changed phone number is true everywhere, and the owner says so."""
    application_id = await _parked(client, complete_candidate, worker_session)

    edited = await client.post(
        f"/applications/{application_id}/resume/edit", json={**EDIT, "adopt": True}
    )
    attached = edited.json()["tailored_resume_id"]

    profiles = (await client.get("/profiles")).json()
    base = next(
        p["base_resume_id"] for p in profiles if p["id"] == complete_candidate["profile_id"]
    )
    assert base == attached


@pytest.mark.asyncio
async def test_the_edit_is_a_new_version_with_a_file_of_its_own(
    client: AsyncClient, complete_candidate: dict[str, str], worker_session
) -> None:
    """`_resume_path` uploads the *file*, so an unrendered edit is invisible.

    The same requirement `/resumes/{id}/edit` has, checked here too because
    these are two routes and only a shared implementation keeps them equal.
    """
    application_id = await _parked(client, complete_candidate, worker_session)

    edited = await client.post(f"/applications/{application_id}/resume/edit", json=EDIT)
    attached = edited.json()["tailored_resume_id"]

    downloaded = await client.get(f"/resumes/{attached}/file")
    assert downloaded.status_code == 200
    assert downloaded.content[:4] == b"%PDF"


@pytest.mark.asyncio
async def test_editing_an_application_that_is_not_parked_is_refused(
    client: AsyncClient, complete_candidate: dict[str, str]
) -> None:
    """A running application is mid-fill; a submitted one has already sent its file.

    Editing either changes a screen without changing anything an employer sees.
    """
    created = await client.post("/applications", json={**complete_candidate, "url": APPLY_URL})

    r = await client.post(f"/applications/{created.json()['id']}/resume/edit", json=EDIT)

    assert r.status_code == 409
    assert r.json()["error"]["code"] == "invalid_state"


@pytest.mark.asyncio
async def test_an_emptied_resume_is_refused(
    client: AsyncClient, complete_candidate: dict[str, str], worker_session
) -> None:
    """An empty document would still render a PDF, and it would still be uploaded."""
    application_id = await _parked(client, complete_candidate, worker_session)

    r = await client.post(
        f"/applications/{application_id}/resume/edit",
        json={"contact": {}, "sections": {"experience": ["  ", ""]}},
    )

    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_request"


@pytest.mark.asyncio
async def test_the_stored_diff_is_marked_rather_than_left_describing_the_old_file(
    client: AsyncClient, complete_candidate: dict[str, str], worker_session
) -> None:
    """The owner is looking at that diff the moment they save.

    Kept rather than cleared — it is still the honest account of what tailoring
    did to the document theirs came from, and it carries the guard's refusal
    count. What it must not do is go on looking like a description of the file
    about to be uploaded.
    """
    from packages.core.models import Application

    application_id = await _parked(client, complete_candidate, worker_session)
    application = await worker_session.get(Application, uuid.UUID(application_id))
    application.review_json = {"resume_diff": {"changed": 3, "rejected": 1, "unchanged": 2}}
    await worker_session.commit()

    await client.post(f"/applications/{application_id}/resume/edit", json=EDIT)

    application = await worker_session.get(Application, uuid.UUID(application_id))
    await worker_session.refresh(application)
    diff = application.review_json["resume_diff"]
    assert diff["owner_pinned"] == "owner_edit"
    # The counts survive. Losing `rejected` would lose the one number that says
    # whether the model kept trying to invent.
    assert diff["rejected"] == 1


@pytest.mark.asyncio
async def test_an_application_with_no_resume_says_so(
    client: AsyncClient, complete_candidate: dict[str, str], worker_session
) -> None:
    """There is nothing to derive an edit from, and a blank one is not a résumé.

    Only reachable by clearing the base after the fact — the completeness gate
    refuses to create an application without one — which is exactly how it
    happens: the owner unsets it while something is already parked.
    """
    from packages.core.models import Application, Profile

    application_id = await _parked(client, complete_candidate, worker_session)
    profile = await worker_session.get(Profile, uuid.UUID(complete_candidate["profile_id"]))
    profile.base_resume_id = None
    await worker_session.commit()

    r = await client.post(f"/applications/{application_id}/resume/edit", json=EDIT)

    assert r.status_code == 400
    assert "no résumé attached" in r.json()["error"]["message"]

    # Untouched: nothing was attached and nothing was invented.
    application = await worker_session.get(Application, uuid.UUID(application_id))
    await worker_session.refresh(application)
    assert application.tailored_resume_id is None


# --------------------------------------------------------------------------
# Surviving approval
# --------------------------------------------------------------------------
#
# The half with consequences. Approving a parked application resumes the
# pipeline from the top, so `_tailor` runs a *second* time — and every path in
# it assigns `tailored_resume_id`. Whatever the owner decided on the review
# screen was silently replaced on the way to the employer, with nothing
# anywhere reporting the swap.


class _Posting:
    """What the adapter hands `_tailor` — a parsed page, not a stored row."""

    description_raw = "Backend engineer. Python, Postgres, Kubernetes."


async def _pipeline_fixtures(db_session, *, pinned_source: str | None):
    """An application whose attached résumé the owner chose, ready to re-tailor."""
    from packages.core.models import Application, Candidate, Profile, Resume, User
    from packages.core.storage import get_storage, resume_key

    suffix = uuid.uuid4().hex[:8]
    user = User(email=f"u-{suffix}@example.com")
    db_session.add(user)
    await db_session.flush()
    candidate = Candidate(user_id=user.id, name="Owner", email=f"c-{suffix}@example.com")
    db_session.add(candidate)
    await db_session.flush()

    storage = get_storage()
    lines = ["Built backend services in Python."]
    base_key = resume_key(str(candidate.id), 1, "resume.pdf")
    storage.put(base_key, b"%PDF-1.4 base")
    base = Resume(
        candidate_id=candidate.id,
        version=1,
        storage_ref=base_key,
        parsed_json={"raw_lines": lines, "sections": {"experience": lines}},
    )
    db_session.add(base)

    chosen_key = resume_key(str(candidate.id), 2, "chosen.pdf")
    storage.put(chosen_key, b"%PDF-1.4 chosen")
    chosen = Resume(
        candidate_id=candidate.id,
        version=2,
        storage_ref=chosen_key,
        parsed_json={"raw_lines": lines, "sections": {"experience": lines}},
        tailored_by="ollama:llama3.1",
    )
    db_session.add(chosen)
    await db_session.flush()

    profile = Profile(candidate_id=candidate.id, label="default", base_resume_id=base.id)
    db_session.add(profile)
    await db_session.flush()

    review: dict = {"resume_diff": {"changed": 2, "unchanged": 1, "rejected": 0}}
    if pinned_source is not None:
        review["resume_pinned"] = {"resume_id": str(chosen.id), "source": pinned_source}

    application = Application(
        candidate_id=candidate.id,
        profile_id=profile.id,
        url=f"https://x.test/{suffix}",
        status=ApplicationStatus.RUNNING.value,
        tailored_resume_id=chosen.id,
        review_json=review,
    )
    db_session.add(application)
    await db_session.flush()
    return application, profile, chosen


@pytest.mark.parametrize("source", ["owner_edit", "comparison"])
@pytest.mark.asyncio
async def test_the_owners_choice_survives_the_resumed_run(db_session, source: str) -> None:
    """The defect this pin exists for.

    Both ways of choosing land in the same place: `tailored_resume_id` set on a
    parked application. Approving re-enters `_tailor`, which would reattach
    whatever the router's default provider produces or has cached — so the
    screen showed the owner's document and the employer got the other one.
    """
    from apps.worker import apply_job

    application, profile, chosen = await _pipeline_fixtures(db_session, pinned_source=source)

    diff = await apply_job._tailor(db_session, application, profile, _Posting())

    assert application.tailored_resume_id == chosen.id
    assert diff is not None
    assert diff["owner_pinned"] == source


@pytest.mark.asyncio
async def test_the_pin_reports_the_model_that_wrote_the_document(db_session) -> None:
    """§7's fallback matters here as much as anywhere.

    A résumé written by llama3.1 after the remote allowance ran out is a
    different document from one written by Gemini, and this screen is where the
    owner decides whether to send it.
    """
    from apps.worker import apply_job

    application, profile, _ = await _pipeline_fixtures(db_session, pinned_source="owner_edit")

    diff = await apply_job._tailor(db_session, application, profile, _Posting())

    assert diff["answered_by"] == "ollama:llama3.1"


@pytest.mark.asyncio
async def test_a_pin_pointing_somewhere_else_does_not_freeze_the_run(db_session) -> None:
    """If the row no longer agrees with the pin, something else moved it.

    Re-tailoring is the safer answer than uploading a document the application
    is no longer attached to.
    """
    from apps.worker import apply_job

    application, profile, _ = await _pipeline_fixtures(db_session, pinned_source="owner_edit")
    application.review_json = {
        **application.review_json,
        "resume_pinned": {"resume_id": str(uuid.uuid4()), "source": "owner_edit"},
    }

    diff = await apply_job._tailor(db_session, application, profile, _Posting())

    assert diff is None or "owner_pinned" not in diff


@pytest.mark.asyncio
async def test_without_a_pin_the_run_tailors_as_it_always_did(db_session) -> None:
    """The pin narrows one case; it must not become the default path."""
    from apps.worker import apply_job

    application, profile, _ = await _pipeline_fixtures(db_session, pinned_source=None)

    diff = await apply_job._tailor(db_session, application, profile, _Posting())

    assert diff is None or "owner_pinned" not in diff


# --------------------------------------------------------------------------
# Reading what is attached
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_attached_resume_is_answered_by_the_api_not_the_caller(
    client: AsyncClient, complete_candidate: dict[str, str], worker_session
) -> None:
    """One definition of "which résumé", shared by every caller.

    A client working it out for itself would eventually disagree with the
    uploader, and that disagreement is invisible until an employer gets the
    wrong file.
    """
    application_id = await _parked(client, complete_candidate, worker_session)

    r = await client.get(f"/applications/{application_id}/resume")

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["is_tailored"] is False
    assert body["editable"] is True
    # The lines themselves, not counts — the caller is about to edit them.
    assert isinstance(body["sections"]["experience"], list)


@pytest.mark.asyncio
async def test_a_running_application_reports_itself_as_not_editable(
    client: AsyncClient, complete_candidate: dict[str, str]
) -> None:
    """The screen and the tool should agree with the route that would refuse."""
    created = await client.post("/applications", json={**complete_candidate, "url": APPLY_URL})

    r = await client.get(f"/applications/{created.json()['id']}/resume")

    assert r.status_code == 200, r.text
    assert r.json()["editable"] is False


# --------------------------------------------------------------------------
# The guard, on the path where the author is a model
# --------------------------------------------------------------------------
#
# The dashboard editor is not guarded: §2.1 constrains the model, not the owner
# writing their own history. A tool call is the other case — there the author
# *is* a model, and an unguarded résumé write handed to one is the door §2.1
# exists to close.


@pytest.mark.asyncio
async def test_guarded_rephrasing_is_allowed(
    client: AsyncClient, complete_candidate: dict[str, str], worker_session
) -> None:
    """§2.1 permits rephrasing, reordering and re-emphasis."""
    application_id = await _parked(client, complete_candidate, worker_session)
    current = (await client.get(f"/applications/{application_id}/resume")).json()

    r = await client.post(
        f"/applications/{application_id}/resume/edit",
        json={
            "contact": current["contact"],
            # Reordered and trimmed; every claim already in the source.
            "sections": {"experience": current["sections"]["experience"]},
            "guard": True,
        },
    )

    assert r.status_code == 200, r.text


@pytest.mark.asyncio
async def test_a_guarded_edit_that_invents_an_employer_is_refused(
    client: AsyncClient, complete_candidate: dict[str, str], worker_session
) -> None:
    """The failure this flag exists to prevent.

    An assistant putting an employer the owner never had onto a document going
    to a real employer, under the owner's name, with no check anywhere.
    """
    application_id = await _parked(client, complete_candidate, worker_session)

    r = await client.post(
        f"/applications/{application_id}/resume/edit",
        json={
            "contact": {"name": "Ada Lovelace"},
            "sections": {"experience": ["Principal Engineer at Netflix, cutting latency by 40%."]},
            "guard": True,
        },
    )

    assert r.status_code == 400, r.text
    message = r.json()["error"]["message"]
    assert "does not support" in message
    # Names what it refused, so the owner can be told exactly what was dropped.
    assert "Netflix" in message


@pytest.mark.asyncio
async def test_the_same_edit_is_allowed_unguarded_from_the_dashboard(
    client: AsyncClient, complete_candidate: dict[str, str], worker_session
) -> None:
    """The asymmetry is deliberate, so it is worth pinning down.

    The owner is the authority on their own employers and dates. If they type
    it on `/review`, it is theirs.
    """
    application_id = await _parked(client, complete_candidate, worker_session)

    r = await client.post(
        f"/applications/{application_id}/resume/edit",
        json={
            "contact": {"name": "Ada Lovelace"},
            "sections": {"experience": ["Principal Engineer at Netflix, cutting latency by 40%."]},
        },
    )

    assert r.status_code == 200, r.text


@pytest.mark.asyncio
async def test_an_unchanged_line_is_not_re_judged(
    client: AsyncClient, complete_candidate: dict[str, str], worker_session
) -> None:
    """The guard is strict enough that some genuine source text fails it.

    Re-checking carried-over lines would refuse edits that touched nothing.
    """
    application_id = await _parked(client, complete_candidate, worker_session)
    current = (await client.get(f"/applications/{application_id}/resume")).json()
    sections = {**current["sections"]}
    sections["experience"] = [*sections["experience"], *sections.get("summary", [])]

    r = await client.post(
        f"/applications/{application_id}/resume/edit",
        json={"contact": current["contact"], "sections": sections, "guard": True},
    )

    assert r.status_code == 200, r.text
