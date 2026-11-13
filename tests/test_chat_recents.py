"""The assistant keeps its conversations, and the owner can delete them.

Asked for by the owner on 2026-11-12. A conversation was the open page's
state: "New conversation" or a reload discarded it and nothing could bring it
back. Each settled conversation is now kept in the browser's `localStorage`,
"New chat" leaves the last one in a Recents list, and one or all of them can
be deleted.

The rules for what is kept are unit-tested where they run
(`apps/web/tests/chat-recents.test.mjs`). This reads the source, as
`test_assistant_copy_edit.py` does, for the wiring a refactor could undo with
nothing else failing. The behaviour was watched in a browser with the model's
answer stubbed at the network.
"""

from __future__ import annotations

from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "apps/web/src"
ASSISTANT = (WEB / "components/assistant.tsx").read_text(encoding="utf-8")
RECENTS = (WEB / "components/assistant-recents.tsx").read_text(encoding="utf-8")
STORE = (WEB / "lib/chat-recents.ts").read_text(encoding="utf-8")


def test_a_settled_conversation_is_kept_and_one_being_written_is_not() -> None:
    """Kept on every piece of an answer, the store would be written dozens of
    times a second and hold half an answer if the page were closed."""
    assert "if (busy || turns.length === 0 || keep(chatId, turns)) return;" in ASSISTANT


def test_the_owner_is_told_when_a_conversation_could_not_be_kept() -> None:
    """A full or blocked store must not look like a kept conversation."""
    assert "could not be kept" in ASSISTANT


def test_nothing_is_deleted_on_the_first_click() -> None:
    """A kept conversation cannot be brought back, so deleting asks once."""
    assert "onClick={() => setConfirming(kept.id)}" in RECENTS
    assert 'onClick={() => setConfirming("all")}' in RECENTS
    assert "onClick={() => remove(kept.id)}" in RECENTS
    assert "onClick={() => remove(null)}" in RECENTS
    assert RECENTS.count("onDelete(") == 1, "only `remove`, after the confirmation, deletes"


def test_deleting_the_open_conversation_clears_the_screen() -> None:
    """Left on screen, its next answer would put it back in the list."""
    assert "if (id === null || id === chatId) show(newChatId(), []," in ASSISTANT


def test_the_list_cannot_change_under_an_answer_being_written() -> None:
    """The answer would arrive in whichever conversation was opened meanwhile."""
    assert "disabled={busy || fresh} onClick={onNew}" in RECENTS
    opened = RECENTS.index("onClick={() => onOpen(kept)}")
    assert "disabled={busy}" in RECENTS[opened - 160 : opened]


def test_who_answers_and_the_mail_gate_are_not_kept() -> None:
    """Both are decided again for each question (`assistant.tsx`): a kept
    conversation must not reopen with a remote model or shared mail chosen."""
    for name in ("provider", "shareMail", "share_mail"):
        assert name not in RECENTS
        assert name not in STORE.split("*/", 1)[1]


def test_kept_conversations_stay_in_the_browser() -> None:
    """No request carries one: the store is `localStorage` and nothing else."""
    assert "window.localStorage" in RECENTS
    # A route as code would write it; the files' comments name API paths.
    for sent in ("fetch(", '"/api/', "`/api/"):
        assert sent not in RECENTS
        assert sent not in STORE


def test_a_dock_and_the_chat_page_keep_their_own() -> None:
    """A conversation about one application is answered with that
    application's mail in context. Reopened elsewhere it would continue
    without it, or with another application's."""
    assert "scopedTo(all, applicationId)" in RECENTS
    assert "applicationId: applicationId ?? null," in RECENTS
