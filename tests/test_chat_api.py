"""The assistant — CLAUDE.md §2.2 and §2.8 applied to a chat surface."""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from packages.llm.provider import LLMError, StubProvider


@pytest.mark.parametrize(
    "question",
    [
        "what should I put for work authorization?",
        "draft my employment history section",
        "what salary_expectation should I give them?",
        "do I need sponsorship, what do I say",
    ],
)
async def test_protected_questions_are_refused(client: AsyncClient, question: str) -> None:
    """§2.2 — refused by code, not by asking the model nicely.

    A system prompt is a request. This is a rule, so it is enforced before the
    model is reached at all.
    """
    answered = await client.post("/chat", json={"message": question})

    assert answered.status_code == 200
    body = answered.json()
    assert body["provider"] == "refused"
    assert body["grounded"] is False
    assert "profile" in body["reply"].lower()


async def test_a_request_that_names_no_provider_stays_local(
    client: AsyncClient, monkeypatch
) -> None:
    """The floor did not move when the ceiling did.

    Remote providers are now reachable by name — the owner widened §14
    deliberately. What must not change is the default: a question that says
    nothing about a provider is answered on this machine, `LLM_PROVIDER` is
    still ignored here, and a local model that is down is an error rather than
    a silent promotion to whatever cloud key happens to be configured. That
    promotion is the one thing a per-request switch exists to prevent.
    """
    import apps.api.routers.chat as chat_module

    asked: list[str] = []

    def only_local(name: str | None = None):
        asked.append(name or "default")
        raise LLMError("connection refused")

    monkeypatch.setattr(chat_module.llm_router, "build_provider", only_local)

    answered = await client.post("/chat", json={"message": "how many are waiting on me?"})

    assert answered.status_code == 500
    assert "Nothing was sent anywhere else" in answered.json()["error"]["message"]
    # Asked for ollama by name, and never retried with anything else.
    assert asked == ["ollama"]


async def test_naming_a_cloud_provider_routes_there_and_says_so(
    client: AsyncClient, monkeypatch
) -> None:
    """The feature the owner asked for, and the label that has to come with it.

    `local=False` is computed from the model rather than assumed, so an answer
    that cost privacy is never indistinguishable from one that did not.
    """
    import apps.api.routers.chat as chat_module

    asked: list[str] = []

    class Cloud(StubProvider):
        def __init__(self) -> None:
            super().__init__({"": "gemini answering"})
            self.model = "gemini-3.6-flash"

    def build(name: str | None = None):
        asked.append(name or "default")
        return Cloud()

    monkeypatch.setattr(chat_module.llm_router, "build_provider", build)

    answered = await client.post(
        "/chat", json={"message": "how many are waiting on me?", "provider": "gemini"}
    )

    assert answered.status_code == 200
    body = answered.json()
    assert asked == ["gemini"]
    assert body["provider"] == "gemini"
    assert body["model"] == "gemini-3.6-flash"
    assert body["local"] is False


async def test_a_remote_provider_that_fails_does_not_drop_to_the_local_one(
    client: AsyncClient, monkeypatch
) -> None:
    """Silently answering from a different model than the one asked for is a lie.

    §7's `LLM_FALLBACK_LOCAL` covers tailoring and deliberately does not reach
    the assistant: there the fallback is recorded on the document, here it
    would just be a different answer wearing the same label.
    """
    import apps.api.routers.chat as chat_module

    asked: list[str] = []

    def always_fails(name: str | None = None):
        asked.append(name or "default")
        raise LLMError("upstream unavailable")

    monkeypatch.setattr(chat_module.llm_router, "build_provider", always_fails)

    answered = await client.post(
        "/chat", json={"message": "how many are waiting on me?", "provider": "openrouter"}
    )

    assert answered.status_code == 500
    assert "Nothing fell back" in answered.json()["error"]["message"]
    assert asked == ["openrouter"]


async def test_an_unknown_provider_is_refused(client: AsyncClient) -> None:
    """The allowed set is explicit; `stub` is not in it.

    Canned text presented as an answer about real applications is exactly what
    StubProvider's marker exists to make visible.
    """
    for name in ("stub", "not-a-provider"):
        answered = await client.post(
            "/chat", json={"message": "how many are waiting?", "provider": name}
        )
        assert answered.status_code == 400
        assert answered.json()["error"]["code"] == "invalid_request"


async def test_the_answer_is_grounded_in_real_counts(client: AsyncClient, monkeypatch) -> None:
    """The model is handed facts, not left to invent a status."""
    import apps.api.routers.chat as chat_module

    seen: dict[str, str] = {}

    class Recorder(StubProvider):
        async def complete(
            self,
            system: str,
            user: str,
            *,
            max_tokens: int = 1024,
            temperature: float = 0.2,
        ) -> str:
            seen["user"] = user
            return "three are waiting"

    monkeypatch.setattr(chat_module.llm_router, "build_provider", lambda name=None: Recorder())

    answered = await client.post("/chat", json={"message": "what needs me?"})

    assert answered.status_code == 200
    assert answered.json()["reply"] == "three are waiting"
    assert "APPLICATIONS BY STATUS:" in seen["user"]


async def test_empty_message_is_rejected(client: AsyncClient) -> None:
    answered = await client.post("/chat", json={"message": "   "})
    assert answered.status_code == 400


@pytest.mark.parametrize("model", ["kimi-k2.6:cloud", "qwen3-coder:480b-cloud"])
async def test_a_cloud_model_is_refused_even_though_the_provider_is_ollama(
    client: AsyncClient, monkeypatch, model: str
) -> None:
    """§2.8 — "ollama" stopped being enough to know the answer stayed here.

    Ollama serves cloud-hosted models over the same localhost API. Both of
    these are in the owner's `ollama list`, neither runs on this machine, and
    nothing in the base URL distinguishes them. Since OLLAMA_MODEL became
    settable, one edit to `.env` could send applications and recruiter mail to
    a third party while the reply still said `provider="ollama"`.

    Note the two spell it differently, `:cloud` against `-cloud`, which is why
    `audit.is_local` matches the substring rather than a suffix.
    """
    import apps.api.routers.chat as chat_module

    class CloudModel(StubProvider):
        def __init__(self) -> None:
            super().__init__()
            self.model = model

    monkeypatch.setattr(chat_module.llm_router, "build_provider", lambda name=None: CloudModel())

    answered = await client.post("/chat", json={"message": "how many are waiting on me?"})

    assert answered.status_code == 400
    message = answered.json()["error"]["message"]
    assert model in message
    # The refusal survived the widening. Remote providers are now selectable by
    # name, but asking for the *local* one and silently getting a third party
    # is not a choice anybody made — which is what this guard has always been
    # about, and why it is keyed on the label matching rather than on distance.
    assert "local answer it claims to be" in message.lower()


async def test_a_pulled_local_model_is_not_refused(client: AsyncClient, monkeypatch) -> None:
    """The guard must not reject the models that actually run here."""
    import apps.api.routers.chat as chat_module

    class LocalModel(StubProvider):
        def __init__(self) -> None:
            super().__init__()
            self.model = "llama3.1"
            self.responses = {"": "Three are waiting on you."}

    monkeypatch.setattr(chat_module.llm_router, "build_provider", lambda name=None: LocalModel())

    answered = await client.post("/chat", json={"message": "how many are waiting on me?"})

    assert answered.status_code == 200
    assert answered.json()["provider"] == "ollama"


def test_the_configured_model_is_the_one_that_is_asked_for(monkeypatch) -> None:
    """OLLAMA_MODEL was named in three files and settable in none."""
    from packages.core.config import get_settings
    from packages.llm.provider import OllamaProvider

    get_settings.cache_clear()
    assert OllamaProvider().model == get_settings().ollama_model

    monkeypatch.setenv("OLLAMA_MODEL", "mistral")
    assert OllamaProvider().model == "mistral"


# --------------------------------------------------------------------------
# Recruiter mail, and who is allowed to read it
# --------------------------------------------------------------------------


def _recorder(model: str | None = None) -> tuple[type[StubProvider], dict[str, str]]:
    """A provider that captures its prompt, plus the dict it captures into.

    Per call rather than a class attribute: a subclass writing to
    `type(self).seen` lands on the subclass, so a shared attribute reads back
    the previous test's prompt and an assertion about what was *withheld*
    passes or fails on stale data.
    """
    seen: dict[str, str] = {}

    class Recorder(StubProvider):
        def __init__(self) -> None:
            super().__init__()
            if model is not None:
                self.model = model

        async def complete(
            self,
            system: str,
            user: str,
            *,
            max_tokens: int = 1024,
            temperature: float = 0.2,
        ) -> str:
            seen["user"] = user
            return "ok"

    return Recorder, seen


async def _application_with_mail(worker_session) -> str:
    """An application carrying one recruiter reply."""
    import uuid as _uuid

    from packages.core.models import Application, Candidate, InboundMessage, Profile, User

    suffix = _uuid.uuid4().hex[:8]
    user = User(email=f"o-{suffix}@example.com")
    worker_session.add(user)
    await worker_session.flush()
    candidate = Candidate(user_id=user.id, name="Owner", email=f"c-{suffix}@example.com")
    worker_session.add(candidate)
    await worker_session.flush()
    profile = Profile(candidate_id=candidate.id, label="p")
    worker_session.add(profile)
    await worker_session.flush()
    application = Application(
        candidate_id=candidate.id,
        profile_id=profile.id,
        url=f"https://x.test/{suffix}",
        ats="greenhouse",
    )
    worker_session.add(application)
    await worker_session.flush()
    worker_session.add(
        InboundMessage(
            candidate_id=candidate.id,
            application_id=application.id,
            from_addr="recruiter@bigco.example",
            subject="Next steps on your application",
            body="We would like to schedule a call.",
        )
    )
    await worker_session.commit()
    return str(application.id)


async def test_the_local_model_always_sees_recruiter_mail(
    client: AsyncClient, worker_session, monkeypatch
) -> None:
    """Nothing to withhold it from — it never leaves the machine.

    The toggle is about what crosses the boundary, so on the side of the
    boundary where there is no crossing it does not apply. `share_mail=False`
    is ignored here rather than obeyed, and the reply says so.
    """
    import apps.api.routers.chat as chat_module

    application_id = await _application_with_mail(worker_session)
    recorder, seen = _recorder()
    monkeypatch.setattr(chat_module.llm_router, "build_provider", lambda name=None: recorder())

    answered = await client.post(
        "/chat",
        json={
            "message": "any replies?",
            "application_id": application_id,
            "share_mail": False,
        },
    )

    assert answered.status_code == 200
    assert answered.json()["shared_mail"] is True
    assert "recruiter@bigco.example" in seen["user"]


async def test_a_cloud_model_does_not_get_the_mail_unless_asked(
    client: AsyncClient, worker_session, monkeypatch
) -> None:
    """The default that matters. Picking Gemini must not quietly take the mail.

    Recruiter correspondence is other people's writing about the owner, and
    they never chose a provider — so this is the one part of the context that
    stays behind unless the owner turns it on for that question.
    """
    import apps.api.routers.chat as chat_module

    application_id = await _application_with_mail(worker_session)
    recorder, seen = _recorder("gemini-3.6-flash")
    monkeypatch.setattr(chat_module.llm_router, "build_provider", lambda name=None: recorder())

    answered = await client.post(
        "/chat",
        json={"message": "any replies?", "application_id": application_id, "provider": "gemini"},
    )

    assert answered.status_code == 200
    body = answered.json()
    assert body["local"] is False
    assert body["shared_mail"] is False
    assert "recruiter@bigco.example" not in seen["user"]
    # Withheld out loud. An absent section would read as "no replies arrived",
    # which is a different answer and a wrong one.
    assert "withheld" in seen["user"]


async def test_a_cloud_model_gets_the_mail_when_the_owner_turns_it_on(
    client: AsyncClient, worker_session, monkeypatch
) -> None:
    """The toggle has to actually work, or it is theatre."""
    import apps.api.routers.chat as chat_module

    application_id = await _application_with_mail(worker_session)
    recorder, seen = _recorder("gemini-3.6-flash")
    monkeypatch.setattr(chat_module.llm_router, "build_provider", lambda name=None: recorder())

    answered = await client.post(
        "/chat",
        json={
            "message": "any replies?",
            "application_id": application_id,
            "provider": "gemini",
            "share_mail": True,
        },
    )

    assert answered.status_code == 200
    assert answered.json()["shared_mail"] is True
    assert "recruiter@bigco.example" in seen["user"]


async def test_ollama_cloud_is_selectable_by_name(client: AsyncClient, monkeypatch) -> None:
    """The narrowing the owner asked for, and the shape it landed in.

    The refusal above is keyed on the *label matching*, not on distance — so
    the way to reach a hosted model was never to loosen it, but to give the
    remote path its own name. Asked for by name, the same model that is
    refused as `ollama` answers, and the reply says it was not local.
    """
    import apps.api.routers.chat as chat_module

    class HostedModel(StubProvider):
        def __init__(self) -> None:
            super().__init__()
            self.model = "glm-5.3-flash:cloud"

    monkeypatch.setattr(chat_module.llm_router, "build_provider", lambda name=None: HostedModel())

    answered = await client.post(
        "/chat", json={"message": "how many are waiting on me?", "provider": "ollama_cloud"}
    )

    assert answered.status_code == 200
    body = answered.json()
    assert body["provider"] == "ollama_cloud"
    assert body["local"] is False, "a hosted answer must never be reported as local"


async def test_asking_for_local_still_refuses_the_same_model(
    client: AsyncClient, monkeypatch
) -> None:
    """The other half, and the reason this is a narrowing rather than a hole.

    Identical model, identical endpoint. Named `ollama_cloud` it answers;
    asked for as the local model it is still refused, because that request
    carries a claim about where the answer came from that would be false.
    """
    import apps.api.routers.chat as chat_module

    class HostedModel(StubProvider):
        def __init__(self) -> None:
            super().__init__()
            self.model = "glm-5.3-flash:cloud"

    monkeypatch.setattr(chat_module.llm_router, "build_provider", lambda name=None: HostedModel())

    answered = await client.post(
        "/chat", json={"message": "how many are waiting on me?", "provider": "ollama"}
    )

    assert answered.status_code == 400
    assert "local answer it claims to be" in answered.json()["error"]["message"].lower()
