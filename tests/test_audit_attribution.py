"""Every provider call says what it was for.

§2.8 permits one upload, the tailoring call, and the audit trail exists so the
owner can check that nothing else left. It counts by task as well as by
provider for exactly that reason (`/audit/summary`, the `audit_trail` MCP
tool), and on 2026-10-14 every one of the owner's 14,458 entries said
`unspecified`: no call site passed a task. 229 of the 732 that left the
machine had no registered prompt either, so nothing said what they were.

Every call site already sends a registered prompt, so the prompt carries its
task and the trail reads it from there. An unregistered prompt is
`unregistered`, which says what is wrong, where `unspecified` said nothing.
Entries written before this keep their line in the file. They are attributed
when read, from the prompt name each already recorded, so the trail is never
rewritten.
"""

from __future__ import annotations

import json

import pytest

from packages.llm import audit, prompts
from packages.llm.prompts import REGISTRY, TAILOR_SYSTEM


def test_every_registered_prompt_names_its_task() -> None:
    for prompt in REGISTRY:
        assert prompt.task, prompt.name


@pytest.mark.parametrize(
    ("prompt", "task"),
    [
        (prompts.TAILOR_SYSTEM, "tailor_resume"),
        (prompts.TAILOR_TARGETED_SYSTEM, "tailor_resume"),
        (prompts.COVER_LETTER_SYSTEM, "write_cover_letter"),
        (prompts.CLASSIFY_SYSTEM, "classify_inbound_email"),
        (prompts.CHAT_SYSTEM, "assistant"),
    ],
)
def test_a_call_is_recorded_under_its_prompts_task(prompt, task: str) -> None:
    entry = audit.record("gemini", prompt.text, "user text", model="gemini-flash")

    assert entry.task == task


def test_an_unregistered_prompt_says_so() -> None:
    entry = audit.record("openrouter", "Write ten questions about this posting.", "user text")

    assert entry.task == "unregistered"


def test_a_task_the_caller_names_wins() -> None:
    entry = audit.record("gemini", TAILOR_SYSTEM.text, "user text", task="evaluation")

    assert entry.task == "evaluation"


def _old_line(**fields: object) -> str:
    entry = {
        "at": "2026-10-01T00:00:00+00:00",
        "provider": "gemini",
        "left_machine": True,
        "task": "unspecified",
        "system_sha256": "0" * 64,
        "user_sha256": "0" * 64,
        "system_chars": 10,
        "user_chars": 20,
        "prompt_name": None,
        "prompt_version": None,
        "model": "gemini-flash",
    }
    entry.update(fields)
    return json.dumps(entry)


def test_history_is_attributed_when_read_and_the_file_is_untouched() -> None:
    path = audit.audit_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        _old_line(prompt_name="tailor.system", prompt_version=2),
        _old_line(prompt_name="assistant.system", prompt_version=6),
        _old_line(),
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    before = path.read_bytes()

    tasks = [entry.task for entry in audit.read_trail()]

    assert tasks == ["tailor_resume", "assistant", "unregistered"]
    assert all(entry.event_version == 1 for entry in audit.read_trail())
    assert all(entry.trace_id is None and entry.task_id is None for entry in audit.read_trail())
    assert path.read_bytes() == before


async def test_the_summary_counts_uploads_by_what_they_were_for(client) -> None:
    path = audit.audit_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                _old_line(prompt_name="tailor.system", prompt_version=2),
                _old_line(prompt_name="tailor.system", prompt_version=2),
                _old_line(),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    summary = (await client.get("/audit/summary")).json()

    assert summary["by_task"] == {"tailor_resume": 2, "unregistered": 1}
