"""What the setup page shows, and the recovery step for each problem. Read-only.

Doctor results, worker liveness, crawler freshness and registry drift all
existed somewhere — a terminal command, a status route, a database query —
and none of them was on a screen that also said what to do. The audit that
found 119 companies "needing a URL" found the cause in a terminal; the
dashboard showing the symptom gave no way to reach it.

Two rules hold across every item:

- **No secret values.** Items are built from doctor checks that already refuse
  to repeat a credential, from counts, and from timestamps. Nothing here reads
  a key, a password, or a database URL.
- **A step is something to run or edit**, in the order to try it. A verdict
  without one is a symptom report, which is what this page exists to replace.

Lives in `apps/api` rather than `packages/core` because it reads the worker's
task-kind constants, and core must not import apps.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.worker.crawl_job import CRAWL_TASK_KIND
from packages.core import doctor
from packages.core.doctor import Check, Health
from packages.core.enums import QueueTaskStatus
from packages.core.heartbeat import STALE_AFTER_S, WorkerState, WorkerView, workers
from packages.core.models import (
    Company,
    CompanyCrawlState,
    CrawlRun,
    InboundMessage,
    Posting,
    QueueTask,
)
from packages.core.schemas_setup import SetupItemOut, SetupState, SetupStatusOut

#: A crawl older than this, with none scheduled, is reported as idle.
FRESH_HOURS = 24

#: Consecutive failures before a board is called failing rather than unlucky.
FAILING_BOARD_THRESHOLD = 3

_RANK: dict[SetupState, int] = {"ok": 0, "unknown": 1, "attention": 2, "blocked": 3}

#: Doctor check name → (title, group). Unknown names still appear, under tools.
_CHECKS: dict[str, tuple[str, str]] = {
    "postgres": ("Database", "core"),
    "migrations": ("Schema migrations", "core"),
    "vault": ("Credential vault", "credentials"),
    "inbox": ("Recruiter inbox", "credentials"),
    "weasyprint": ("PDF rendering", "tools"),
    "noun-phrase tagger": ("Fabrication-guard tagger", "tools"),
    "playwright": ("Browser for application forms", "tools"),
    "storage": ("Local storage", "tools"),
    "llm": ("LLM provider", "tools"),
    "ollama": ("Local model (Ollama)", "tools"),
}

#: Shown by their own richer items instead of the doctor's one-liner.
_REPLACED = {"registry"}


def _steps(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


def _from_check(check: Check) -> SetupItemOut:
    title, group = _CHECKS.get(check.name, (check.name.capitalize(), "tools"))
    state: SetupState
    if check.health is Health.OK:
        state = "ok"
    elif check.health is Health.SKIPPED:
        state = "unknown"
    else:
        state = "blocked" if check.required else "attention"
    return SetupItemOut(
        key=check.name.replace(" ", "_"),
        title=title,
        group=group,
        state=state,
        detail=check.detail,
        steps=[] if check.ok else _steps(check.fix),
        facts={"required": check.required},
    )


def _age(moment: datetime | None, now: datetime) -> float | None:
    if moment is None:
        return None
    stamp = moment if moment.tzinfo else moment.replace(tzinfo=UTC)
    return max(0.0, (now - stamp).total_seconds())


def _ago(seconds: float | None) -> str:
    if seconds is None:
        return "never"
    if seconds < 90:
        return f"{int(seconds)}s ago"
    if seconds < 90 * 60:
        return f"{int(seconds // 60)} min ago"
    if seconds < 48 * 3600:
        return f"{seconds / 3600:.1f} h ago"
    return f"{int(seconds // 86400)} days ago"


async def _count(session: AsyncSession, statement: object) -> int:
    return int(await session.scalar(statement) or 0)  # type: ignore[call-overload]


async def worker_item(session: AsyncSession, now: datetime) -> SetupItemOut:
    views = await workers(session, now=now)
    waiting = await _count(
        session,
        select(func.count())
        .select_from(QueueTask)
        .where(QueueTask.status == QueueTaskStatus.PENDING.value, QueueTask.run_after <= now),
    )
    alive = [view for view in views if view.state is WorkerState.ALIVE]
    facts: dict[str, str | int | float | bool | None] = {
        "workers_seen": len(views),
        "workers_alive": len(alive),
        "tasks_waiting": waiting,
    }
    start = ["make worker   # in its own terminal; `make workers n=4` runs a pool"]

    if alive:
        return SetupItemOut(
            key="worker",
            title="Queue worker",
            group="core",
            state="ok",
            detail="; ".join(_describe(view) for view in alive),
            facts=facts,
        )

    if not views:
        return SetupItemOut(
            key="worker",
            title="Queue worker",
            group="core",
            state="blocked" if waiting else "attention",
            detail=(
                "No worker has reported a heartbeat. "
                + (f"{waiting} task(s) are waiting for one. " if waiting else "")
                + "A worker started before heartbeats existed does not report until restarted."
            ),
            steps=start,
            facts=facts,
        )

    latest = views[0]
    crashed = latest.state is WorkerState.STALE
    detail = f"Last heartbeat from {latest.worker_id} was {_ago(latest.seconds_since_seen)}" + (
        f" and it did not stop cleanly (silent for over {int(STALE_AFTER_S)}s)."
        if crashed
        else ", after a clean stop."
    )
    if waiting:
        detail += f" {waiting} task(s) are waiting."
    steps = (
        ["Look at the worker's terminal for a traceback before restarting it", *start]
        if crashed
        else start
    )
    return SetupItemOut(
        key="worker",
        title="Queue worker",
        group="core",
        state="blocked" if waiting else "attention",
        detail=detail,
        steps=steps,
        facts={**facts, "last_error_kind": latest.last_error_kind},
    )


def _describe(view: WorkerView) -> str:
    doing = f"running {view.current_task_kind}" if view.current_task_kind else "idle"
    return (
        f"{view.worker_id} alive, {doing}, seen {_ago(view.seconds_since_seen)}, "
        f"{view.tasks_completed} task(s) handled"
    )


async def registry_item(session: AsyncSession) -> SetupItemOut:
    from packages.crawler.extract import SeedFileError, load_retired, load_seed
    from packages.crawler.registry_health import SYNC_FIX, diagnose_registry

    try:
        seeds = load_seed()
        retired = load_retired()
    except SeedFileError as exc:
        return SetupItemOut(
            key="registry",
            title="Company registry",
            group="discovery",
            state="blocked",
            detail=f"seeds/companies.yaml cannot be read: {exc}",
            steps=["Fix the file; `git diff seeds/companies.yaml` shows what changed"],
        )

    health = await diagnose_registry(session, seeds, retired)
    facts: dict[str, str | int | float | bool | None] = {
        "live_entries": health.live_seeds,
        "retired_entries": health.retired_seeds,
        "company_rows": health.rows,
        "fetchable": health.fetchable,
    }
    if health.ok:
        return SetupItemOut(
            key="registry",
            title="Company registry",
            group="discovery",
            state="ok",
            detail=health.summary(),
            facts=facts,
        )

    lines = []
    for problem in health.problems:
        facts[problem.code] = problem.count
        example = f" (e.g. {', '.join(problem.examples)})" if problem.examples else ""
        lines.append(f"{problem.count} {problem.detail}{example}.")
    steps = _steps(SYNC_FIX)
    for problem in health.problems:
        for step in _steps(problem.fix):
            if step not in steps:
                steps.append(step)
    return SetupItemOut(
        key="registry",
        title="Company registry",
        group="discovery",
        # With nothing fetchable the crawler cannot find a single posting.
        state="blocked" if health.fetchable == 0 else "attention",
        detail=" ".join(lines),
        steps=steps,
        facts=facts,
        actions=["registry_sync"],
    )


async def crawler_item(session: AsyncSession, now: datetime) -> SetupItemOut:
    from packages.crawler.runs import fetchable

    fetchable_count = await _count(
        session, select(func.count()).select_from(Company).where(fetchable())
    )
    last_run = (
        await session.scalars(select(CrawlRun).order_by(CrawlRun.started_at.desc()).limit(1))
    ).first()
    scheduled = await _count(
        session,
        select(func.count())
        .select_from(QueueTask)
        .where(
            QueueTask.kind == CRAWL_TASK_KIND,
            QueueTask.status.in_((QueueTaskStatus.PENDING.value, QueueTaskStatus.RUNNING.value)),
        ),
    )
    failing = await _count(
        session,
        select(func.count())
        .select_from(CompanyCrawlState)
        .join(Company, Company.id == CompanyCrawlState.company_id)
        .where(fetchable(), CompanyCrawlState.consecutive_failures >= FAILING_BOARD_THRESHOLD),
    )
    newest = await session.scalar(select(func.max(Posting.first_seen_at)))
    finished_age = _age(last_run.finished_at if last_run else None, now)
    facts: dict[str, str | int | float | bool | None] = {
        "fetchable_boards": fetchable_count,
        "last_run_status": last_run.status if last_run else None,
        "last_run_finished": _ago(finished_age) if last_run else None,
        "crawl_scheduled": bool(scheduled),
        "newest_posting_seen": _ago(_age(newest, now)),
        "failing_boards": failing,
    }
    crawl = "make crawl    # enqueues a cycle; it makes real requests to employer boards"

    def item(state: SetupState, detail: str, steps: list[str]) -> SetupItemOut:
        return SetupItemOut(
            key="crawler",
            title="Crawler freshness",
            group="discovery",
            state=state,
            detail=detail,
            steps=steps,
            facts=facts,
        )

    if fetchable_count == 0:
        return item(
            "blocked",
            "No company board is fetchable, so a crawl cannot find any postings.",
            ["Fix the Company registry item first (make registry-sync)"],
        )
    if last_run is None:
        return item(
            "attention",
            f"No crawl has run on this database. {fetchable_count} boards are ready.",
            [crawl, "make worker"],
        )
    if last_run.finished_at is None:
        return item(
            "ok", f"A crawl is in progress (started {_ago(_age(last_run.started_at, now))}).", []
        )
    stale = finished_age is not None and finished_age > FRESH_HOURS * 3600 and not scheduled
    if stale:
        return item(
            "attention",
            f"The last crawl finished {_ago(finished_age)} ({last_run.status}) and none is "
            "scheduled, so new postings are not being found.",
            [crawl, "make worker"],
        )
    if failing:
        return item(
            "attention",
            f"{failing} board(s) failed {FAILING_BOARD_THRESHOLD}+ times in a row. A dead board "
            "yields nothing, which reads exactly like a quiet one.",
            ["make validate-seeds   # checks whether those boards still exist"],
        )
    return item(
        "ok",
        f"Last crawl finished {_ago(finished_age)} ({last_run.status}); "
        f"newest posting first seen {_ago(_age(newest, now))}.",
        [],
    )


async def _inbox_facts(session: AsyncSession) -> dict[str, str | int | float | bool | None]:
    count = await _count(session, select(func.count()).select_from(InboundMessage))
    return {"messages_ingested": count}


async def build_status(session: AsyncSession, *, now: datetime | None = None) -> SetupStatusOut:
    current = now or datetime.now(UTC)
    report = await doctor.run()
    checks = {check.name: check for check in report.checks}

    items: list[SetupItemOut] = [
        SetupItemOut(
            key="api",
            title="API",
            group="core",
            state="ok",
            detail="answering (this page reached it)",
        )
    ]
    for name in ("postgres", "migrations"):
        if name in checks:
            items.append(_from_check(checks[name]))

    database_ok = checks.get("postgres") is None or checks["postgres"].ok
    if database_ok:
        items.append(await worker_item(session, current))

    for name in ("vault", "inbox"):
        if name not in checks:
            continue
        entry = _from_check(checks[name])
        if name == "vault":
            entry.facts["stored_credentials"] = len(doctor.stored_credentials())
        if name == "inbox" and database_ok:
            entry.facts.update(await _inbox_facts(session))
        items.append(entry)

    if database_ok:
        items.append(await registry_item(session))
        items.append(await crawler_item(session, current))

    shown = {"postgres", "migrations", "vault", "inbox", *_REPLACED}
    items.extend(_from_check(check) for name, check in checks.items() if name not in shown)

    overall = max((item.state for item in items), key=lambda state: _RANK[state])
    return SetupStatusOut(generated_at=current, overall=overall, items=items)


__all__ = ["build_status", "crawler_item", "registry_item", "worker_item"]
