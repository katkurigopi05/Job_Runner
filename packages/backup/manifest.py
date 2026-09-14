"""The versioned description of one backup, and the checksums that make it checkable."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

FORMAT_VERSION = 1

MANIFEST = "manifest.json"
DATABASE_FILE = "database.dump"
ARTIFACTS_DIR = "artifacts"
VAULT_DIR = "vault"
VERIFICATION = "verification.json"

#: Top-level `storage/` directories left out unless asked for, and why. Written
#: into the manifest so a restored installation knows what it does not have.
EXCLUDED_BY_DEFAULT = {
    "browser": (
        "browser profiles hold live ATS session cookies; include with --include-browser-profiles"
    ),
    "logs": "logs are regenerated and not needed to restore",
}


class ManifestError(Exception):
    """A manifest this code cannot trust."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


@dataclass(frozen=True)
class FileEntry:
    path: str
    sha256: str
    bytes: int


@dataclass
class Manifest:
    created_at: str
    app_revision: str | None
    alembic_revision: str | None
    database: dict[str, Any]
    artifacts: list[FileEntry] = field(default_factory=list)
    excluded: dict[str, str] = field(default_factory=dict)
    vault: dict[str, Any] = field(default_factory=dict)
    format_version: int = FORMAT_VERSION

    def write(self, directory: Path) -> Path:
        path = directory / MANIFEST
        path.write_text(json.dumps(asdict(self), indent=2, sort_keys=True) + "\n")
        return path

    @classmethod
    def read(cls, directory: Path) -> Manifest:
        path = directory / MANIFEST
        if not path.is_file():
            raise ManifestError(f"{directory} has no {MANIFEST}")
        try:
            raw = json.loads(path.read_text())
        except json.JSONDecodeError as exc:
            raise ManifestError(f"{path} is not valid JSON") from exc
        version = raw.get("format_version")
        if version != FORMAT_VERSION:
            raise ManifestError(
                f"{path} is format {version!r}; this code reads format {FORMAT_VERSION}"
            )
        return cls(
            created_at=raw["created_at"],
            app_revision=raw.get("app_revision"),
            alembic_revision=raw.get("alembic_revision"),
            database=raw["database"],
            artifacts=[FileEntry(**entry) for entry in raw.get("artifacts", [])],
            excluded=raw.get("excluded", {}),
            vault=raw.get("vault", {}),
            format_version=version,
        )


__all__ = [
    "ARTIFACTS_DIR",
    "DATABASE_FILE",
    "EXCLUDED_BY_DEFAULT",
    "FORMAT_VERSION",
    "MANIFEST",
    "VAULT_DIR",
    "VERIFICATION",
    "FileEntry",
    "Manifest",
    "ManifestError",
    "sha256_file",
]
