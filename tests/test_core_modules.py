"""Storage, vault, and the LLM stub — the Phase 0 modules.

None of these need a database.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from packages.core.storage import LocalStorage, receipt_key, resume_key
from packages.core.vault import Vault, VaultError, VaultKeyMissingError, generate_key
from packages.llm.provider import LLMError, StubProvider, build_provider

# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------


def test_put_and_get(tmp_path) -> None:
    storage = LocalStorage(tmp_path)
    storage.put("receipts/app-1/shot.png", b"binary")
    assert storage.get("receipts/app-1/shot.png") == b"binary"
    assert storage.exists("receipts/app-1/shot.png")


def test_put_file_and_delete(tmp_path) -> None:
    source = tmp_path / "src.pdf"
    source.write_bytes(b"%PDF")
    storage = LocalStorage(tmp_path / "root")

    storage.put_file("resumes/c1/v1/resume.pdf", source)
    assert storage.get("resumes/c1/v1/resume.pdf") == b"%PDF"

    storage.delete("resumes/c1/v1/resume.pdf")
    assert not storage.exists("resumes/c1/v1/resume.pdf")


def test_nested_keys_create_directories(tmp_path) -> None:
    storage = LocalStorage(tmp_path)
    storage.put("a/b/c/d.txt", b"x")
    assert storage.path_for("a/b/c/d.txt").is_file()


@pytest.mark.parametrize(
    "key",
    ["../escape.txt", "a/../../escape.txt", "/absolute.txt", ""],
)
def test_keys_cannot_escape_the_root(tmp_path, key: str) -> None:
    """Keys carry user-influenced text, so traversal must be impossible."""
    storage = LocalStorage(tmp_path / "root")
    with pytest.raises(ValueError):
        storage.put(key, b"x")


def test_key_helpers() -> None:
    assert receipt_key("app-1", "filled.png") == "receipts/app-1/filled.png"
    assert resume_key("c-1", 2, "cv.pdf") == "resumes/c-1/v2/cv.pdf"


def test_delete_is_idempotent(tmp_path) -> None:
    LocalStorage(tmp_path).delete("never/existed.txt")


# --------------------------------------------------------------------------
# Vault
# --------------------------------------------------------------------------


def test_roundtrip(tmp_path) -> None:
    vault = Vault(key=generate_key(), root=tmp_path)
    vault.put("cand-1", {"workday_password": "hunter2"})
    assert vault.get("cand-1") == {"workday_password": "hunter2"}


def test_ciphertext_does_not_contain_the_secret(tmp_path) -> None:
    """The whole point: plaintext never lands on disk."""
    vault = Vault(key=generate_key(), root=tmp_path)
    vault.put("cand-1", {"password": "super-secret-value"})

    on_disk = (tmp_path / "cand-1.enc").read_bytes()
    assert b"super-secret-value" not in on_disk
    assert b"password" not in on_disk


def test_wrong_key_cannot_decrypt(tmp_path) -> None:
    Vault(key=generate_key(), root=tmp_path).put("cand-1", {"a": "b"})

    with pytest.raises(VaultError, match="wrong VAULT_KEY"):
        Vault(key=generate_key(), root=tmp_path).get("cand-1")


def test_missing_key_refuses_rather_than_storing_plaintext(tmp_path, monkeypatch) -> None:
    from packages.core.config import get_settings

    monkeypatch.delenv("VAULT_KEY", raising=False)
    get_settings.cache_clear()
    try:
        with pytest.raises(VaultKeyMissingError):
            Vault(root=tmp_path)
    finally:
        get_settings.cache_clear()


def test_invalid_key_is_rejected(tmp_path) -> None:
    with pytest.raises(VaultError, match="not a valid Fernet key"):
        Vault(key="not-a-real-key", root=tmp_path)


def test_unknown_ref_raises(tmp_path) -> None:
    with pytest.raises(VaultError, match="no secrets stored"):
        Vault(key=generate_key(), root=tmp_path).get("nope")


@pytest.mark.parametrize("ref", ["../escape", "a/b", ".."])
def test_refs_cannot_traverse(tmp_path, ref: str) -> None:
    vault = Vault(key=generate_key(), root=tmp_path)
    with pytest.raises(ValueError):
        vault.put(ref, {"a": "b"})


def test_repr_does_not_leak(tmp_path) -> None:
    vault = Vault(key=generate_key(), root=tmp_path)
    assert "redacted" in repr(vault)


# --------------------------------------------------------------------------
# LLM stub
# --------------------------------------------------------------------------


class _Answer(BaseModel):
    text: str


async def test_stub_returns_canned_response() -> None:
    provider = StubProvider({"tailor": "rewritten bullet"})
    assert await provider.complete("sys", "please tailor this") == "rewritten bullet"


async def test_stub_marks_unknown_prompts_obviously() -> None:
    """An unrecognized prompt must not return plausible-looking prose."""
    provider = StubProvider()
    assert await provider.complete("sys", "anything") == StubProvider.UNKNOWN


async def test_stub_records_calls_for_audit() -> None:
    provider = StubProvider()
    await provider.complete("sys", "first")
    await provider.complete("sys", "second")

    assert len(provider.calls) == 2
    assert provider.last_prompt() == ("sys", "second")


async def test_stub_json_mode() -> None:
    provider = StubProvider({"classify": '{"text": "interview"}'})
    result = await provider.complete_json("sys", "classify this", _Answer)
    assert result.text == "interview"


async def test_stub_json_fails_loudly_without_a_canned_answer() -> None:
    with pytest.raises(LLMError):
        await StubProvider().complete_json("sys", "unknown", _Answer)


def test_build_provider_defaults_to_stub(monkeypatch) -> None:
    from packages.core.config import get_settings

    get_settings.cache_clear()
    try:
        assert build_provider().name == "stub"
    finally:
        get_settings.cache_clear()


def test_unimplemented_provider_raises() -> None:
    with pytest.raises(LLMError, match="not implemented"):
        build_provider("anthropic")


def test_vault_root_is_outside_storage(monkeypatch, tmp_path) -> None:
    """Encrypted credentials must not live in the tree you copy off the box.

    storage/ holds résumés, screenshots, and browser profiles. If the vault
    sat inside it, backing up or inspecting that tree would carry the
    ciphertext along with the PII.
    """
    from pathlib import Path

    from packages.core.config import get_settings

    get_settings.cache_clear()
    try:
        settings = get_settings()
        storage_root = Path(settings.storage_root).resolve()
        vault_root = Path(settings.vault_root).resolve()
        assert storage_root != vault_root
        assert storage_root not in vault_root.parents
    finally:
        get_settings.cache_clear()
