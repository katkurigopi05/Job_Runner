"""Setup and recovery — what this installation is missing, and how to fix it.

`GET /setup/status` is read-only and builds everything from diagnostics that
never repeat a secret. `POST /setup/registry-sync` is the one repair offered
here, because it is local, idempotent and previewable: it projects
`seeds/companies.yaml` into the database through the same `sync_registry` that
`make registry-sync` runs, and with `dry_run` (the default) rolls back after
computing the change.

Nothing here starts a crawl, sends mail, or writes a key. Those stay
deliberate acts at a terminal.
"""

from __future__ import annotations

from fastapi import APIRouter

from apps.api.deps import SessionDep
from apps.api.errors import ApiError
from apps.api.setup_status import build_status
from packages.core.enums import ErrorCode
from packages.core.schemas_setup import RegistrySyncIn, RegistrySyncOut, SetupStatusOut

router = APIRouter(prefix="/setup", tags=["setup"])


@router.get("/status", response_model=SetupStatusOut)
async def setup_status(session: SessionDep) -> SetupStatusOut:
    """Every area's state, detail, facts and recovery steps. No secret values."""
    return await build_status(session)


@router.post("/registry-sync", response_model=RegistrySyncOut)
async def registry_sync(body: RegistrySyncIn, session: SessionDep) -> RegistrySyncOut:
    """Preview (default) or apply the registry repair."""
    from packages.crawler.extract import SeedFileError, load_retired, load_seed
    from packages.crawler.registry import sync_registry

    try:
        seeds = load_seed()
        retired = load_retired()
    except SeedFileError as exc:
        raise ApiError(ErrorCode.INVALID_REQUEST, f"registry file cannot be read: {exc}") from exc

    outcome = await sync_registry(session, seeds, retired=retired)
    if body.dry_run:
        await session.rollback()
    else:
        await session.commit()
    return RegistrySyncOut(
        dry_run=body.dry_run,
        summary=("Preview, nothing written: " if body.dry_run else "") + outcome.summary(),
        created=outcome.created,
        verified=outcome.verified,
        newer_in_db=outcome.newer_in_db,
        retired=outcome.retired,
        retired_newer_in_db=outcome.retired_newer_in_db,
        moved_boards=outcome.moved_boards,
    )
