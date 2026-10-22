"""Local, text-free timings for one retrieval invocation.

Stage times are inclusive: nested stages overlap and must not be summed.
No exporter, database writes, inputs, outputs or exception messages.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable, Callable
from contextlib import suppress
from contextvars import ContextVar
from functools import wraps
from typing import Literal

import structlog
from pydantic import BaseModel

from packages.core.trace import current_task_id, current_trace_id

log = structlog.get_logger(__name__)

StageName = Literal[
    "keyword", "metadata_totals", "dense", "chunks", "hydrate", "rerank", "semantic"
]


class StageTiming(BaseModel):
    stage: StageName
    elapsed_ms: float
    success: bool
    # Only list/dict cardinalities, not tuple elements or arbitrary __len__ calls.
    items: int | None


_stages: ContextVar[list[StageTiming] | None] = ContextVar("retrieval_stages", default=None)


def stage[**P, R](
    name: StageName,
) -> Callable[[Callable[P, Awaitable[R]]], Callable[P, Awaitable[R]]]:
    def decorate(function: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
        @wraps(function)
        async def measured(*args: P.args, **kwargs: P.kwargs) -> R:
            stages = _stages.get()
            if stages is None:
                return await function(*args, **kwargs)
            start = time.perf_counter()
            success = False
            items = None
            try:
                result = await function(*args, **kwargs)
                success = True
                items = len(result) if isinstance(result, (list, dict)) else None
                return result
            finally:
                stages.append(
                    StageTiming(
                        stage=name,
                        elapsed_ms=(time.perf_counter() - start) * 1000,
                        success=success,
                        items=items,
                    )
                )

        return measured

    return decorate


def traced[**P, R](function: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
    @wraps(function)
    async def measured(*args: P.args, **kwargs: P.kwargs) -> R:
        stages: list[StageTiming] = []
        token = _stages.set(stages)
        start = time.perf_counter()
        success = False
        try:
            result = await function(*args, **kwargs)
            success = True
            return result
        finally:
            _stages.reset(token)
            # Optional instrumentation cannot mask a result or original error.
            with suppress(Exception):
                log.info(
                    "retrieval_completed",
                    retrieval_id=str(uuid.uuid4()),
                    trace_id=current_trace_id(),
                    task_id=current_task_id(),
                    success=success,
                    elapsed_ms=(time.perf_counter() - start) * 1000,
                    stages=[stage.model_dump() for stage in stages],
                )

    return measured
