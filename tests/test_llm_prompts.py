"""Prompt versioning and the daily quota.

The version beside a prompt is only worth recording if it is true. The pinned
digests below are the mechanism that makes it true: edit a prompt without
bumping its version and this file fails, which is the whole point.
"""

from __future__ import annotations

import pytest

from packages.llm import audit, quota
from packages.llm.prompts import REGISTRY, identify
from packages.llm.provider import StubProvider
from packages.llm.quota import QuotaExceeded

#: (name, version, sha256). Bump the version *and* this digest together.
#: A digest change with the same version is the failure this catches — the
#: trail would then label old and new output identically.
PINNED = [
    ("tailor.system", 2, "93d2208664a6e3895bd72b9a48ef6daa9dd255830b1a50345b15c201a226b31b"),
    (
        "inbox.classify.system",
        1,
        "2fdebc22e4494764980b48065467286f1386ec34650a2ef6a10aa493d0be1ab0",
    ),
    ("assistant.system", 1, "8495f35bf42b55024f681088d1f8fcc2b85ad6e56819cb68d1da1aaf12f2b63d"),
]


def test_every_prompt_matches_its_pinned_digest() -> None:
    """If this fails you edited a prompt. Bump its version and this table."""
    actual = [(p.name, p.version, p.digest) for p in REGISTRY]
    assert actual == PINNED


def test_the_registry_is_the_only_definition() -> None:
    """The modules must not carry their own copy, or the two can drift and
    the trail would name a version that produced something else."""
    import importlib

    from packages.inbox.classify import CLASSIFY_SYSTEM_PROMPT
    from packages.tailor.rewrite import SYSTEM_PROMPT

    chat = importlib.import_module("apps.api.routers.chat")

    assert identify(SYSTEM_PROMPT) is not None
    assert identify(CLASSIFY_SYSTEM_PROMPT) is not None
    assert identify(chat.SYSTEM) is not None


def test_an_unregistered_prompt_is_not_labelled() -> None:
    """Unlabelled beats mislabelled."""
    assert identify("some ad-hoc prompt") is None


def test_prompt_names_are_unique() -> None:
    names = [p.name for p in REGISTRY]
    assert len(names) == len(set(names))


async def test_the_trail_records_which_prompt_ran(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(audit, "audit_path", lambda: tmp_path / "trail.jsonl")

    from packages.tailor.rewrite import SYSTEM_PROMPT

    await StubProvider().complete(SYSTEM_PROMPT, "a bullet")

    entry = audit.read_trail()[-1]
    assert entry.prompt_name == "tailor.system"
    assert entry.prompt_version == 2


async def test_an_unversioned_prompt_still_records(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(audit, "audit_path", lambda: tmp_path / "trail.jsonl")

    await StubProvider().complete("ad hoc", "question")

    entry = audit.read_trail()[-1]
    assert entry.prompt_name is None
    assert entry.user_sha256  # still fully auditable — §2.8 is unaffected


# --------------------------------------------------------------------------
# Quota
# --------------------------------------------------------------------------


def test_local_providers_are_never_limited() -> None:
    """Nothing leaves the machine, so there is no allowance to spend."""
    assert quota.limit_for("ollama") is None
    assert quota.limit_for("stub") is None


def test_a_remote_provider_is_limited(monkeypatch) -> None:
    monkeypatch.setenv("LLM_DAILY_REMOTE_CALLS", "5")
    from packages.core.config import get_settings

    get_settings.cache_clear()
    assert quota.limit_for("gemini") == 5
    get_settings.cache_clear()


def test_zero_means_unlimited(monkeypatch) -> None:
    monkeypatch.setenv("LLM_DAILY_REMOTE_CALLS", "0")
    from packages.core.config import get_settings

    get_settings.cache_clear()
    assert quota.limit_for("gemini") is None
    get_settings.cache_clear()


def test_a_spent_allowance_refuses_rather_than_downgrading(tmp_path, monkeypatch) -> None:
    """The tempting behaviour is to fall back to a local model. That would put
    two quality tiers in one résumé with nothing recording the seam."""
    monkeypatch.setattr(audit, "audit_path", lambda: tmp_path / "trail.jsonl")
    monkeypatch.setattr(quota, "limit_for", lambda provider: 2)
    monkeypatch.setattr(quota, "used_today", lambda provider: 2)

    with pytest.raises(QuotaExceeded) as excinfo:
        quota.authorize("gemini")

    # The message has to tell the owner what to do about it.
    assert "gemini" in str(excinfo.value)
    assert "LLM_DAILY_REMOTE_CALLS" in str(excinfo.value)


def test_the_refusal_happens_before_the_request_leaves(tmp_path, monkeypatch) -> None:
    """record() runs at the top of every provider method, so a refused call
    never reaches the network."""
    monkeypatch.setattr(audit, "audit_path", lambda: tmp_path / "trail.jsonl")
    monkeypatch.setattr(quota, "limit_for", lambda provider: 0 if provider == "x" else 1)
    monkeypatch.setattr(quota, "used_today", lambda provider: 99)

    with pytest.raises(QuotaExceeded):
        audit.record("gemini", "system", "user")

    # Nothing was written either — a refused call is not a call.
    assert audit.read_trail() == []


def test_remaining_counts_down(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(quota, "limit_for", lambda provider: 10)
    monkeypatch.setattr(quota, "used_today", lambda provider: 4)

    assert quota.remaining("gemini") == 6
    assert quota.remaining("gemini") != 0
