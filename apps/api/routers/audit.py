"""The audit trail, readable.

CLAUDE.md §2.8 permits one third-party upload — the LLM call needed for
tailoring — "and that call is logged so the owner can audit what left the
machine". `packages/llm/audit.py` has recorded every call for some time and
nothing exposed it, so auditing meant reading a JSONL file by hand. A record
nobody can read satisfies the letter of that rule and none of its purpose.

Two things this deliberately does not do.

**It never returns prompt text, because none is stored.** The trail keeps
digests and sizes precisely so it does not become a second copy of the résumé
(§10 forbids logging résumé contents). An endpoint that could show the prompt
would mean the file contained it.

**It does not let the owner search by content.** `/verify` takes text, hashes
it, and reports whether a matching entry exists — the text is never persisted
and never logged. That is the whole audit story: holding the original, you can
prove what was sent; without it, an entry reveals nothing.
"""

from __future__ import annotations

from fastapi import APIRouter, Query

from packages.core.schemas import AuditEntryOut, AuditSummaryOut, AuditVerifyRequest
from packages.llm import audit

router = APIRouter(prefix="/audit", tags=["audit"])

DEFAULT_LIMIT = 200
MAX_LIMIT = 2000


def _out(entry: audit.AuditEntry) -> AuditEntryOut:
    return AuditEntryOut(
        at=entry.at,
        provider=entry.provider,
        model=entry.model,
        left_machine=entry.left_machine,
        task=entry.task,
        prompt_name=entry.prompt_name,
        prompt_version=entry.prompt_version,
        user_chars=entry.user_chars,
        system_chars=entry.system_chars,
        user_sha256=entry.user_sha256,
        system_sha256=entry.system_sha256,
    )


@router.get("", response_model=list[AuditEntryOut])
async def list_calls(
    uploads_only: bool = False,
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
) -> list[AuditEntryOut]:
    """Every provider call, newest last.

    `uploads_only=true` narrows to calls that actually left the machine, which
    is the §2.8 question. Local calls are still recorded and still listed by
    default — a trail that showed only uploads would be a trail that decided
    for the owner what counts.
    """
    entries = audit.read_trail(limit=limit)
    if uploads_only:
        entries = audit.uploads_only(entries)
    return [_out(entry) for entry in entries]


@router.get("/summary", response_model=AuditSummaryOut)
async def summary() -> AuditSummaryOut:
    """The headline: how much has left this machine, and to whom."""
    entries = audit.read_trail()
    uploads = audit.uploads_only(entries)

    by_provider: dict[str, int] = {}
    for entry in uploads:
        key = f"{entry.provider}/{entry.model}" if entry.model else entry.provider
        by_provider[key] = by_provider.get(key, 0) + 1

    return AuditSummaryOut(
        total_calls=len(entries),
        uploads=len(uploads),
        uploaded_chars=sum(entry.user_chars for entry in uploads),
        by_provider=by_provider,
        first_at=entries[0].at if entries else None,
        last_at=entries[-1].at if entries else None,
    )


@router.post("/verify", response_model=list[AuditEntryOut])
async def verify(body: AuditVerifyRequest) -> list[AuditEntryOut]:
    """Entries whose recorded text matches what you paste.

    The point of storing digests rather than text: holding the résumé you think
    was sent, you can confirm it against the trail. The submitted text is
    hashed and discarded — never written to the trail, never logged.
    """
    return [
        _out(entry)
        for entry in audit.read_trail()
        if entry.user_sha256 == audit.digest_of(body.text)
    ]
