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

import pytest

from packages.core.config import get_settings
from packages.core.doctor import (
    Check,
    Health,
    Report,
    _redacted,
    check_database,
    check_vault_key,
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
    from packages.core.doctor import run

    report = await run()

    assert report.checks
    assert all(isinstance(check.health, Health) for check in report.checks)
