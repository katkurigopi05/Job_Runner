"""Contacts and tasks on applications, and the calendar export.

Everything here is the owner's own record and stays on this machine. No route
sends anything to anyone: a follow-up task reminds the owner to write one, and
the `.ics` routes return a file for the owner's own calendar.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Query, Response
from sqlalchemy import select

from apps.api.deps import SessionDep
from apps.api.errors import ApiError
from packages.core.enums import ErrorCode
from packages.core.models import Application
from packages.core.models_tracking import ApplicationContact, ApplicationTask, Contact
from packages.core.schemas_tracking import (
    ContactIn,
    ContactOut,
    LinkContactIn,
    LinkedContactOut,
    TaskIn,
    TaskOut,
    TaskPatch,
    TrackingOut,
    UpcomingTaskOut,
)
from packages.tracking import ics
from packages.tracking.tasks import checklist

router = APIRouter(tags=["tracking"])

CALENDAR = "text/calendar; charset=utf-8"


async def _application(session: SessionDep, application_id: uuid.UUID) -> Application:
    application = await session.get(Application, application_id)
    if application is None:
        raise ApiError(ErrorCode.NOT_FOUND, "application not found")
    return application


async def _task(session: SessionDep, task_id: uuid.UUID) -> ApplicationTask:
    task = await session.get(ApplicationTask, task_id)
    if task is None:
        raise ApiError(ErrorCode.NOT_FOUND, "task not found")
    return task


async def _tracking(session: SessionDep, application_id: uuid.UUID) -> TrackingOut:
    linked = (
        await session.execute(
            select(ApplicationContact.relationship, Contact)
            .join(Contact, Contact.id == ApplicationContact.contact_id)
            .where(ApplicationContact.application_id == application_id)
            .order_by(ApplicationContact.created_at)
        )
    ).all()
    tasks = (
        await session.scalars(
            select(ApplicationTask)
            .where(ApplicationTask.application_id == application_id)
            .order_by(
                ApplicationTask.completed_at.is_not(None), ApplicationTask.due_at.asc().nullslast()
            )
        )
    ).all()
    return TrackingOut(
        contacts=[
            LinkedContactOut(relationship=relationship, contact=ContactOut.model_validate(contact))
            for relationship, contact in linked
        ],
        tasks=[TaskOut.model_validate(task) for task in tasks],
    )


@router.get("/applications/{application_id}/tracking", response_model=TrackingOut)
async def get_tracking(application_id: uuid.UUID, session: SessionDep) -> TrackingOut:
    await _application(session, application_id)
    return await _tracking(session, application_id)


@router.post("/applications/{application_id}/contacts", response_model=LinkedContactOut)
async def link_contact(
    application_id: uuid.UUID, body: LinkContactIn, session: SessionDep
) -> LinkedContactOut:
    application = await _application(session, application_id)
    if body.contact_id is not None:
        contact = await session.get(Contact, body.contact_id)
        if contact is None or contact.candidate_id != application.candidate_id:
            raise ApiError(ErrorCode.NOT_FOUND, "contact not found")
    else:
        assert body.contact is not None
        contact = Contact(candidate_id=application.candidate_id, **body.contact.model_dump())
        session.add(contact)
        await session.flush()
    existing = await session.scalar(
        select(ApplicationContact).where(
            ApplicationContact.application_id == application.id,
            ApplicationContact.contact_id == contact.id,
        )
    )
    if existing is None:
        session.add(
            ApplicationContact(
                application_id=application.id, contact_id=contact.id, relationship=body.relationship
            )
        )
    else:
        existing.relationship = body.relationship
    await session.commit()
    await session.refresh(contact)
    return LinkedContactOut(
        relationship=body.relationship, contact=ContactOut.model_validate(contact)
    )


@router.delete("/applications/{application_id}/contacts/{contact_id}", status_code=204)
async def unlink_contact(
    application_id: uuid.UUID, contact_id: uuid.UUID, session: SessionDep
) -> Response:
    """Removes the link only. The contact and its notes stay."""
    link = await session.scalar(
        select(ApplicationContact).where(
            ApplicationContact.application_id == application_id,
            ApplicationContact.contact_id == contact_id,
        )
    )
    if link is not None:
        await session.delete(link)
        await session.commit()
    return Response(status_code=204)


@router.get("/contacts", response_model=list[ContactOut])
async def list_contacts(session: SessionDep) -> list[Contact]:
    return list((await session.scalars(select(Contact).order_by(Contact.name))).all())


@router.put("/contacts/{contact_id}", response_model=ContactOut)
async def update_contact(contact_id: uuid.UUID, body: ContactIn, session: SessionDep) -> Contact:
    contact = await session.get(Contact, contact_id)
    if contact is None:
        raise ApiError(ErrorCode.NOT_FOUND, "contact not found")
    for name, value in body.model_dump().items():
        setattr(contact, name, value)
    contact.updated_at = datetime.now(UTC)
    await session.commit()
    await session.refresh(contact)
    return contact


@router.post("/applications/{application_id}/tasks", response_model=TaskOut, status_code=201)
async def create_task(application_id: uuid.UUID, body: TaskIn, session: SessionDep) -> TaskOut:
    await _application(session, application_id)
    task = ApplicationTask(
        application_id=application_id,
        kind=body.kind,
        title=body.title,
        due_at=body.due_at,
        reminder_at=body.reminder_at,
        location=body.location,
        notes=body.notes,
        checklist_json=checklist(body.kind, body.checklist),
        source="owner",
    )
    session.add(task)
    await session.commit()
    await session.refresh(task)
    return TaskOut.model_validate(task)


@router.patch("/tasks/{task_id}", response_model=TaskOut)
async def update_task(task_id: uuid.UUID, body: TaskPatch, session: SessionDep) -> TaskOut:
    task = await _task(session, task_id)
    sent = body.model_fields_set
    for name in ("title", "due_at", "location", "notes"):
        if name in sent:
            setattr(task, name, getattr(body, name))
    if "reminder_at" in sent and body.reminder_at != task.reminder_at:
        task.reminder_at = body.reminder_at
        # A moved reminder is a new reminder, so it rings again.
        task.reminded_at = None
    if "checklist" in sent:
        task.checklist_json = [item.model_dump() for item in body.checklist or []]
    if body.completed is not None:
        task.completed_at = datetime.now(UTC) if body.completed else None
    if task.due_at and task.reminder_at and task.reminder_at > task.due_at:
        raise ApiError(ErrorCode.INVALID_REQUEST, "reminder_at must not be after due_at")
    task.updated_at = datetime.now(UTC)
    await session.commit()
    await session.refresh(task)
    return TaskOut.model_validate(task)


@router.delete("/tasks/{task_id}", status_code=204)
async def delete_task(task_id: uuid.UUID, session: SessionDep) -> Response:
    task = await session.get(ApplicationTask, task_id)
    if task is not None:
        await session.delete(task)
        await session.commit()
    return Response(status_code=204)


@router.get("/tasks", response_model=list[UpcomingTaskOut])
async def upcoming_tasks(
    session: SessionDep,
    open_only: bool = True,
    due_within_days: int | None = Query(default=None, ge=0, le=365),
) -> list[UpcomingTaskOut]:
    """Tasks across applications, soonest first; overdue ones flagged."""
    now = datetime.now(UTC)
    query = select(ApplicationTask, Application.url).join(
        Application, Application.id == ApplicationTask.application_id
    )
    if open_only:
        query = query.where(ApplicationTask.completed_at.is_(None))
    if due_within_days is not None:
        query = query.where(
            ApplicationTask.due_at.is_not(None),
            ApplicationTask.due_at <= now + timedelta(days=due_within_days),
        )
    rows = (await session.execute(query.order_by(ApplicationTask.due_at.asc().nullslast()))).all()
    return [
        UpcomingTaskOut(
            **TaskOut.model_validate(task).model_dump(),
            application_url=url,
            overdue=bool(task.due_at and task.due_at < now and task.completed_at is None),
        )
        for task, url in rows
    ]


@router.get("/applications/{application_id}/tasks.ics")
async def application_calendar(application_id: uuid.UUID, session: SessionDep) -> Response:
    application = await _application(session, application_id)
    tasks = (
        await session.scalars(
            select(ApplicationTask).where(ApplicationTask.application_id == application_id)
        )
    ).all()
    return Response(
        ics.render([(task, application.url) for task in tasks]),
        media_type=CALENDAR,
        headers={"Content-Disposition": f'attachment; filename="application-{application_id}.ics"'},
    )


@router.get("/tasks.ics")
async def all_open_tasks_calendar(session: SessionDep) -> Response:
    rows = (
        await session.execute(
            select(ApplicationTask, Application.url)
            .join(Application, Application.id == ApplicationTask.application_id)
            .where(ApplicationTask.completed_at.is_(None), ApplicationTask.due_at.is_not(None))
        )
    ).all()
    return Response(
        ics.render([(task, url) for task, url in rows]),
        media_type=CALENDAR,
        headers={"Content-Disposition": 'attachment; filename="jobrunner-tasks.ics"'},
    )
