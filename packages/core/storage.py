"""Blob storage behind an S3-shaped interface.

Everything the agent writes — résumés, tailored PDFs, screenshots, receipts —
goes through here rather than touching paths directly, so swapping the local
backend for S3 later is a constructor change and nothing else.

Résumés and screenshots of half-filled application forms are PII (CLAUDE.md
§2.8). The local backend keeps them under `storage/`, which is gitignored.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Protocol

from packages.core.config import get_settings


class StorageLimitError(Exception):
    """A write exceeded the configured per-file cap."""


class StorageBackend(Protocol):
    """The subset of S3 semantics this project needs."""

    def put(self, key: str, data: bytes) -> str:
        """Write bytes to storage at the given key."""
        ...

    def put_file(self, key: str, source: Path) -> str:
        """Copy a file to storage at the given key."""
        ...

    def get(self, key: str) -> bytes:
        """Read bytes from storage for the given key."""
        ...

    def exists(self, key: str) -> bool:
        """Check if a key exists in storage."""
        ...

    def delete(self, key: str) -> None:
        """Delete a key from storage."""
        ...

    def path_for(self, key: str) -> Path:
        """Return the absolute path for a key."""
        ...


class LocalStorage:
    """Files under a root directory. Keys are POSIX-style relative paths."""

    def __init__(self, root: str | Path | None = None, *, max_file_mb: int | None = None) -> None:
        settings = get_settings()
        self.root = Path(root or settings.storage_root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        limit = settings.storage_max_file_mb if max_file_mb is None else max_file_mb
        self.max_bytes = limit * 1024 * 1024

    def _check_size(self, key: str, size: int) -> None:
        """Screenshots of long postings grow without bound otherwise."""
        if size > self.max_bytes:
            raise StorageLimitError(
                f"{key} is {size / 1024 / 1024:.1f}MB, over the "
                f"{self.max_bytes / 1024 / 1024:.0f}MB limit"
            )

    def _resolve(self, key: str) -> Path:
        """Resolve a key inside the root, refusing to escape it.

        Keys can carry user-influenced text (a company name, a posting id), so
        `../` traversal has to be impossible rather than merely unlikely.
        """
        if not key or key.startswith("/"):
            raise ValueError(f"invalid storage key: {key!r}")
        candidate = (self.root / key).resolve()
        if candidate != self.root and self.root not in candidate.parents:
            raise ValueError(f"storage key escapes root: {key!r}")
        return candidate

    def put(self, key: str, data: bytes) -> str:
        target = self._resolve(key)
        self._check_size(key, len(data))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return key

    def put_file(self, key: str, source: Path) -> str:
        target = self._resolve(key)
        self._check_size(key, source.stat().st_size)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        return key

    def enforce_limit(self, key: str) -> bool:
        """Delete a file already on disk if it is over the cap.

        Used for writes that go straight to a path (a browser screenshot),
        where the size is only knowable afterwards.
        """
        target = self._resolve(key)
        if target.is_file() and target.stat().st_size > self.max_bytes:
            target.unlink()
            return False
        return True

    def get(self, key: str) -> bytes:
        return self._resolve(key).read_bytes()

    def exists(self, key: str) -> bool:
        return self._resolve(key).is_file()

    def delete(self, key: str) -> None:
        target = self._resolve(key)
        if target.is_file():
            target.unlink()

    def path_for(self, key: str) -> Path:
        """Absolute path for a key. Needed by tools that require a real file."""
        return self._resolve(key)


_backend: StorageBackend | None = None


def get_storage() -> StorageBackend:
    """Get the active storage backend, creating it if needed."""
    global _backend
    if _backend is None:
        _backend = LocalStorage()
    return _backend


def set_storage(backend: StorageBackend | None) -> None:
    """Swap the backend. Tests use this to redirect writes to a tmp dir."""
    global _backend
    _backend = backend


def receipt_key(application_id: str, name: str) -> str:
    """Where an application's audit artifacts live."""
    return f"receipts/{application_id}/{name}"


def resume_key(candidate_id: str, version: int, filename: str) -> str:
    """Generate a storage key for a resume."""
    return f"resumes/{candidate_id}/v{version}/{filename}"


def cover_letter_key(application_id: str, filename: str) -> str:
    """Where an application's cover letter lives.

    Keyed by application rather than by candidate, because a letter is written
    for exactly one posting and reusing one across applications is the failure
    `packages/tailor/cache.py` documents for résumés, with less excuse: a
    résumé is mostly the same document twice, a letter is not.
    """
    return f"letters/{application_id}/{filename}"
