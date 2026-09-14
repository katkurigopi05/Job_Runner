"""The health check, and the two ways a health check betrays you.

**It invents a fault.** The first version of the Playwright check used
`sync_playwright()`, which raises when entered inside a running asyncio loop —
so it reported "could not resolve Chromium" on a machine where Chromium was
fine. A diagnostic that manufactures a problem is worse than no diagnostic,
because the owner goes and fixes something that was never broken.

**It leaks a secret.** Diagnostic output gets pasted into issues and chat
windows more readily than anything else in a project. §2.7 has no exception
for it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from packages.core.config import get_settings
from packages.core.doctor import (
    Check,
    Health,
    Report,
    _redacted,
    check_database,
    check_inbox,
    check_vault_key,
    run,
)

# --------------------------------------------------------------------------
# Secrets
# --------------------------------------------------------------------------


def test_a_database_password_never_reaches_the_output() -> None:
    url = "postgresql://jobrunner:hunter2@localhost:5433/jobrunner"

    redacted = _redacted(url)

    assert "hunter2" not in redacted
    # The parts that are actually useful for diagnosis survive.
    assert "localhost:5433" in redacted
    assert "jobrunner" in redacted


def test_a_url_without_a_password_is_left_alone() -> None:
    url = "postgresql://localhost:5433/jobrunner"

    assert _redacted(url) == url


def test_an_unparseable_url_does_not_fall_back_to_printing_it() -> None:
    """The fallback must not be "show the raw string".

    That is the exact case where a password would slip through — the parser
    failed, so nothing has been redacted.
    """
    assert "://" not in _redacted("postgresql://user:pw@[bad")


def test_a_bad_port_is_redacted_rather_than_raising() -> None:
    """`urlparse` is lazy — `.port` does the work and raises, not the parse.

    With only the parse guarded this raised from inside `check_database`'s own
    `except` block, so `make doctor` ended in a traceback rather than a report.
    The caller's fix string is "check DATABASE_URL's port", which makes a bad
    port the one input this must survive.
    """
    for url in (
        "postgresql://user:pw@localhost:notaport/db",
        "postgresql://user:pw@localhost:99999/db",
    ):
        assert _redacted(url) == "<unparseable>", url


@pytest.mark.asyncio
async def test_a_bad_port_is_reported_rather_than_crashing_the_check(monkeypatch) -> None:
    """The whole point: a diagnostic that dies is worse than a failing one."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:hunter2@localhost:notaport/db")
    get_settings.cache_clear()
    try:
        check = await check_database()
    finally:
        get_settings.cache_clear()

    assert check.health is Health.FAIL
    assert "hunter2" not in str(check)


def test_the_redacted_string_is_still_a_url() -> None:
    """`{username}@` then `***@` produced `user@***@host` — two separators."""
    assert (
        _redacted("postgresql://jobrunner:hunter2@localhost:5433/jobrunner")
        == "postgresql://jobrunner:***@localhost:5433/jobrunner"
    )


def test_an_invalid_vault_key_is_reported_without_echoing_it(monkeypatch) -> None:
    """The real defect this caught: a 91-character VAULT_KEY.

    Fernet needs 32 bytes, base64 to 44 characters. The value in `.env` was
    neither, so every credential write would have raised — and nothing had
    ever noticed, because nothing had ever tried to store one.

    `Fernet(...)` raises with the key in the message, so the check must not
    pass the exception text through.
    """
    monkeypatch.setenv("VAULT_KEY", "x" * 91)
    from packages.core.config import get_settings

    get_settings.cache_clear()
    try:
        check = check_vault_key()
    finally:
        get_settings.cache_clear()

    assert check.health is Health.FAIL
    assert "x" * 20 not in check.detail
    assert not check.required


# --------------------------------------------------------------------------
# Verdicts
# --------------------------------------------------------------------------


def test_an_optional_failure_does_not_block() -> None:
    """Ollama being down must not report a crawl as impossible."""
    report = Report(
        checks=[
            Check("postgres", Health.OK, ""),
            Check("ollama", Health.FAIL, "not running", required=False),
        ]
    )

    assert report.healthy
    assert report.failures
    assert report.blocking == []


def test_a_required_failure_blocks() -> None:
    report = Report(checks=[Check("postgres", Health.FAIL, "refused")])

    assert not report.healthy
    assert len(report.blocking) == 1


def test_skipped_is_not_counted_as_passing() -> None:
    """A check that could not run is not a check that passed.

    The tempting shortcut is to treat "skipped" as "fine" so the summary reads
    clean. Then a machine with no database reports every downstream check as
    healthy, which is the opposite of what the owner needs to know.
    """
    report = Report(
        checks=[
            Check("postgres", Health.FAIL, "refused"),
            Check("migrations", Health.SKIPPED, "not checked"),
        ]
    )

    assert "0/2 checks passed" in report.summary()
    assert not report.healthy


@pytest.mark.asyncio
async def test_the_real_report_runs_without_raising() -> None:
    """Whatever is broken on this machine, the doctor itself must not break.

    A health check that throws is the least useful thing in the repository.
    """
    report = await run()

    assert report.checks
    assert all(isinstance(check.health, Health) for check in report.checks)


# --------------------------------------------------------------------------
# The inbox
# --------------------------------------------------------------------------
#
# Phase 6 routes recruiter replies back onto applications, and the whole chain
# is inert without a mailbox. `make doctor` said nothing about that at all, so
# "IMAP host, username and password incomplete" was something a diagnostic
# sweep had to go and find. A check that is silent about a feature that cannot
# run is the same failure as a crawl that reports zero postings from a dead
# board: indistinguishable from working.


@pytest.fixture
def inbox_env(monkeypatch):
    """Set IMAP_* for one check, and put the settings cache back afterwards.

    `get_settings` is cached, so clearing it only on the way in leaves the
    next test reading this test's mailbox. That is how a green suite starts
    reporting a configured inbox to a module that has none.
    """

    def _set(**values: str) -> None:
        for name, value in values.items():
            monkeypatch.setenv(name, value)
        get_settings.cache_clear()

    try:
        yield _set
    finally:
        get_settings.cache_clear()


def test_an_unconfigured_inbox_is_reported_not_silent(inbox_env) -> None:
    inbox_env(IMAP_HOST="", IMAP_USERNAME="", IMAP_PASSWORD="")

    check = check_inbox()

    assert not check.ok
    # Optional: an owner who does not use the tracker is not broken.
    assert not check.required
    assert check.fix


def test_a_half_configured_inbox_names_what_is_missing(inbox_env) -> None:
    """The worst state of the three: it looks set up and cannot connect."""
    inbox_env(IMAP_HOST="imap.gmail.com", IMAP_USERNAME="owner@gmail.com", IMAP_PASSWORD="")

    check = check_inbox()

    assert not check.ok
    assert "IMAP_PASSWORD" in check.detail
    assert "IMAP_HOST" not in check.detail


def test_the_inbox_password_never_reaches_the_output(inbox_env) -> None:
    """§2.7 — diagnostic output is pasted into chat windows more than anything."""
    inbox_env(
        IMAP_HOST="imap.gmail.com",
        IMAP_USERNAME="owner@gmail.com",
        IMAP_PASSWORD=APP_PASSWORD,
        INBOX_ALIAS_BASE="owner@gmail.com",
    )

    check = check_inbox()

    assert check.ok
    assert APP_PASSWORD not in f"{check.detail} {check.fix}"


def test_a_configured_inbox_is_not_claimed_to_be_reachable(inbox_env) -> None:
    """Settings being present is not the same as a mailbox answering.

    The check does no network: `make doctor` is a precondition in scripts and
    must not hang on an unreachable host. Saying more than it knows is how a
    green check stops being worth reading.
    """
    inbox_env(
        IMAP_HOST="imap.gmail.com",
        IMAP_USERNAME="owner@gmail.com",
        IMAP_PASSWORD=APP_PASSWORD,
        INBOX_ALIAS_BASE="owner@gmail.com",
    )

    check = check_inbox()

    assert "configured" in check.detail.lower()
    assert "connected" not in check.detail.lower()


async def test_the_report_includes_the_inbox() -> None:
    report = await run()

    assert "inbox" in {c.name for c in report.checks}


# --------------------------------------------------------------------------
# Vault diagnostics that say what to do, and what not to
# --------------------------------------------------------------------------

#: A shape-valid Gmail app password. Not a real credential.
APP_PASSWORD = "abcdefghijklmnop"


@pytest.fixture
def vault_env(monkeypatch, tmp_path):
    """An isolated vault root and key, with the settings cache restored after."""
    root = tmp_path / "vault"
    root.mkdir()

    def _set(key: str | None) -> Path:
        monkeypatch.setenv("VAULT_ROOT", str(root))
        if key is None:
            monkeypatch.setenv("VAULT_KEY", "")
        else:
            monkeypatch.setenv("VAULT_KEY", key)
        get_settings.cache_clear()
        return root

    try:
        yield _set
    finally:
        get_settings.cache_clear()


def _store(root: Path, key: str, name: str = "greenhouse") -> None:
    from cryptography.fernet import Fernet

    (root / f"{name}.enc").write_bytes(Fernet(key.encode()).encrypt(b'{"password": "x"}'))


def test_key_shape_problems_name_the_cause_and_repeat_none_of_it() -> None:
    from cryptography.fernet import Fernet

    from packages.core.doctor import vault_key_problems

    key = Fernet.generate_key().decode()
    problems = vault_key_problems(f"{key}  # my vault key")

    joined = " ".join(problems)
    assert "'#'" in joined
    assert "44" in joined
    assert key[:8] not in joined


def test_a_quoted_key_is_named_as_quoted() -> None:
    from cryptography.fernet import Fernet

    from packages.core.doctor import vault_key_problems

    key = Fernet.generate_key().decode()
    assert any("quotes" in problem for problem in vault_key_problems(f'"{key}"'))


def test_an_invalid_key_with_stored_credentials_forbids_a_new_key(vault_env, tmp_path) -> None:
    """Replacing the key strands every credential. The fix must say so first."""
    from cryptography.fernet import Fernet

    root = vault_env("not-a-key")
    _store(root, Fernet.generate_key().decode())

    check = check_vault_key(env_file=tmp_path / "absent.env")

    assert check.health is Health.FAIL
    assert "do NOT generate a new key" in check.fix
    assert "1 stored credential" in check.fix


def test_an_invalid_key_with_an_empty_vault_permits_a_new_one(vault_env, tmp_path) -> None:
    vault_env("not-a-key")

    check = check_vault_key(env_file=tmp_path / "absent.env")

    assert "make vault-key" in check.fix
    assert "loses nothing" in check.fix


def test_a_valid_key_that_reads_nothing_is_the_wrong_key(vault_env, tmp_path) -> None:
    from cryptography.fernet import Fernet

    other = Fernet.generate_key().decode()
    root = vault_env(Fernet.generate_key().decode())
    _store(root, other)

    check = check_vault_key(env_file=tmp_path / "absent.env")

    assert check.health is Health.FAIL
    assert "not the key they were written with" in check.detail
    assert "do not" in check.fix.lower()


def test_a_valid_key_that_reads_the_vault_passes(vault_env, tmp_path) -> None:
    from cryptography.fernet import Fernet

    key = Fernet.generate_key().decode()
    root = vault_env(key)
    _store(root, key)

    check = check_vault_key(env_file=tmp_path / "absent.env")

    assert check.ok
    assert "reads 1 stored credential" in check.detail
    assert key not in check.detail


def test_duplicate_key_lines_in_env_are_reported(vault_env, tmp_path) -> None:
    from cryptography.fernet import Fernet

    vault_env(Fernet.generate_key().decode())
    env_file = tmp_path / ".env"
    env_file.write_text("VAULT_KEY=one\nOTHER=1\nexport VAULT_KEY=two\n")

    check = check_vault_key(env_file=env_file)

    assert "2 VAULT_KEY lines" in check.detail


def test_a_gmail_account_password_is_refused_without_echoing_it(inbox_env) -> None:
    inbox_env(
        IMAP_HOST="imap.gmail.com",
        IMAP_USERNAME="owner@gmail.com",
        IMAP_PASSWORD="hunter2",
        INBOX_ALIAS_BASE="owner@gmail.com",
    )

    check = check_inbox()

    assert not check.ok
    assert "app password" in check.detail
    assert "hunter2" not in f"{check.detail} {check.fix}"


def test_an_app_password_with_spaces_is_accepted(inbox_env) -> None:
    """Google displays it in four groups; people paste it that way."""
    inbox_env(
        IMAP_HOST="imap.gmail.com",
        IMAP_USERNAME="owner@gmail.com",
        IMAP_PASSWORD="abcd efgh ijkl mnop",
        INBOX_ALIAS_BASE="owner@gmail.com",
    )

    assert check_inbox().ok


def test_a_missing_alias_base_is_named_as_the_reason_replies_never_conclude(inbox_env) -> None:
    inbox_env(
        IMAP_HOST="imap.gmail.com",
        IMAP_USERNAME="owner@gmail.com",
        IMAP_PASSWORD=APP_PASSWORD,
        INBOX_ALIAS_BASE="",
    )

    check = check_inbox()

    assert not check.ok
    assert not check.required
    assert "INBOX_ALIAS_BASE" in check.detail


async def test_the_report_includes_the_registry_when_the_database_answers() -> None:
    report = await run()

    names = {c.name for c in report.checks}
    database = next(c for c in report.checks if c.name == "postgres")
    assert ("registry" in names) is database.ok
