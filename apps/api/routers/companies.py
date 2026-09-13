"""Where the registry has got to — the screen the import path never had.

`/crawl/status` answers "is a crawl running". Nothing answered "did my 3,869
companies get anywhere", and an empty match feed has at least four quite
different causes: nothing imported, candidates still waiting on discovery,
boards waiting on a fetch, or postings fetched and scored under fallback
weighting because the corpus is small. Told apart here rather than guessed at.

Read-only. Starting discovery or a crawl stays a deliberate act at a terminal,
because both make real outbound requests to employers' sites.
"""

from __future__ import annotations

from fastapi import APIRouter
from sqlalchemy import func, select

from apps.api.deps import SessionDep
from packages.core.enums import QueueTaskStatus, SourceStatus
from packages.core.models import Company, CompanyCrawlState, Match, Posting, QueueTask
from packages.core.schemas import DiscoveryStatusOut
from packages.crawler.dispatch import CRAWL_COMPANY_TASK_KIND, DISCOVER_COMPANY_TASK_KIND
from packages.matching.idf import MIN_DOCUMENTS, open_document_count

router = APIRouter(prefix="/companies", tags=["companies"])


async def _count(session, *where) -> int:
    return int(await session.scalar(select(func.count()).select_from(Company).where(*where)) or 0)


async def _queue_depth(session, kind: str) -> int:
    return int(
        await session.scalar(
            select(func.count())
            .select_from(QueueTask)
            .where(
                QueueTask.kind == kind,
                QueueTask.status.in_(
                    (QueueTaskStatus.PENDING.value, QueueTaskStatus.RUNNING.value)
                ),
            )
        )
        or 0
    )


@router.get("/status", response_model=DiscoveryStatusOut)
async def discovery_status(session: SessionDep) -> DiscoveryStatusOut:
    """Counts by source status, the two queue depths, and matching readiness."""
    documents = await open_document_count(session)
    weighted = documents >= MIN_DOCUMENTS

    next_retry = await session.scalar(
        select(func.min(CompanyCrawlState.discovery_next_at))
        .select_from(CompanyCrawlState)
        .join(Company, Company.id == CompanyCrawlState.company_id)
        .where(Company.source_status == SourceStatus.FAILED.value)
    )

    return DiscoveryStatusOut(
        companies_total=await _count(session),
        pending=await _count(session, Company.source_status == SourceStatus.HINT.value),
        unverified=await _count(session, Company.source_status == SourceStatus.UNVERIFIED.value),
        verified=await _count(session, Company.source_status == SourceStatus.VERIFIED.value),
        retrying=await _count(session, Company.source_status == SourceStatus.FAILED.value),
        retrying_next_at=next_retry,
        needs_review=await _count(session, Company.source_status == SourceStatus.NO_WEBSITE.value),
        discovery_queue=await _queue_depth(session, DISCOVER_COMPANY_TASK_KIND),
        fetch_queue=await _queue_depth(session, CRAWL_COMPANY_TASK_KIND),
        postings_total=int(await session.scalar(select(func.count()).select_from(Posting)) or 0),
        postings_open=int(
            await session.scalar(
                select(func.count()).select_from(Posting).where(Posting.closed_at.is_(None))
            )
            or 0
        ),
        postings_unembedded=int(
            await session.scalar(
                select(func.count())
                .select_from(Posting)
                .where(
                    Posting.closed_at.is_(None),
                    Posting.description_embedding.is_(None),
                )
            )
            or 0
        ),
        matches_total=int(await session.scalar(select(func.count()).select_from(Match)) or 0),
        corpus_documents=documents,
        corpus_min_documents=MIN_DOCUMENTS,
        weighting="idf" if weighted else "unweighted",
        # Spelled out rather than left to be inferred from two numbers. A small
        # corpus does not stop a posting being scored; it makes the weights a
        # weaker description of the domain, and the owner reading a feed should
        # know which of those they are looking at.
        weighting_reason=(
            f"corpus has {documents} documents; IDF weighting active"
            if weighted
            else (
                f"corpus has {documents} of {MIN_DOCUMENTS} documents — scoring runs with the "
                "unweighted embedder, which describes the domain less well. Not a block: "
                "postings are still embedded, scored and listed."
            )
        ),
    )
