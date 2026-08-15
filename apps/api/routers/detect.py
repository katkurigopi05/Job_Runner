"""ATS detection — POST /detect, GET /ats."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from packages.ats.registry import detect_ats, supported

router = APIRouter(tags=["ats"])


class DetectRequest(BaseModel):
    url: str


class DetectResponse(BaseModel):
    url: str
    ats: str | None
    supported: bool


@router.post("/detect", response_model=DetectResponse)
async def detect(body: DetectRequest) -> DetectResponse:
    """Identify the ATS behind a posting URL.

    Pure URL-pattern matching — no network call, so it is cheap enough to run
    on every posting the crawler sees.
    """
    ats = detect_ats(body.url)
    return DetectResponse(url=body.url, ats=ats, supported=ats is not None)


@router.get("/ats", response_model=list[str])
async def list_ats() -> list[str]:
    return supported()
