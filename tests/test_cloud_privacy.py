"""Synthetic contacts must not cross any production cloud-provider boundary."""

from __future__ import annotations

import asyncio
import json
from typing import Literal

import pytest
from pydantic import BaseModel, EmailStr

from packages.llm import privacy
from packages.llm.provider import LLMError, StubProvider, build_provider, stream_text

NAME = "Alex Morgan"
EMAIL = "alex.morgan@example.net"
PHONE = "+1 (202) 555-0147"


@pytest.fixture
def identities(monkeypatch):
    values = privacy.Identifiers(names=(NAME,), emails=(EMAIL,), phones=(PHONE,))

    async def read():
        return values

    monkeypatch.setattr(privacy, "load_identifiers", read)
    return values


def test_contact_patterns_and_known_names_are_masked_and_restore_exactly(identities) -> None:
    text = f"{NAME}: {EMAIL}, {PHONE}; new.sender@example.org; 202-555-0173"
    redactor = privacy.Redactor(identities)
    masked = redactor.mask(text)
    for value in (NAME, "Alex", "Morgan", EMAIL, PHONE, "new.sender@example.org", "202-555-0173"):
        assert value not in masked
    assert redactor.restore(masked) == text
    assert NAME not in repr(redactor) and EMAIL not in repr(identities)


def test_url_encoding_does_not_hide_contact_values(identities) -> None:
    text = "https://example.org/Alex-Morgan?email=alex.morgan%40example.net&phone=%2B1%20202%20555%200147"
    masked = privacy.Redactor(identities).mask(text)
    assert "Alex" not in masked and "Morgan" not in masked
    assert "example.net" not in masked and "0147" not in masked


@pytest.mark.parametrize("path", ["Alex_Morgan", "Alex-O'Neal"])
def test_name_variants_in_url_paths_do_not_leave_identifying_fragments(path) -> None:
    names = privacy.Identifiers(names=("Alex Morgan", "Alex O'Neal"))
    redactor = privacy.Redactor(names)
    original = f"https://example.org/{path}"
    masked = redactor.mask(original)
    assert not any(word in masked for word in ("Alex", "Morgan", "Neal"))
    assert redactor.restore(masked) == original


def test_encoded_urls_restore_without_normalizing_them(identities) -> None:
    text = "https://example.org/Alex-Morgan?email=alex.morgan%40example.net"
    redactor = privacy.Redactor(identities)
    assert redactor.restore(redactor.mask(text)) == text


def test_unicode_json_escaped_known_names_are_masked() -> None:
    redactor = privacy.Redactor(privacy.Identifiers(names=("José Example",)))
    text = json.dumps({"name": "José Example"})
    masked = redactor.mask(text)
    assert "Jos" not in masked and "Example" not in masked
    assert redactor.restore(masked) == text


@pytest.mark.parametrize("phone", ["2025550147", "+44 20 7946 0958", "+91 98765 43210"])
def test_common_phone_formats_are_masked(phone) -> None:
    redactor = privacy.Redactor(privacy.Identifiers())
    assert phone not in redactor.mask(phone)


def test_damaged_placeholders_are_refused_locally(identities) -> None:
    redactor = privacy.Redactor(identities)
    masked = redactor.mask(EMAIL)
    with pytest.raises(LLMError, match="privacy"):
        redactor.restore(masked.replace("_0]", "_99]"))


def test_counts_dates_and_technical_facts_are_preserved(identities) -> None:
    text = (
        "[P1] 241 Kafka jobs at 104 companies; 2026-10-21; 2024–2025; "
        "Python 3.12; 20%; Qwen3 8B; $120,000."
    )
    assert privacy.Redactor(identities).mask(text) == text


class Remote(StubProvider):
    name = "example_cloud"
    model = "synthetic-model"

    async def complete(self, system, user, **kwargs):
        self.calls.append((system, user))
        return user

    async def complete_json(self, system, user, schema):
        self.calls.append((system, user))
        return schema.model_validate_json(user)

    async def stream(self, system, user, **kwargs):
        self.calls.append((system, user))
        try:
            for char in user:
                yield char
        finally:
            self.closed = True


async def test_text_and_split_stream_tokens_restore_only_locally(identities) -> None:
    for streaming in (False, True):
        remote = Remote()
        guarded = privacy.protect_provider(remote)
        prompt = f"Earlier: {NAME}, {EMAIL}. Mail: {PHONE}. Question: {NAME}."
        if streaming:
            result = "".join([piece async for piece in stream_text(guarded, NAME, prompt)])
            assert remote.closed
        else:
            result = await guarded.complete(NAME, prompt)
        assert result == prompt
        sent = str(remote.calls)
        assert NAME not in sent and EMAIL not in sent and PHONE not in sent


async def test_json_is_restored_before_email_validation(identities) -> None:
    class Contact(BaseModel):
        name: str
        email: EmailStr
        phone: str

    original = {"name": NAME, "email": EMAIL, "phone": PHONE}
    remote = Remote()
    answer = await privacy.protect_provider(remote).complete_json(
        "Return contact JSON", json.dumps(original), Contact
    )
    assert answer.model_dump() == original
    assert EMAIL not in str(remote.calls)


async def test_schema_defaults_enums_aliases_and_quoted_strings_are_protected(identities) -> None:
    from pydantic import Field

    class Contact(BaseModel):
        name: Literal[NAME] = NAME
        email: EmailStr = Field(alias=EMAIL, default=EMAIL)
        note: str

    class SchemaRemote(Remote):
        async def complete_json(self, system, user, schema):
            self.calls.append((system, user, json.dumps(schema.model_json_schema())))
            return schema.model_validate_json(user)

    original = {"name": NAME, EMAIL: EMAIL, "note": f'{NAME} says "hello"'}
    remote = SchemaRemote()
    answer = await privacy.protect_provider(remote).complete_json(
        "system", json.dumps(original), Contact
    )
    assert answer.model_dump(by_alias=True) == original
    assert NAME not in str(remote.calls) and EMAIL not in str(remote.calls)


async def test_json_restores_escaped_unicode_and_quotes_before_validating(monkeypatch) -> None:
    name = 'José "JJ" Example'

    async def read():
        return privacy.Identifiers(names=(name,))

    monkeypatch.setattr(privacy, "load_identifiers", read)

    class Contact(BaseModel):
        name: Literal['José "JJ" Example']

    answer = await privacy.protect_provider(Remote()).complete_json(
        "system", json.dumps({"name": name}), Contact
    )
    assert answer.name == name


async def test_names_matching_schema_keywords_do_not_break_the_schema(monkeypatch) -> None:
    async def read():
        return privacy.Identifiers(names=("String Type",))

    monkeypatch.setattr(privacy, "load_identifiers", read)

    class Contact(BaseModel):
        name: str = "String Type"

    class SchemaRemote(Remote):
        async def complete_json(self, system, user, schema):
            spec = schema.model_json_schema()
            assert spec["type"] == "object"
            assert spec["properties"]["name"]["type"] == "string"
            assert "String Type" not in json.dumps(spec)
            return schema.model_validate_json(user)

    assert (
        await privacy.protect_provider(SchemaRemote()).complete_json(
            "system", json.dumps({"name": "String Type"}), Contact
        )
    ).name == "String Type"


async def test_json_validation_failure_does_not_expose_restored_values(identities) -> None:
    class Contact(BaseModel):
        email: int

    with pytest.raises(LLMError) as raised:
        await privacy.protect_provider(Remote()).complete_json(
            "system", json.dumps({"email": EMAIL}), Contact
        )
    assert EMAIL not in str(raised.value)


async def test_cancelling_a_stream_closes_the_transport(identities) -> None:
    remote = Remote()
    pieces = stream_text(privacy.protect_provider(remote), "system", f"prefix {EMAIL}")
    assert await anext(pieces) == "p"
    await pieces.aclose()
    assert remote.closed


async def test_parallel_calls_do_not_share_substitutions(identities) -> None:
    remote = Remote()
    guarded = privacy.protect_provider(remote)
    results = await asyncio.gather(guarded.complete("s", EMAIL), guarded.complete("s", PHONE))
    assert results == [EMAIL, PHONE]
    assert remote.calls[0][1] != remote.calls[1][1]


async def test_failed_cloud_call_falls_back_locally_with_original_contacts(
    identities, monkeypatch
) -> None:
    from packages.llm.router import FallbackProvider

    class Broken(Remote):
        async def complete(self, system, user, **kwargs):
            self.calls.append((system, user))
            raise LLMError("Synthetic cloud unavailable")

    remote = Broken()
    local = StubProvider({EMAIL: "local answer"})
    provider = FallbackProvider(privacy.protect_provider(remote), "tailor_resume")
    monkeypatch.setattr(provider, "_local", lambda: local)
    assert await provider.complete(NAME, EMAIL) == "local answer"
    assert EMAIL not in str(remote.calls)
    assert local.calls == [(NAME, EMAIL)]


async def test_local_calls_do_not_read_contacts_or_redact(monkeypatch) -> None:
    async def forbidden():
        raise AssertionError("Local model must not load cloud privacy data")

    monkeypatch.setattr(privacy, "load_identifiers", forbidden)
    local = StubProvider({"": "local"})
    assert privacy.protect_provider(local) is local
    await local.complete("system", f"{NAME} {EMAIL} {PHONE}")
    assert EMAIL in local.calls[0][1]


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "[::1]", "gpu.example.net"])
async def test_only_loopback_ollama_is_exempt_from_masking(host, identities) -> None:
    remote = Remote()
    remote.name = "ollama"
    remote.base_url = f"http://{host}:11434"
    guarded = privacy.protect_provider(remote)
    assert await guarded.complete("system", EMAIL) == EMAIL
    if host == "gpu.example.net":
        assert isinstance(guarded, privacy.PrivacyProvider)
        assert EMAIL not in str(remote.calls)
    else:
        assert guarded is remote
        assert EMAIL in str(remote.calls)


async def test_identity_lookup_failure_blocks_before_the_provider(monkeypatch, capsys) -> None:
    async def broken():
        raise RuntimeError(f"private {NAME} {EMAIL}")

    monkeypatch.setattr(privacy, "load_identifiers", broken)
    remote = Remote()
    with pytest.raises(LLMError, match="privacy guard") as raised:
        await privacy.protect_provider(remote).complete("system", EMAIL)
    assert not remote.calls
    assert NAME not in str(raised.value) and EMAIL not in str(raised.value)
    assert EMAIL not in capsys.readouterr().out


@pytest.mark.parametrize(
    "provider", ["gemini", "anthropic", "openrouter", "tokenrouter", "ollama_cloud"]
)
async def test_each_cloud_factory_masks_the_wire_payload(provider, identities, monkeypatch) -> None:
    import httpx

    for key in ("GEMINI_API_KEY", "ANTHROPIC_API_KEY", "OPENROUTER_API_KEY", "TOKENROUTER_API_KEY"):
        monkeypatch.setenv(key, "synthetic-test-key")
    monkeypatch.setenv("OLLAMA_CLOUD_MODEL", "synthetic:cloud")
    seen = []
    sent_users = []

    class Response:
        status_code = 200
        headers = {}

        def raise_for_status(self):
            pass

        def json(self):
            return {
                "message": {"content": self.content},
                "content": [{"text": self.content}],
                "choices": [{"message": {"content": self.content}}],
                "candidates": [
                    {"content": {"parts": [{"text": self.content}]}, "finishReason": "STOP"}
                ],
            }

    async def post(self, url, **kwargs):
        payload = kwargs["json"]
        seen.append(json.dumps(payload))
        response = Response()
        response.content = (
            payload["contents"][0]["parts"][0]["text"]
            if "contents" in payload
            else payload["messages"][-1]["content"]
        )
        sent_users.append(response.content)
        return response

    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    guarded = build_provider(provider)
    assert isinstance(guarded, privacy.PrivacyProvider)
    assert await guarded.complete(NAME, f"{EMAIL} {PHONE}") == f"{EMAIL} {PHONE}"

    class Contact(BaseModel):
        name: Literal[NAME] = NAME
        email: EmailStr = EMAIL

    original = {"name": NAME, "email": EMAIL}
    assert (
        await guarded.complete_json(NAME, json.dumps(original), Contact)
    ).model_dump() == original
    assert len(seen) == 2 and not any(value in str(seen) for value in (NAME, EMAIL, PHONE))

    from packages.llm.audit import digest_of, read_trail

    # The trail records what crossed the boundary, rather than hashes of a
    # different, unmasked prompt. No restoration dictionary is persisted.
    assert read_trail()[0].user_sha256 == digest_of(sent_users[0])
    assert read_trail()[0].user_sha256 != digest_of(f"{EMAIL} {PHONE}")
    assert all(entry.left_machine for entry in read_trail())


async def test_contact_source_reads_all_profiles_and_resume_versions(db_session) -> None:
    from packages.core.models import Candidate, Profile, Resume, User

    user = User(email="account@example.net")
    db_session.add(user)
    await db_session.flush()
    candidate = Candidate(
        user_id=user.id, name=NAME, email=EMAIL, managed_alias="alias@example.net"
    )
    db_session.add(candidate)
    await db_session.flush()
    db_session.add_all(
        [
            Profile(candidate_id=candidate.id, label="first", phone=PHONE),
            Profile(
                candidate_id=candidate.id,
                label="second",
                answers_kv_json={
                    "preferred_name": "Alexis",
                    "mobile_number": "+44 20 7946 0958",
                    "employment_history": "Private unrelated text must not enter the dictionary",
                },
            ),
            Resume(
                candidate_id=candidate.id,
                version=9,
                storage_ref="synthetic/base.pdf",
                parsed_json={
                    "contact": {
                        "name": "Alexandra Example",
                        "email": "old@example.net",
                        "phone": "2025550173",
                    },
                    "raw_lines": ["Unrelated résumé body"],
                },
            ),
            Resume(
                candidate_id=candidate.id,
                version=13,
                storage_ref="synthetic/new.pdf",
                parsed_json=None,
            ),
        ]
    )
    await db_session.flush()

    contacts = await privacy.read_identifiers(db_session)
    assert set(contacts.names) == {NAME, "Alexis", "Alexandra Example"}
    assert set(contacts.emails) == {
        EMAIL,
        "account@example.net",
        "alias@example.net",
        "old@example.net",
    }
    assert set(contacts.phones) == {PHONE, "+44 20 7946 0958", "2025550173"}
    assert not db_session.dirty


async def test_tailoring_restores_identifiers_before_fact_checks(identities) -> None:
    from packages.tailor.guard import SourceCorpus
    from packages.tailor.rewrite import tailor_bullet

    class BulletRemote(Remote):
        async def complete(self, system, user, **kwargs):
            self.calls.append((system, user))
            return user.split("Original bullet:\n")[1].split("\n\n")[0]

    bullet = f"Built Python services for {NAME}, serving 20 customers."
    corpus = SourceCorpus.from_texts(bullet)
    remote = BulletRemote()
    result = await tailor_bullet(
        privacy.protect_provider(remote), bullet, f"Python; {EMAIL}; {PHONE}", corpus
    )
    assert result.tailored == bullet
    assert not result.used_fallback and not result.provider_failed
    assert not any(value in str(remote.calls) for value in (NAME, EMAIL, PHONE))


@pytest.mark.parametrize("old,new", [("_0]", "_99]"), ("[JR_", "JR_"), ("_0]", "_0")])
async def test_damaged_stream_markers_are_not_returned_as_contact_details(
    identities, old, new
) -> None:
    class Damaged(Remote):
        async def stream(self, system, user, **kwargs):
            for char in user.replace(old, new):
                yield char

    with pytest.raises(LLMError, match="privacy"):
        _ = "".join(
            [
                chunk
                async for chunk in stream_text(privacy.protect_provider(Damaged()), "system", EMAIL)
            ]
        )
