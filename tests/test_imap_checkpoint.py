"""Interruption-safe IMAP ingestion and its durable UID checkpoint."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from apps.worker import inbox_job
from packages.core.models import (
    Application,
    Candidate,
    InboundMessage,
    MailboxCheckpoint,
    Profile,
    QueueTask,
    User,
)
from packages.core.queue import ClaimedTask, enqueue
from packages.inbox.alias import alias_for
from packages.inbox.imap import ImapMailSource, StaticMailSource
from packages.inbox.route import InboundEmail


def _raw(message_id: str, subject: str = "Update") -> bytes:
    return (
        f"Message-ID: <{message_id}@mail.test>\n"
        "From: recruiter@acme.test\n"
        "To: owner@example.com\n"
        f"Subject: {subject}\n\n"
        "A message body."
    ).encode()


class FakeImap:
    def __init__(
        self,
        *,
        high_water: int,
        search_uids: bytes,
        uid_validity: int = 42,
        fail_uid: int | None = None,
    ) -> None:
        self.high_water = high_water
        self.search_uids = search_uids
        self.uid_validity = uid_validity
        self.fail_uid = fail_uid
        self.calls: list[tuple[object, ...]] = []

    def login(self, username, password):  # noqa: ANN001
        self.calls.append(("login", username, password))
        return "OK", []

    def select(self, mailbox, readonly=False):  # noqa: ANN001
        self.calls.append(("select", mailbox, readonly))
        return "OK", [b"2"]

    def response(self, name):  # noqa: ANN001
        self.calls.append(("response", name))
        if name == "UIDNEXT":
            return name, [str(self.high_water + 1).encode()]
        return name, [str(self.uid_validity).encode()]

    def uid(self, command, *args):  # noqa: ANN001
        self.calls.append(("uid", command, *args))
        if command == "search":
            return "OK", [self.search_uids]
        if command == "fetch":
            uid = str(args[0])
            if self.fail_uid == int(uid):
                return "NO", []
            return "OK", [(b"header", _raw(uid))]
        raise AssertionError(command)

    def logout(self):
        self.calls.append(("logout",))
        return "BYE", []


def test_imap_reads_with_uid_and_body_peek_without_changing_seen_state() -> None:
    connection = FakeImap(high_water=7, search_uids=b"4 7")
    source = ImapMailSource(
        "imap.test",
        "owner@example.com",
        "secret",
        connector=lambda: connection,
    )

    batch = source._fetch_sync(
        last_uid=0,
        initialized=False,
        uid_validity=0,
        limit=10,
    )

    assert [item.uid for item in batch.messages] == [4, 7]
    assert batch.uid_validity == 42
    assert batch.high_water == 7
    assert batch.drained is True
    assert ("select", "INBOX", True) in connection.calls
    assert ("uid", "search", None, "UNSEEN") in connection.calls
    fetches = [call for call in connection.calls if call[:2] == ("uid", "fetch")]
    assert fetches == [
        ("uid", "fetch", "4", "(BODY.PEEK[])"),
        ("uid", "fetch", "7", "(BODY.PEEK[])"),
    ]


def test_initialized_mailbox_resumes_after_the_committed_uid() -> None:
    connection = FakeImap(high_water=10, search_uids=b"10")
    source = ImapMailSource(
        "imap.test",
        "owner@example.com",
        "secret",
        connector=lambda: connection,
    )

    batch = source._fetch_sync(
        last_uid=7,
        initialized=True,
        uid_validity=42,
        limit=10,
    )

    assert [item.uid for item in batch.messages] == [10]
    assert ("uid", "search", None, "UID 8:10") in connection.calls


def test_password_is_not_part_of_the_checkpoint_identity() -> None:
    first = ImapMailSource("imap.test", "owner@example.com", "old-password")
    rotated = ImapMailSource("imap.test", "owner@example.com", "new-password")

    assert first.source_key == rotated.source_key
    assert "owner" not in first.source_key


def test_fetch_stops_at_a_uid_gap_instead_of_skipping_it() -> None:
    connection = FakeImap(high_water=7, search_uids=b"4 7", fail_uid=7)
    source = ImapMailSource(
        "imap.test",
        "owner@example.com",
        "secret",
        connector=lambda: connection,
    )

    batch = source._fetch_sync(
        last_uid=0,
        initialized=False,
        uid_validity=0,
        limit=10,
    )

    assert [item.uid for item in batch.messages] == [4]
    assert batch.drained is False


def _mail(application_id: uuid.UUID, number: int) -> InboundEmail:
    return InboundEmail(
        message_id=f"<message-{number}@mail.test>",
        from_addr="recruiter@acme.test",
        to_addr=alias_for("owner@example.com", application_id),
        subject="Interview invitation" if number == 2 else "Application update",
        body="Please schedule an interview." if number == 2 else "We received your application.",
        received_at=datetime.now(UTC),
    )


async def _application(session) -> Application:  # noqa: ANN001
    user = User(email=f"imap-{uuid.uuid4()}@example.com")
    session.add(user)
    await session.flush()
    candidate = Candidate(user_id=user.id, name="Owner", email=user.email)
    session.add(candidate)
    await session.flush()
    profile = Profile(candidate_id=candidate.id, label="default")
    session.add(profile)
    await session.flush()
    application = Application(
        candidate_id=candidate.id,
        profile_id=profile.id,
        url=f"https://jobs.test/{uuid.uuid4()}",
        status="queued",
    )
    session.add(application)
    await session.flush()
    return application


async def _claim(session, *, limit: int = 50) -> ClaimedTask:  # noqa: ANN001
    task = await enqueue(session, "inbox", {"limit": limit})
    await session.commit()
    return ClaimedTask(task=task, reclaimed=False, previous_owner=None)


async def test_checkpoint_prevents_replay_after_a_committed_batch(
    worker_session, monkeypatch
) -> None:
    application = await _application(worker_session)
    source = StaticMailSource(
        [_mail(application.id, 1), _mail(application.id, 2)],
        source_key="checkpoint-resume",
    )
    monkeypatch.setattr(inbox_job, "build_mail_source", lambda: source)
    claimed = await _claim(worker_session, limit=1)

    await inbox_job.handle_inbox(worker_session, claimed)
    first = await worker_session.get(MailboxCheckpoint, source.source_key)
    assert first is not None
    assert (first.last_uid, first.initialized) == (1, False)

    await inbox_job.handle_inbox(worker_session, claimed)
    await inbox_job.handle_inbox(worker_session, claimed)

    stored = (await worker_session.scalars(select(InboundMessage))).all()
    checkpoint = await worker_session.get(MailboxCheckpoint, source.source_key)
    assert len(stored) == 2
    assert checkpoint is not None
    assert (checkpoint.last_uid, checkpoint.initialized) == (2, True)


async def test_interrupted_batch_rolls_back_and_replays_without_loss_or_duplicates(
    worker_session, monkeypatch
) -> None:
    application = await _application(worker_session)
    source = StaticMailSource(
        [_mail(application.id, 1), _mail(application.id, 2)],
        source_key="checkpoint-interruption",
    )
    monkeypatch.setattr(inbox_job, "build_mail_source", lambda: source)
    claimed = await _claim(worker_session)
    task_id = claimed.task.id
    route = inbox_job.route_message
    calls = 0

    async def fail_on_second(*args, **kwargs):  # noqa: ANN002, ANN003
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("interrupted")
        return await route(*args, **kwargs)

    monkeypatch.setattr(inbox_job, "route_message", fail_on_second)
    with pytest.raises(RuntimeError, match="interrupted"):
        await inbox_job.handle_inbox(worker_session, claimed)
    await worker_session.rollback()

    assert (await worker_session.scalars(select(InboundMessage))).all() == []
    assert await worker_session.get(MailboxCheckpoint, source.source_key) is None

    monkeypatch.setattr(inbox_job, "route_message", route)
    task = await worker_session.get(QueueTask, task_id)
    assert task is not None
    claimed = ClaimedTask(task=task, reclaimed=True, previous_owner="worker")
    await inbox_job.handle_inbox(worker_session, claimed)
    await inbox_job.handle_inbox(worker_session, claimed)

    stored = (await worker_session.scalars(select(InboundMessage))).all()
    assert {message.message_id for message in stored} == {
        "<message-1@mail.test>",
        "<message-2@mail.test>",
    }
    assert len(stored) == 2


async def test_uidvalidity_change_restarts_safely_with_message_id_dedupe(
    worker_session, monkeypatch
) -> None:
    application = await _application(worker_session)
    original = _mail(application.id, 1)
    source = StaticMailSource(
        [original],
        source_key="checkpoint-uidvalidity",
        uid_validity=1,
    )
    monkeypatch.setattr(inbox_job, "build_mail_source", lambda: source)
    claimed = await _claim(worker_session)
    await inbox_job.handle_inbox(worker_session, claimed)

    rebuilt = StaticMailSource(
        [original, _mail(application.id, 2)],
        source_key=source.source_key,
        uid_validity=2,
    )
    monkeypatch.setattr(inbox_job, "build_mail_source", lambda: rebuilt)
    await inbox_job.handle_inbox(worker_session, claimed)

    stored = (await worker_session.scalars(select(InboundMessage))).all()
    checkpoint = await worker_session.get(MailboxCheckpoint, source.source_key)
    assert len(stored) == 2
    assert checkpoint is not None
    assert (checkpoint.uid_validity, checkpoint.last_uid) == (2, 2)
