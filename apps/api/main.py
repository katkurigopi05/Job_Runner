"""FastAPI app. Localhost only — no auth layer, single user. CLAUDE.md §11."""

from __future__ import annotations

from fastapi import FastAPI

from apps.api.errors import register_error_handlers
from apps.api.routers import applications, candidates, detect, profiles

app = FastAPI(
    title="Jobrunner",
    description="Local, single-user job-application agent.",
    version="0.1.0",
)

register_error_handlers(app)

app.include_router(detect.router)
app.include_router(candidates.router)
app.include_router(profiles.router)
app.include_router(applications.router)


@app.get("/health", tags=["meta"])
async def health() -> dict[str, str]:
    return {"status": "ok"}
