"""Attempts to send that nobody has vouched for either way.

The answers are on `/applications/{id}/submitted` and
`/applications/{id}/not-submitted`; this lists what still needs one. Not an
MCP tool: the answer comes from somebody who checked with the employer.
"""

from __future__ import annotations

from fastapi import APIRouter
from sqlalchemy import select

from apps.api.deps import SessionDep
from packages.core import submissions
from packages.core.enums import ApplicationStatus
from packages.core.models import Application, AppliedPosting, Company, Posting
from packages.core.schemas import UnsettledSubmissionOut

router = APIRouter(prefix="/submissions", tags=["submissions"])


@router.get("/unsettled", response_model=list[UnsettledSubmissionOut])
async def unsettled(session: SessionDep) -> list[UnsettledSubmissionOut]:
    """Oldest attempt first. A running application's is a click in flight,
    which the worker is about to settle itself, so it is left out."""
    rows = (
        await session.execute(
            select(AppliedPosting, Application, Posting.title, Company.name)
            .join(Application, Application.id == AppliedPosting.application_id)
            .outerjoin(Posting, Posting.id == AppliedPosting.posting_id)
            .outerjoin(Company, Company.id == Posting.company_id)
            .where(
                AppliedPosting.state.in_(submissions.UNSETTLED),
                Application.status != ApplicationStatus.RUNNING.value,
            )
            .order_by(AppliedPosting.attempted_at)
        )
    ).all()
    return [
        UnsettledSubmissionOut(
            application_id=application.id,
            application_status=application.status,
            failure_reason=application.failure_reason,
            url=fence.url,
            company=company,
            title=title,
            state=fence.state,
            source=fence.source,
            attempted_at=fence.attempted_at,
        )
        for fence, application, title, company in rows
    ]
