"""Local, application-scoped evidence retrieval using reusable Qwen vectors."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any
from uuid import UUID, uuid5

from sqlalchemy import Select, delete, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.config import get_settings
from packages.core.models import Application, Posting
from packages.core.models_chunks import LARGE_EMBEDDING_DIM
from packages.core.models_interview import InterviewPlan, InterviewRound, InterviewVector
from packages.interview.plan import CANDIDATE_KINDS
from packages.interview.schema import Evidence
from packages.llm.audit import is_loopback
from packages.matching import query_vectors
from packages.matching.chunk_vectors import QWEN_4B
from packages.matching.qwen_redis import prune, query_vector, write_documents
from packages.matching.vector_search import ensure_redis_index, model_tag, redis_client, redis_knn

INDEX = "jobrunner:interview:qwen3-embedding-4b:2560:v1"
TAGS = ("model", "application", "company", "round", "plan", "group")


def eligible(item: Evidence) -> bool:
    return not item.superseded


async def embed_checkpoint(
    session: AsyncSession,
    plan: InterviewPlan,
    sources: Sequence[Evidence],
) -> list[UUID]:
    if not get_settings().interview_qwen_embeddings or plan.inputs["fixture"]:
        return []
    superseded = [item.id for item in sources if item.superseded]
    if superseded:
        await session.execute(
            delete(InterviewVector).where(
                InterviewVector.plan_id == plan.id, InterviewVector.source_id.in_(superseded)
            )
        )
    existing = set(
        await session.scalars(
            select(InterviewVector.source_id).where(
                InterviewVector.plan_id == plan.id, InterviewVector.model == QWEN_4B
            )
        )
    )
    pending = [item for item in sources if eligible(item) and item.id not in existing]
    inserted: list[UUID] = []
    for offset in range(0, len(pending), 12):
        group = pending[offset : offset + 12]
        values = await query_vectors.embed_passages([item.excerpt[:850] for item in group])
        records = [
            {
                "id": uuid5(plan.id, f"{QWEN_4B}:{item.id}"),
                "plan_id": plan.id,
                "source_id": item.id,
                "kind": item.kind,
                "model": QWEN_4B,
                "embedding": list(vector),
            }
            for item, vector in zip(group, values, strict=True)
        ]
        statement = (
            insert(InterviewVector)
            .values(records)
            .on_conflict_do_nothing(constraint="uq_interview_vector_source")
            .returning(InterviewVector.id)
        )
        inserted.extend(await session.scalars(statement))
    return inserted


def _records() -> Select[tuple[UUID]]:
    return (
        select(InterviewVector.id)
        .select_from(InterviewVector)
        .join(InterviewPlan, InterviewPlan.id == InterviewVector.plan_id)
        .join(InterviewRound, InterviewRound.id == InterviewPlan.round_id)
        .join(Application, Application.id == InterviewRound.application_id)
        .outerjoin(Posting, Posting.id == Application.posting_id)
        .where(InterviewVector.model == QWEN_4B)
    )


def local_redis() -> Any:
    if not is_loopback(get_settings().redis_url):
        raise RuntimeError("Interview cache requires loopback Redis.")
    return redis_client()


async def _write(session: AsyncSession, client: Any, ids: Sequence[UUID], index: str) -> int:
    query = (
        _records()
        .add_columns(
            InterviewVector.embedding,
            InterviewRound.application_id,
            Posting.company_id,
            InterviewRound.id,
            InterviewVector.plan_id,
            InterviewVector.kind,
        )
        .where(InterviewVector.id.in_(ids))
    )
    docs = [
        (
            id_,
            {
                "id": str(id_),
                "model": model_tag(QWEN_4B),
                "application": app.hex,
                "round": round_.hex,
                "company": company.hex if company else "none",
                "plan": plan.hex,
                "group": "candidate" if kind in CANDIDATE_KINDS else "public",
            },
            values,
        )
        for id_, values, app, company, round_, plan, kind in (await session.execute(query))
    ]
    return await write_documents(client, index, docs)


async def publish(session: AsyncSession, ids: Sequence[UUID]) -> None:
    if not ids or get_settings().qwen_vector_backend != "redis_hnsw":
        return
    async with local_redis() as client:
        await ensure_redis_index(client, index=INDEX, dim=LARGE_EMBEDDING_DIM, tags=TAGS)
        for offset in range(0, len(ids), 100):
            await _write(session, client, ids[offset : offset + 100], INDEX)


async def sync(
    session: AsyncSession,
    client: Any,
    *,
    batch_size: int = 500,
    max_batches: int | None = None,
    rebuild: bool = False,
    index: str = INDEX,
) -> dict[str, Any]:
    if batch_size < 1 or (max_batches is not None and max_batches < 1):
        raise ValueError("Batch sizes and limits must be positive.")
    if not await session.scalar(text("SELECT pg_try_advisory_xact_lock(731942110)")):
        raise RuntimeError("Another interview sync is running.")
    cursor = f"{index}:cursor"
    if rebuild:
        await client.delete(cursor)
        try:
            await client.execute_command("FT.DROPINDEX", index, "DD")
        except Exception as exc:
            if "unknown index" not in str(exc).lower():
                raise
    await ensure_redis_index(client, index=index, dim=LARGE_EMBEDDING_DIM, tags=TAGS)
    stored = await client.get(cursor)
    last = UUID(stored.decode()) if stored else None
    stats = {"scanned": 0, "written": 0, "deleted": 0, "complete": False}
    batches = 0
    while max_batches is None or batches < max_batches:
        query = _records().order_by(InterviewVector.id).limit(batch_size)
        if last:
            query = query.where(InterviewVector.id > last)
        ids = list(await session.scalars(query))
        if not ids:
            stats["deleted"] = await prune(session, client, index, _records(), batch_size)
            await client.delete(cursor)
            stats["complete"] = True
            break
        stats["scanned"] += len(ids)
        stats["written"] += await _write(session, client, ids, index)
        last = ids[-1]
        await client.set(cursor, str(last))
        batches += 1
    return stats


async def search(
    session: AsyncSession,
    values: list[float],
    *,
    application_id: UUID,
    round_id: UUID | None = None,
    plan_id: UUID | None = None,
    company_id: UUID | None = None,
    k: int = 10,
    candidate_only: bool = False,
    redis: bool = False,
    client: Any = None,
    index: str = INDEX,
) -> list[tuple[UUID, str]]:
    vector, blob = query_vector(values)
    distance = InterviewVector.embedding.cosine_distance(vector)
    query = (
        _records()
        .add_columns(InterviewVector.source_id)
        .where(
            InterviewRound.application_id == application_id,
            distance < 2.01,
        )
    )
    tags = [f"@model:{{{model_tag(QWEN_4B)}}}", f"@application:{{{application_id.hex}}}"]
    for scope, value, column in (
        ("round", round_id, InterviewRound.id),
        ("plan", plan_id, InterviewVector.plan_id),
        ("company", company_id, Posting.company_id),
    ):
        if value is not None:
            tags.append(f"@{scope}:{{{value.hex}}}")
            query = query.where(column == value)
    if candidate_only:
        tags.append("@group:{candidate}")
        query = query.where(InterviewVector.kind.in_(CANDIDATE_KINDS))
    if redis:
        owned = client is None
        client = local_redis() if owned else client
        try:
            ids = await redis_knn(client, index, " ".join(tags), blob, 2 * k, field="id")
        finally:
            if owned:
                await client.aclose()
        query = query.where(InterviewVector.id.in_(ids))
    return [
        (row.id, row.source_id)
        for row in (
            await session.execute(query.order_by(distance + 0.0, InterviewVector.id).limit(k))
        )
    ]


async def candidate_hits(
    session: AsyncSession,
    app: UUID,
    plan: InterviewPlan,
    wording: str,
) -> list[str]:
    settings = get_settings()
    if not settings.interview_qwen_embeddings:
        return []
    try:
        async with asyncio.timeout(30):
            vector = list((await query_vectors.embed_question(wording)).values)
            args: dict[str, Any] = dict(
                application_id=app,
                plan_id=plan.id,
                round_id=plan.round_id,
                candidate_only=True,
                k=5,
            )
            if settings.qwen_vector_backend == "redis_hnsw":
                try:
                    hits = await search(session, vector, redis=True, **args)
                except Exception as exc:
                    if not isinstance(exc, (OSError, RuntimeError)) and not redis_error(exc):
                        raise
                else:
                    if hits:
                        return [source for _, source in hits]
            return [source for _, source in await search(session, vector, **args)]
    except (TimeoutError, query_vectors.QueryEmbeddingError):
        return []


def redis_error(exc: Exception) -> bool:
    return type(exc).__module__.split(".")[0] == "redis"
