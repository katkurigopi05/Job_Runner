"""Encrypted storage for ATS account credentials.

CLAUDE.md §2.7: secrets never touch the database in plaintext and never appear
in logs. Callers store a `secrets_ref` on the candidate row; the ciphertext
lives in a file outside the database, and the key comes from the environment
(or, later, the OS keychain) — never from the repo.

`SecretStr`-style discipline is enforced by shape: `get()` returns the plaintext
only when explicitly asked, and nothing here has a `__repr__` that leaks.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from packages.core.config import get_settings


class VaultError(Exception):
    """Vault is unusable — missing key, wrong key, or corrupt payload."""


class VaultKeyMissingError(VaultError):
    """No VAULT_KEY configured. Refuse rather than store plaintext."""


def generate_key() -> str:
    """A fresh Fernet key, for `VAULT_KEY` in .env."""
    return Fernet.generate_key().decode()


class Vault:
    """Fernet-encrypted key/value store, one JSON blob per reference."""

    def __init__(self, key: str | None = None, root: str | Path | None = None) -> None:
        settings = get_settings()
        raw_key = key or settings.vault_key or os.environ.get("VAULT_KEY")
        if not raw_key:
            raise VaultKeyMissingError(
                "VAULT_KEY is not set. Generate one with "
                "`python -c 'from packages.core.vault import generate_key; "
                "print(generate_key())'` and put it in .env."
            )
        try:
            self._fernet = Fernet(raw_key.encode() if isinstance(raw_key, str) else raw_key)
        except (ValueError, TypeError) as exc:
            raise VaultError("VAULT_KEY is not a valid Fernet key") from exc

        # Deliberately not inside storage/ — secrets and PII have different
        # handling, and conflating them makes it easy to sync the wrong tree.
        self.root = Path(root or Path(settings.storage_root) / ".secrets").resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, ref: str) -> Path:
        if not ref or "/" in ref or ".." in ref:
            raise ValueError(f"invalid secrets_ref: {ref!r}")
        return self.root / f"{ref}.enc"

    def put(self, ref: str, secrets: dict[str, str]) -> str:
        """Encrypt and store. Returns the reference to persist on the row."""
        payload = json.dumps(secrets).encode()
        self._path(ref).write_bytes(self._fernet.encrypt(payload))
        return ref

    def get(self, ref: str) -> dict[str, str]:
        """Decrypt. Callers must not log the result."""
        path = self._path(ref)
        if not path.is_file():
            raise VaultError(f"no secrets stored under {ref!r}")
        try:
            decrypted = self._fernet.decrypt(path.read_bytes())
        except InvalidToken as exc:
            raise VaultError(
                f"cannot decrypt {ref!r} — wrong VAULT_KEY, or the file is corrupt"
            ) from exc
        loaded: dict[str, str] = json.loads(decrypted)
        return loaded

    def exists(self, ref: str) -> bool:
        return self._path(ref).is_file()

    def delete(self, ref: str) -> None:
        path = self._path(ref)
        if path.is_file():
            path.unlink()

    def __repr__(self) -> str:  # pragma: no cover - defensive
        return f"<Vault root={self.root} keys=redacted>"
