"""Run and commit interview research one batch at a time."""

from __future__ import annotations

from copy import deepcopy
from functools import partial
from time import perf_counter
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from apps.worker.apply_job import TaskPayloadError
from packages.core.models_interview import InterviewPlan, InterviewRound
from packages.core.queue import ClaimedTask, ClaimOwnershipLost, heartbeat
from packages.interview import retrieval
from packages.interview.research import step
from packages.interview.schema import Evidence, Snapshot
from packages.interview.service import TASK_KIND as INTERVIEW_TASK_KIND
from packages.interview.service import refresh_project
from packages.llm.audit import AuditUnavailable
from packages.llm.provider import LLMError
from packages.llm.quota import QuotaExceeded
from packages.matching.query_vectors import QueryEmbeddingError

log = structlog.get_logger(__name__)

__all__ = ["INTERVIEW_TASK_KIND", "handle_interview"]


async def handle_interview(session: AsyncSession, claimed: ClaimedTask) -> None:
    try:
        plan_id = UUID(claimed.task.payload_json["plan_id"])
    except (TypeError, ValueError, KeyError) as exc:
        raise TaskPayloadError("Expected an interview plan ID") from exc
    while True:
        row = await session.get(
            InterviewPlan, plan_id, with_for_update=True, populate_existing=True
        )
        if (
            row is None
            or row.queue_id != claimed.task_id
            or row.status not in {"queued", "running", "interrupted"}
        ):
            return
        snapshot = Snapshot.model_validate(row.inputs)
        progress = deepcopy(row.progress)
        trace: dict[str, Any] = {"attempt": claimed.attempt}
        start = perf_counter()
        failure = None
        linked = await session.get(InterviewRound, row.round_id)
        assert linked is not None

        async def retrieve(
            wording: str, app: UUID = linked.application_id, plan: InterviewPlan = row
        ) -> list[str]:
            return await retrieval.candidate_hits(session, app, plan, wording)

        try:
            action, status = await step(
                snapshot,
                progress,
                trace,
                retrieve=retrieve,
                read_project=partial(refresh_project, session, linked.application_id),
            )
        except (LLMError, AuditUnavailable, QuotaExceeded) as exc:
            failure = exc
            progress = deepcopy(row.progress)
            action, status = trace.get("action", "connect_kaggle"), "interrupted"
            trace["outcome"] = type(exc).__name__
        vector_ids = []
        if "read" in action or not any(
            "embedding_status" in previous for previous in progress["steps"]
        ):
            try:
                vector_ids = await retrieval.embed_checkpoint(
                    session, row, [Evidence.model_validate(item) for item in progress["evidence"]]
                )
            except QueryEmbeddingError:
                trace["embedding_status"] = "local_embedder_unavailable"
            else:
                trace["embedding_status"] = "indexed" if vector_ids else "no_new_vectors"
                trace["new_vectors"] = len(vector_ids)
        trace.update(action=action, elapsed_ms=round((perf_counter() - start) * 1000, 1))
        progress["steps"] = [*progress["steps"][-47:], trace]
        if not await heartbeat(session, claimed):
            raise ClaimOwnershipLost(str(claimed.task_id))
        row.progress = progress
        row.status = status
        await session.commit()
        try:
            await retrieval.publish(session, vector_ids)
        except Exception as exc:
            if not isinstance(exc, (OSError, RuntimeError)) and not retrieval.redis_error(exc):
                raise
            # The vectors are already durable. Sync can publish them independently.
            log.warning("interview_redis_publish_failed", error=type(exc).__name__)
        finally:
            if vector_ids:
                await session.commit()
        if failure is not None:
            raise failure
        if status == "ready":
            return
