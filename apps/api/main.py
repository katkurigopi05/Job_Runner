"""FastAPI app. Localhost only — no auth layer, single user. CLAUDE.md §11."""

from __future__ import annotations

from fastapi import FastAPI

from apps.api.errors import register_error_handlers
from apps.api.middleware import LocalhostOnlyMiddleware
from apps.api.routers import (
    analytics,
    applications,
    audit,
    candidates,
    chat,
    crawl,
    detect,
    inbox,
    matches,
    postings,
    profiles,
    projects,
    resumes,
)
from packages.core.config import get_settings

app = FastAPI(
    title="Jobrunner",
    description="Local, single-user job-application agent.",
    version="0.1.0",
)

app.add_middleware(LocalhostOnlyMiddleware, allow_non_local=get_settings().allow_non_local)

register_error_handlers(app)

app.include_router(detect.router)
app.include_router(candidates.router)
app.include_router(profiles.router)
app.include_router(applications.router)
app.include_router(projects.router)
app.include_router(resumes.router)
app.include_router(postings.router)
app.include_router(inbox.router)
app.include_router(chat.router)
app.include_router(crawl.router)
app.include_router(matches.router)
app.include_router(analytics.router)
app.include_router(audit.router)


@app.get("/health", tags=["meta"])
async def health() -> dict[str, str]:
    """Whether the API is up *and* whether it can reach the database.

    It used to return a hardcoded `{"status": "ok"}`, which answered only "did
    this process start". Postgres could be stopped and this still said ok — so
    anything built on it, a dashboard indicator most of all, would report
    healthy while every page that loads data threw. A health check that cannot
    fail is not a health check.

    Still 200 when the database is down, and the body carries the detail. This
    is a liveness endpoint for one local dashboard, not a load-balancer probe:
    the process *is* answering, and a 503 would make the pill unreachable at
    exactly the moment it has something to say. `status` is `degraded` rather
    than `ok` so a caller reading one field still learns something is wrong.

    Never raises. A failure here must not become a second error on top of the
    one it is trying to report.
    """
    from sqlalchemy import text

    from packages.core import db as core_db

    database = "ok"
    try:
        async with core_db.get_sessionmaker()() as session:
            await session.execute(text("SELECT 1"))
    except Exception:  # noqa: BLE001 - reporting the failure is the whole job
        database = "down"

    return {
        "status": "ok" if database == "ok" else "degraded",
        "api": "ok",
        "database": database,
    }
