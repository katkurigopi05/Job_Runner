"""Local-only Kaggle reconnect and live readiness; no credentials in responses."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, SecretStr, field_validator

from apps.api.errors import ApiError
from packages.core.enums import ErrorCode
from packages.llm import kaggle_connection
from packages.llm.kaggle import KaggleStatus
from packages.llm.provider import LLMError

router = APIRouter(prefix="/chat/kaggle", tags=["chat"])


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
    try:
        return await kaggle_connection.connect_session(body.server_url, body.kernel_id)
    except LLMError as exc:
        raise ApiError(ErrorCode.INVALID_REQUEST, str(exc)) from None
