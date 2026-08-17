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

log = structlog.get_logger(__name__)

#: Local providers never leave the machine, so §2.8 does not apply to them.
#: They are still recorded, marked, so the trail is complete rather than
#: selectively honest.
LOCAL_PROVIDERS = frozenset({"stub", "ollama"})


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

    def matches(self, system: str, user: str) -> bool:
        """Whether this entry records that exact pair of prompts.

        The point of the digest: the owner can hold the résumé they think was
        sent and confirm it against the trail.
        """
        return self.system_sha256 == _digest(system) and self.user_sha256 == _digest(user)


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def record(provider: str, system: str, user: str, *, task: str = "unspecified") -> AuditEntry:
    """Append one entry to the audit trail and return it.

    Never raises into the caller's path: a broken audit file must not stop an
    application, but it also must not pass silently, so the failure is logged.
    """
    entry = AuditEntry(
        at=datetime.now(UTC).isoformat(),
        provider=provider,
        left_machine=provider not in LOCAL_PROVIDERS,
        task=task,
        system_sha256=_digest(system),
        user_sha256=_digest(user),
        system_chars=len(system),
        user_chars=len(user),
    )

    # Digests and counts only — safe for the log. The prompt itself is not.
    log.info(
        "llm_call",
        provider=entry.provider,
        left_machine=entry.left_machine,
        task=entry.task,
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
