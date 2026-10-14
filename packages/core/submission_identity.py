"""Versioned URL identity shared with the ledger backfill. Keep v1 stable.

No title/description fuzzy matching: two openings with the same title may be
different jobs. Unknown query parameters are retained, especially job ids.
"""

from __future__ import annotations

import hashlib
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


def submission_key_v1(url: str) -> str:
    parts = urlsplit(url.strip())
    host = parts.netloc.lower()
    path = parts.path.rstrip("/")
    if host in {"boards.greenhouse.io", "job-boards.greenhouse.io"}:
        host = "boards.greenhouse.io"
    if host in {"jobs.lever.co", "jobs.eu.lever.co", "apply.workable.com"}:
        path = path.removesuffix("/apply")
    if host == "jobs.ashbyhq.com":
        path = path.removesuffix("/application")
    query = urlencode(
        sorted(
            (key, value)
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
            if not key.lower().startswith("utm_") and key.lower() != "gh_src"
        )
    )
    normalized = urlunsplit(("https", host, path, query, ""))
    return hashlib.sha256(normalized.encode()).hexdigest()
