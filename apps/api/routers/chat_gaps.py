"""More of a recurring gap's jobs, read again: `POST /chat/gap-jobs`.

An answer carries the id of every job behind a gap's count and the first few in
full (`GapEvidenceOut`). The dock asks for the rest here, by those ids, so the
list stays the population the count was taken over: nothing is searched again,
and no job joins it. Each is read as it is now and says whether it still
directly requires the skill, by the rule the graph counted it with
(`graph_rag.build_graph`). Reads only; no model, no provider.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter
from sqlalchemy import select

from apps.api.deps import SessionDep
from apps.api.errors import ApiError
from packages.core.enums import ErrorCode
from packages.core.models import Company, Posting
from packages.core.schemas_graph import GapJobOut, GapJobsOut, GapJobsRequest
from packages.matching.graph_rag import Entity, build_graph, one_per_job, saved_links
from packages.matching.skill_vocab import BY_KEY

router = APIRouter(prefix="/chat", tags=["chat"])

#: `build_graph` needs a candidate for résumé and project edges; none are read here.
_NO_CANDIDATE = uuid.UUID(int=0)


@router.post("/gap-jobs", response_model=GapJobsOut)
async def gap_jobs(body: GapJobsRequest, session: SessionDep) -> GapJobsOut:
    """The jobs named, in that order, each with its current requirement line for the skill."""
    if body.skill not in BY_KEY:
        raise ApiError(ErrorCode.INVALID_REQUEST, f"Unknown skill {body.skill!r}.")
    asked = list(dict.fromkeys(body.posting_ids))
    rows = await session.execute(
        select(Posting, Company.name)
        .outerjoin(Company, Company.id == Posting.company_id)
        .where(Posting.id.in_(asked))
    )
    found = {posting.id: (posting, company) for posting, company in rows}
    links = await saved_links(session, list(found))
    graph = build_graph(_NO_CANDIDATE, [], [], list(found.values()), links=links)
    quotes = {
        edge.source.key: edge.quote
        for edge in one_per_job(graph.links(Entity("skill", body.skill), "requires", incoming=True))
    }
    jobs = []
    for posting_id in asked:
        if posting_id not in found:
            continue
        posting, company = found[posting_id]
        quote = quotes.get(str(posting_id))
        jobs.append(
            GapJobOut(
                posting_id=posting.id,
                title=posting.title or "(untitled)",
                company=company,
                location=posting.location,
                url=posting.url,
                quote=quote,
                state=(
                    "closed" if posting.closed_at is not None else "current" if quote else "changed"
                ),
            )
        )
    return GapJobsOut(skill=body.skill, jobs=jobs)
