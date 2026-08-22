"""The record of what left this machine.

CLAUDE.md pulls in two directions here and both are right:

- §2.8 permits exactly one third-party upload — the LLM call needed for
  tailoring — and only on the condition that "that call is logged so the owner
  can audit what left the machine."
- §10 says never log résumé contents.

Writing the prompt into structlog satisfies the first and breaks the second: it
puts the owner's résumé into rotating log files, which is a copy nobody decided
to make. Writing nothing satisfies §10 and breaks §2.8.

So the audit trail is a separate artifact from the logs. It records *that* a
call happened, to whom, how large it was, and a digest that proves which text
was sent — enough to answer "what left the machine and when", without becoming
a second copy of the résumé.

The digest is the load-bearing part. Holding the original text, the owner can
recompute it and confirm an entry matches. Without the original, the entry
reveals nothing.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

import structlog

from packages.core.config import get_settings
from packages.llm.prompts import identify

log = structlog.get_logger(__name__)

#: Providers that run on this machine — but see `is_local`, because for Ollama
#: that depends on the model.
LOCAL_PROVIDERS = frozenset({"stub", "ollama"})

#: Ollama serves cloud-hosted models under the same API as local ones, marked
#: only by "cloud" in the model tag. `kimi-k2.6:cloud` and
#: `qwen3-coder:480b-cloud` are not on disk and do not run here — and note the
#: two spell it differently, `:cloud` against `-cloud`, so matching the exact
#: suffix misses one. Matching the substring catches both.
#:
#: It also over-reports: a local model with "cloud" in its name would be
#: recorded as having left. That is the right direction to be wrong in. An
#: audit that under-reports says a résumé stayed here when it did not, which is
#: the one failure this file exists to prevent.
#:
#: Judging locality by provider name alone recorded those calls as
#: `left_machine=False` — the trail asserting a résumé never left the machine
#: while it was being sent to Ollama's servers. §2.8 permits that upload for
#: tailoring, but only *audited*, and an audit that is confidently wrong is
#: worse than none: it answers the one question it exists for, incorrectly.
CLOUD_MODEL_MARKERS = ("cloud",)


def is_local(provider: str, model: str | None = None) -> bool:
    """Whether a call stayed on this machine.

    The model matters, not just the provider. Ollama is local until the model
    is one it hosts remotely.
    """
    if provider not in LOCAL_PROVIDERS:
        return False
    return not (model and any(m in model.lower() for m in CLOUD_MODEL_MARKERS))


@dataclass(frozen=True)
class AuditEntry:
    """One call to a provider."""

    at: str
    provider: str
    #: True when the text crossed the network to a third party.
    left_machine: bool
    task: str
    system_sha256: str
    user_sha256: str
    system_chars: int
    user_chars: int
    #: Which registered prompt produced this call, e.g. "tailor.system", and
    #: its version. None for an unregistered prompt — a user message, or one
    #: nobody has versioned yet. Metadata, not content: §2.8 and §10 both hold.
    prompt_name: str | None = None
    prompt_version: int | None = None
    #: Which model answered. Recorded because provider alone does not say
    #: whether a call stayed here — see `is_local`.
    model: str | None = None

    def matches(self, system: str, user: str) -> bool:
        """Whether this entry records that exact pair of prompts.

        The point of the digest: the owner can hold the résumé they think was
        sent and confirm it against the trail.
        """
        return self.system_sha256 == _digest(system) and self.user_sha256 == _digest(user)


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def digest_of(text: str) -> str:
    """The digest the trail would have recorded for `text`.

    Public because verification is a caller's job, not this module's: holding
    the résumé you believe was sent, you hash it and look for the entry. The
    text itself is never stored, never logged, and never leaves the caller —
    which is the entire reason the trail keeps digests instead of prompts.
    """
    return _digest(text)


def record(
    provider: str,
    system: str,
    user: str,
    *,
    task: str = "unspecified",
    model: str | None = None,
) -> AuditEntry:
    """Authorize, then append one entry to the audit trail and return it.

    Two kinds of failure, deliberately treated differently:

    - **Quota** raises. Called before the request goes out, so a refused call
      never reaches the network. See `packages/llm/quota.py` on why spending
      the allowance parks the work instead of downgrading it.
    - **A broken audit file** does not raise. It must not stop an application,
      and it must not pass silently either, so it is logged.
    """
    from packages.llm.quota import authorize

    authorize(provider)

    prompt = identify(system)

    entry = AuditEntry(
        at=datetime.now(UTC).isoformat(),
        provider=provider,
        model=model,
        left_machine=not is_local(provider, model),
        task=task,
        system_sha256=_digest(system),
        user_sha256=_digest(user),
        system_chars=len(system),
        user_chars=len(user),
        prompt_name=prompt.name if prompt else None,
        prompt_version=prompt.version if prompt else None,
    )

    # Digests and counts only — safe for the log. The prompt itself is not.
    log.info(
        "llm_call",
        provider=entry.provider,
        model=entry.model,
        left_machine=entry.left_machine,
        task=entry.task,
        prompt=prompt.label if prompt else None,
        user_chars=entry.user_chars,
        user_sha256=entry.user_sha256[:12],
    )

    try:
        path = audit_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(asdict(entry)) + "\n")
    except OSError as exc:  # pragma: no cover - filesystem failure
        log.error("llm_audit_write_failed", error=type(exc).__name__)

    return entry


def audit_path() -> Path:
    """Where the trail lives. Outside storage/, next to the vault.

    storage/ holds résumés and screenshots and is the tree you might copy off
    the machine. The record of what was uploaded should not travel with the
    thing that was uploaded.
    """
    return Path(get_settings().vault_root).resolve() / "llm-audit.jsonl"


def read_trail(limit: int | None = None) -> list[AuditEntry]:
    """The trail, oldest first. Empty when nothing has been sent anywhere."""
    path = audit_path()
    if not path.is_file():
        return []

    entries = [AuditEntry(**json.loads(line)) for line in path.read_text().splitlines() if line]
    return entries[-limit:] if limit else entries


def uploads_only(entries: list[AuditEntry] | None = None) -> list[AuditEntry]:
    """Just the calls that actually left the machine — what §2.8 is about."""
    return [
        entry for entry in (entries if entries is not None else read_trail()) if entry.left_machine
    ]
