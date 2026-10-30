"""Local-only Kaggle reconnect and live readiness; no credentials in responses."""

from __future__ import annotations

import asyncio
from uuid import UUID

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, SecretStr, field_validator

from apps.api.errors import ApiError
from packages.core.enums import ErrorCode
from packages.llm import kaggle_connection, kaggle_setup
from packages.llm.kaggle import (
    DETAILS,
    Connection,
    ConnectionFailure,
    KaggleProvider,
    KaggleStatus,
    connection_path,
    read_connection,
)
from packages.llm.provider import LLMError

router = APIRouter(prefix="/chat/kaggle", tags=["chat"])


class SetupStatus(BaseModel):
    running: bool = False
    detail: str = ""
    connection: KaggleStatus | None = None
    error: str | None = None


_setup_status = SetupStatus()
_setup_task: asyncio.Task[None] | None = None


async def _load_helper(saved: Connection) -> None:
    global _setup_status
    loop = asyncio.get_running_loop()
    destination = connection_path()

    def progress(line: str) -> None:
        # Only fixed setup messages reach the dashboard, never kernel output.
        if not line.startswith("  "):
            loop.call_soon_threadsafe(update_progress, line)

    def update_progress(line: str) -> None:
        global _setup_status
        _setup_status = SetupStatus(running=True, detail=line)

    async def prepare() -> KaggleStatus:
        await kaggle_setup.setup(
            kaggle_setup.Session(saved.server_url), saved, destination, say=progress
        )
        return await KaggleProvider(connection=read_connection(destination)).health()

    try:
        # Session HTTP calls are synchronous. Keep setup off the API event loop
        # and independent of the browser request/proxy timeout.
        status = await asyncio.to_thread(lambda: asyncio.run(prepare()))
        _setup_status = SetupStatus(detail=status.detail, connection=status)
    except ConnectionFailure as exc:
        _setup_status = SetupStatus(error=DETAILS[exc.state])
    except Exception:
        # Transport and validation exceptions may contain the saved credential.
        _setup_status = SetupStatus(
            error="Could not load the Kaggle helper. Check that GPU is enabled and the "
            "model is available in the notebook, then retry."
        )


@router.get("/setup", response_model=SetupStatus)
async def kaggle_setup_status() -> SetupStatus:
    return _setup_status


@router.post("/setup", response_model=SetupStatus, status_code=202)
async def kaggle_load_helper() -> SetupStatus:
    global _setup_status, _setup_task
    if _setup_task is not None and not _setup_task.done():
        return _setup_status
    try:
        saved = read_connection(connection_path())
    except LLMError:
        raise ApiError(ErrorCode.INVALID_REQUEST, DETAILS["unconfigured"]) from None
    _setup_status = SetupStatus(running=True, detail="Preparing the Kaggle model and helper…")
    _setup_task = asyncio.create_task(_load_helper(saved))
    return _setup_status


class ConnectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    server_url: SecretStr
    kernel_id: UUID | None = None

    @field_validator("server_url")
    @classmethod
    def bounded(cls, value: SecretStr) -> SecretStr:
        if not 1 <= len(value.get_secret_value()) <= 2048:
            raise ValueError("Provide the current Kaggle VSCode Compatible URL")
        return value


@router.get("/status", response_model=KaggleStatus)
async def kaggle_status() -> KaggleStatus:
    return await kaggle_connection.saved_status()


@router.post("/connect", response_model=KaggleStatus)
async def kaggle_connect(body: ConnectRequest) -> KaggleStatus:
    if _setup_task is not None and not _setup_task.done():
        raise ApiError(
            ErrorCode.INVALID_STATE, "Wait for Kaggle setup to finish before reconnecting."
        )
    try:
        return await kaggle_connection.connect_session(body.server_url, body.kernel_id)
    except LLMError as exc:
        raise ApiError(ErrorCode.INVALID_REQUEST, str(exc)) from None
