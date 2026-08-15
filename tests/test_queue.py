"""Queue lease tests.

The scenario that motivates all of this: a worker claims a task, commits
`queued -> running`, then dies. The task is redelivered. A handler guarding
only with `can_transition()` finds `running -> running` illegal and can neither
proceed nor legally fail — a deadlocked task. The lease is what breaks it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from packages.core.enums import ApplicationStatus, QueueTaskStatus
from packages.core.models import QueueTask
from packages.core.queue import (
    claim_task,
    complete_task,
    enqueue,
    expire_leases,
    fail_task,
    heartbeat,
    release_task,
)
from packages.core.state import WorkClaim, begin_work

WORKER_A = "worker-a"
WORKER_B = "worker-b"


async def test_claim_returns_none_on_empty_queue(db_session) -> None:
    assert await claim_task(db_session, worker_id=WORKER_A) is None


async def test_claim_takes_a_lease(db_session) -> None:
    await enqueue(db_session, "apply", {"application_id": "x"})

    claimed = await claim_task(db_session, worker_id=WORKER_A, lease_seconds=300)

    assert claimed is not None
    assert claimed.task.status == QueueTaskStatus.RUNNING
    assert claimed.task.locked_by == WORKER_A
    assert claimed.task.attempts == 1
    assert claimed.task.lease_expires_at is not None
    assert claimed.task.lease_expires_at > datetime.now(UTC)
    assert not claimed.reclaimed


async def test_held_lease_blocks_other_workers(db_session) -> None:
    """An unexpired lease is what makes ownership exclusive."""
    await enqueue(db_session, "apply", {"application_id": "x"})

    first = await claim_task(db_session, worker_id=WORKER_A)
    assert first is not None

    second = await claim_task(db_session, worker_id=WORKER_B)
    assert second is None


async def test_expired_lease_is_reclaimable(db_session) -> None:
    await enqueue(db_session, "apply", {"application_id": "x"})
    first = await claim_task(db_session, worker_id=WORKER_A)
    assert first is not None

    await expire_leases(db_session)

    second = await claim_task(db_session, worker_id=WORKER_B)
    assert second is not None
    assert second.reclaimed
    assert second.previous_owner == WORKER_A
    assert not second.reclaimed_from_self
    assert second.task.locked_by == WORKER_B
    assert second.task.attempts == 2


async def test_worker_recognizes_its_own_dead_lease(db_session) -> None:
    """Same worker id after a restart — the lease it finds is its own."""
    await enqueue(db_session, "apply", {"application_id": "x"})
    await claim_task(db_session, worker_id=WORKER_A)
    await expire_leases(db_session)

    again = await claim_task(db_session, worker_id=WORKER_A)

    assert again is not None
    assert again.reclaimed
    assert again.previous_owner == WORKER_A
    assert again.reclaimed_from_self


async def test_heartbeat_extends_the_lease(db_session) -> None:
    await enqueue(db_session, "apply", {"application_id": "x"})
    claimed = await claim_task(db_session, worker_id=WORKER_A, lease_seconds=10)
    assert claimed is not None
    before = claimed.task.lease_expires_at
    assert before is not None

    await heartbeat(db_session, claimed.task, lease_seconds=600)

    assert claimed.task.lease_expires_at is not None
    assert claimed.task.lease_expires_at > before


async def test_heartbeat_keeps_a_task_unclaimable(db_session) -> None:
    await enqueue(db_session, "apply", {"application_id": "x"})
    claimed = await claim_task(db_session, worker_id=WORKER_A, lease_seconds=1)
    assert claimed is not None

    await expire_leases(db_session)
    await heartbeat(db_session, claimed.task, lease_seconds=600)

    assert await claim_task(db_session, worker_id=WORKER_B) is None


async def test_future_run_after_is_not_claimed(db_session) -> None:
    await enqueue(
        db_session,
        "apply",
        {"application_id": "x"},
        run_after=datetime.now(UTC) + timedelta(hours=1),
    )
    assert await claim_task(db_session, worker_id=WORKER_A) is None


async def test_kind_filter(db_session) -> None:
    await enqueue(db_session, "crawl", {})
    assert await claim_task(db_session, worker_id=WORKER_A, kinds=["apply"]) is None
    assert await claim_task(db_session, worker_id=WORKER_A, kinds=["crawl"]) is not None


async def test_complete_releases_the_lease(db_session) -> None:
    await enqueue(db_session, "apply", {"application_id": "x"})
    claimed = await claim_task(db_session, worker_id=WORKER_A)
    assert claimed is not None

    await complete_task(db_session, claimed.task)

    assert claimed.task.status == QueueTaskStatus.DONE
    assert claimed.task.locked_by is None
    assert claimed.task.lease_expires_at is None
    assert await claim_task(db_session, worker_id=WORKER_B) is None


async def test_failure_retries_then_gives_up(db_session) -> None:
    await enqueue(db_session, "apply", {"application_id": "x"})

    for expected_attempt in (1, 2):
        claimed = await claim_task(db_session, worker_id=WORKER_A)
        assert claimed is not None
        assert claimed.task.attempts == expected_attempt
        assert await fail_task(db_session, claimed.task, "boom", retry_in_s=0, max_attempts=3)

    claimed = await claim_task(db_session, worker_id=WORKER_A)
    assert claimed is not None
    assert claimed.task.attempts == 3
    assert not await fail_task(db_session, claimed.task, "boom", retry_in_s=0, max_attempts=3)
    assert claimed.task.status == QueueTaskStatus.FAILED


async def test_failure_message_is_truncated(db_session) -> None:
    """Errors land in a column; unbounded text from a page must not."""
    await enqueue(db_session, "apply", {"application_id": "x"})
    claimed = await claim_task(db_session, worker_id=WORKER_A)
    assert claimed is not None

    await fail_task(db_session, claimed.task, "x" * 10_000, retry_in_s=0)

    assert claimed.task.last_error is not None
    assert len(claimed.task.last_error) == 2000


async def test_release_does_not_consume_a_retry(db_session) -> None:
    await enqueue(db_session, "apply", {"application_id": "x"})
    claimed = await claim_task(db_session, worker_id=WORKER_A)
    assert claimed is not None

    await release_task(db_session, claimed.task)

    assert claimed.task.status == QueueTaskStatus.PENDING
    assert claimed.task.locked_by is None
    assert claimed.task.last_error is None


async def test_claims_are_ordered_by_run_after(db_session) -> None:
    now = datetime.now(UTC)
    await enqueue(db_session, "apply", {"n": "second"}, run_after=now - timedelta(seconds=10))
    await enqueue(db_session, "apply", {"n": "first"}, run_after=now - timedelta(seconds=60))

    claimed = await claim_task(db_session, worker_id=WORKER_A)

    assert claimed is not None
    assert claimed.task.payload_json["n"] == "first"


async def test_enqueue_defaults_to_pending_and_due(db_session) -> None:
    task = await enqueue(db_session, "apply", {"application_id": "x"})
    stored = await db_session.scalar(select(QueueTask).where(QueueTask.id == task.id))
    assert stored is not None
    assert stored.status == QueueTaskStatus.PENDING
    assert stored.attempts == 0


# --------------------------------------------------------------------------
# The deadlock this design exists to prevent
# --------------------------------------------------------------------------


async def test_retried_task_after_crash_does_not_deadlock(db_session, application) -> None:
    """Worker A commits queued->running then dies. The retry must proceed.

    `can_transition(running, running)` is False, so a handler guarding on that
    alone would be stuck. `begin_work()` reads the lease instead and resumes.
    """
    await enqueue(db_session, "apply", {"application_id": str(application.id)})

    first = await claim_task(db_session, worker_id=WORKER_A)
    assert first is not None
    assert await begin_work(db_session, application) is WorkClaim.STARTED
    assert application.status == ApplicationStatus.RUNNING

    # Worker A dies here. Its lease goes stale.
    await expire_leases(db_session)

    second = await claim_task(db_session, worker_id=WORKER_A)
    assert second is not None
    assert second.reclaimed_from_self

    # The row is already running; this must resume rather than raise.
    assert await begin_work(db_session, application) is WorkClaim.RESUMED
    assert application.status == ApplicationStatus.RUNNING


async def test_resume_writes_no_spurious_transition_event(db_session, application) -> None:
    """Resuming traverses no edge, so it must not fake one in the audit log."""
    from packages.core.models import ApplicationEvent

    await begin_work(db_session, application)
    await db_session.flush()

    before = len(
        (
            await db_session.scalars(
                select(ApplicationEvent).where(ApplicationEvent.application_id == application.id)
            )
        ).all()
    )

    await begin_work(db_session, application)
    await db_session.flush()

    after = len(
        (
            await db_session.scalars(
                select(ApplicationEvent).where(ApplicationEvent.application_id == application.id)
            )
        ).all()
    )
    assert after == before


@pytest.mark.parametrize("terminal", [ApplicationStatus.SUBMITTED, ApplicationStatus.FAILED])
async def test_redelivery_after_completion_is_a_no_op(db_session, application, terminal) -> None:
    """At-least-once delivery must not re-run finished work."""
    from packages.core.enums import FailureReason
    from packages.core.state import transition

    await begin_work(db_session, application)
    if terminal is ApplicationStatus.SUBMITTED:
        await transition(db_session, application, ApplicationStatus.SUBMITTED)
    else:
        await transition(
            db_session,
            application,
            ApplicationStatus.FAILED,
            failure_reason=FailureReason.SITE_ERROR,
        )

    assert await begin_work(db_session, application) is WorkClaim.ALREADY_DONE
    assert application.status == terminal
