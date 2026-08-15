"""Playwright browser lifecycle.

One persistent context per (worker, ATS), stored under
`storage/browser/<worker_id>/<ats>/`, so cookies and any logged-in session
survive between runs. That directory is gitignored — it holds real session
state.

The profile is keyed by worker because Chromium does *not* refuse a second
launch against a directory already in use: it silently proceeds, and two
processes then share one cookie store. An exclusive lock turns that silent
corruption into a loud failure.
"""

from __future__ import annotations

import fcntl
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import structlog

from packages.core.config import get_settings

log = structlog.get_logger(__name__)

#: Long enough for a slow ATS, short enough that a hung page fails the task
#: rather than holding the lease forever.
DEFAULT_TIMEOUT_MS = 30_000

LOCK_FILENAME = ".profile.lock"


class ProfileInUseError(RuntimeError):
    """Another process already holds this browser profile.

    Two workers sharing one profile corrupt each other's cookies and session
    state. Give each worker its own WORKER_ID.
    """


def _worker_id() -> str:
    import socket

    return get_settings().worker_id or socket.gethostname()


def profile_dir(ats: str, worker_id: str | None = None) -> Path:
    root = Path(get_settings().storage_root) / "browser" / (worker_id or _worker_id()) / ats
    root.mkdir(parents=True, exist_ok=True)
    return root


@asynccontextmanager
async def _profile_lock(directory: Path) -> AsyncIterator[None]:
    """Exclusive, non-blocking lock on a profile directory.

    Released automatically if the holding process dies, so a crashed worker
    does not permanently wedge its own profile.
    """
    lock_path = directory / LOCK_FILENAME
    handle = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise ProfileInUseError(
                f"browser profile {directory} is already in use by another process. "
                "Give each worker a distinct WORKER_ID."
            ) from exc
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
    finally:
        os.close(handle)


@asynccontextmanager
async def browser_page(
    ats: str,
    *,
    headless: bool = True,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
) -> AsyncIterator[Any]:
    """Yield a page in a persistent context for `ats`.

    Deliberately plain: default user agent, no fingerprint spoofing, no
    stealth plugins. CLAUDE.md §2.5 — if a site blocks automation the
    application fails as `manual_completion_required`.
    """
    from playwright.async_api import async_playwright

    directory = profile_dir(ats)
    async with _profile_lock(directory), async_playwright() as pw:
        context = await pw.chromium.launch_persistent_context(
            user_data_dir=str(directory),
            headless=headless,
        )
        context.set_default_timeout(timeout_ms)
        page = context.pages[0] if context.pages else await context.new_page()
        try:
            yield page
        finally:
            await context.close()


@asynccontextmanager
async def ephemeral_page(*, headless: bool = True) -> AsyncIterator[Any]:
    """A throwaway page with no persisted profile. Used by tests."""
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=headless)
        context = await browser.new_context()
        context.set_default_timeout(DEFAULT_TIMEOUT_MS)
        page = await context.new_page()
        try:
            yield page
        finally:
            await context.close()
            await browser.close()
