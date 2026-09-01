"""Check this machine before a run, and say how to fix what is wrong.

Every check here exists because its absence has already cost a debugging
session on this project, and in each case the failure arrived somewhere far
from its cause:

- **Postgres on the wrong port** — another project holds 5432 here, so the
  URL in `.env` points at 5433. Get it wrong and the API starts fine and every
  request 500s.
- **Migrations behind head** — the app imports, the queue runs, and the first
  query touching a new column fails inside a worker task.
- **WeasyPrint's native libraries** — Pango and cairo are not Python
  dependencies. A missing one does not raise; it *segfaults pytest partway
  through the run*, which reads as a flaky test suite.
- **Playwright's browser** — installed separately from the package, so
  `import playwright` succeeding proves nothing about whether a page can open.
- **The vault key** — absent, every ATS credential is unreadable, and the
  failure surfaces as a login that cannot be attempted.
- **Ollama down** — §14 makes the assistant local-only *by name*, so it errors
  rather than falling back to a cloud provider. Correct, and confusing if you
  do not know Ollama stopped.

Two rules this file follows without exception.

**No secret is ever printed.** Checks report that a key is present and
parseable, never its value, and never a connection string with a password in
it. §2.7 does not have an exception for diagnostics — diagnostic output is
pasted into issues and chat windows more often than anything else.

**A check that cannot run is not a check that passed.** Every result is one of
ok, fail, or skipped, and skipped is visibly not ok.
"""

from __future__ import annotations

import asyncio
import os
import shutil
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from urllib.parse import urlparse

from packages.core.config import get_settings

#: How long any single check may take. A doctor that hangs is worse than a
#: doctor that reports a timeout, because the owner cannot tell it apart from
#: a slow database.
CHECK_TIMEOUT_S = 5.0


class Health(StrEnum):
    """Health check result status."""

    OK = "ok"
    FAIL = "fail"
    #: Could not be determined — an optional feature is unconfigured, or a
    #: prerequisite check already failed. Never counted as passing.
    SKIPPED = "skipped"


@dataclass(frozen=True)
class Check:
    """A single health check result."""

    name: str
    health: Health
    detail: str
    #: The command or edit that fixes it. Empty when nothing is wrong.
    fix: str = ""
    #: False for checks whose failure does not stop the core loop.
    required: bool = True

    @property
    def ok(self) -> bool:
        """True if the check passed."""
        return self.health is Health.OK


@dataclass
class Report:
    """Collection of health check results."""

    checks: list[Check] = field(default_factory=list)

    @property
    def failures(self) -> list[Check]:
        """All checks that failed."""
        return [check for check in self.checks if check.health is Health.FAIL]

    @property
    def blocking(self) -> list[Check]:
        """Failed required checks that block operation."""
        return [check for check in self.failures if check.required]

    @property
    def healthy(self) -> bool:
        """True when nothing required is broken.

        Optional failures — no IMAP configured, Ollama not running — do not
        make this false. They are reported and they do not stop a crawl or an
        apply.
        """
        return not self.blocking

    def summary(self) -> str:
        """One-line summary of check results."""
        ok = sum(1 for check in self.checks if check.ok)
        return f"{ok}/{len(self.checks)} checks passed, {len(self.blocking)} blocking"


def _redacted(url: str) -> str:
    """A connection string with the password removed.

    Printed in diagnostics, which get pasted into issues and chat windows.
    """
    try:
        parsed = urlparse(url)
    except ValueError:
        return "<unparseable>"
    if parsed.password is None:
        return url
    host = parsed.hostname or ""
    port = f":{parsed.port}" if parsed.port else ""
    user = f"{parsed.username}@" if parsed.username else ""
    return f"{parsed.scheme}://{user}***@{host}{port}{parsed.path}"


async def check_database() -> Check:
    """Check Postgres database connectivity."""
    settings = get_settings()
    url = settings.database_url
    async_url = url.replace("postgresql://", "postgresql+asyncpg://", 1)

    try:
        from sqlalchemy import text
        from sqlalchemy.ext.asyncio import create_async_engine

        engine = create_async_engine(async_url)
        try:
            async with asyncio.timeout(CHECK_TIMEOUT_S):
                async with engine.connect() as conn:
                    await conn.execute(text("SELECT 1"))
        finally:
            await engine.dispose()
    except TimeoutError:
        return Check(
            "postgres",
            Health.FAIL,
            f"no answer within {CHECK_TIMEOUT_S:g}s from {_redacted(url)}",
            fix="make up",
        )
    except Exception as exc:  # noqa: BLE001 - every failure mode is a report
        return Check(
            "postgres",
            Health.FAIL,
            f"{type(exc).__name__} connecting to {_redacted(url)}",
            # The port is the usual culprit on this machine and the message
            # from asyncpg does not say so.
            fix="make up — and check DATABASE_URL's port matches docker-compose.override.yml",
        )
    return Check("postgres", Health.OK, _redacted(url))


async def check_migrations() -> Check:
    """Whether the schema is at Alembic's head revision."""
    try:
        from alembic.config import Config
        from alembic.script import ScriptDirectory
        from sqlalchemy import text
        from sqlalchemy.ext.asyncio import create_async_engine

        config = Config("alembic.ini")
        heads = set(ScriptDirectory.from_config(config).get_heads())

        settings = get_settings()
        engine = create_async_engine(
            settings.database_url.replace("postgresql://", "postgresql+asyncpg://", 1)
        )
        try:
            async with asyncio.timeout(CHECK_TIMEOUT_S):
                async with engine.connect() as conn:
                    rows = await conn.execute(text("SELECT version_num FROM alembic_version"))
                    applied = {row[0] for row in rows}
        finally:
            await engine.dispose()
    except Exception as exc:  # noqa: BLE001
        return Check(
            "migrations",
            Health.SKIPPED,
            f"could not be read ({type(exc).__name__})",
            fix="make migrate",
        )

    if not applied:
        return Check("migrations", Health.FAIL, "no migration has been applied", fix="make migrate")
    if applied != heads:
        return Check(
            "migrations",
            Health.FAIL,
            f"database at {', '.join(sorted(applied))}, head is {', '.join(sorted(heads))}",
            fix="make migrate",
        )
    # More than one head means two branches added migrations against the same
    # parent. `make migrate` fails outright rather than picking one.
    if len(heads) > 1:
        return Check(
            "migrations",
            Health.FAIL,
            f"{len(heads)} heads present — branches diverged",
            fix="alembic merge heads",
        )
    return Check("migrations", Health.OK, f"at head {next(iter(heads))}")


def check_weasyprint() -> Check:
    """Pango and cairo, which are not Python dependencies.

    Imported rather than merely located, because the failure is a load-time
    one: cffi resolves the shared libraries when the module is imported, and a
    missing Pango segfaults the process rather than raising ImportError.
    """
    try:
        import weasyprint  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        return Check(
            "weasyprint",
            Health.FAIL,
            f"{type(exc).__name__} on import — native libraries are probably missing",
            fix="brew install pango cairo gdk-pixbuf libffi",
        )
    return Check("weasyprint", Health.OK, "imports; native libraries resolved")


def check_np_tagger() -> Check:
    """The POS tagger the fabrication guard needs to see lowercase claims.

    Optional in the sense that the app runs without it, and not optional in
    any sense that matters: without it §9 Gate 3 matches on capitalization,
    and a rewrite claiming "machine learning" against a résumé that never says
    it is accepted with zero entities checked.

    This is the check that keeps the fallback honest. Every GuardReport
    carries which extractor produced it, but nobody reads a passing report.
    """
    from packages.tailor.chunk import available

    if not available():
        return Check(
            "noun-phrase tagger",
            Health.SKIPPED,
            "not installed — the guard falls back to capitalization and cannot "
            "see lowercase claims like 'machine learning'",
            fix="make nltk-data",
            required=False,
        )
    return Check("noun-phrase tagger", Health.OK, "installed; guard checks noun phrases")


async def check_playwright_browser() -> Check:
    """Chromium on disk. Importing playwright proves nothing about this.

    Async rather than sync deliberately: `sync_playwright()` raises if it is
    entered while an asyncio loop is running, and this whole report runs under
    one. The first version of this check used the sync API and reported
    "could not resolve Chromium" on a machine where Chromium was fine — a
    diagnostic that invents a fault is worse than no diagnostic.
    """
    try:
        from playwright.async_api import async_playwright
    except Exception as exc:  # noqa: BLE001
        return Check(
            "playwright",
            Health.FAIL,
            f"package not importable ({type(exc).__name__})",
            fix="make install",
        )

    try:
        async with async_playwright() as p:
            path = Path(p.chromium.executable_path)
    except Exception as exc:  # noqa: BLE001
        return Check(
            "playwright",
            Health.FAIL,
            f"could not resolve Chromium ({type(exc).__name__})",
            fix="playwright install chromium",
        )

    if not path.exists():
        return Check(
            "playwright",
            Health.FAIL,
            "Chromium is not installed",
            fix="playwright install chromium",
        )
    return Check("playwright", Health.OK, "Chromium present")


def check_vault_key() -> Check:
    """Whether the vault key exists and is a valid Fernet key.

    Never reports the key. "Present and parseable" is the whole answer.
    """
    settings = get_settings()
    raw = settings.vault_key or os.environ.get("VAULT_KEY")
    if not raw:
        return Check(
            "vault",
            Health.FAIL,
            "VAULT_KEY is not set — stored ATS credentials cannot be read",
            fix=(
                "python -c 'from packages.core.vault import generate_key; print(generate_key())' "
                ">> .env"
            ),
            required=False,
        )
    try:
        from cryptography.fernet import Fernet

        Fernet(raw.encode())
    except Exception:  # noqa: BLE001 - the exception text can echo the key
        return Check(
            "vault",
            Health.FAIL,
            "VAULT_KEY is set but is not a valid Fernet key",
            fix="regenerate it; any credential stored under the old key is unreadable",
            required=False,
        )
    return Check("vault", Health.OK, "key present and parseable")


def check_storage() -> Check:
    """Check that storage root is writable."""
    settings = get_settings()
    root = Path(settings.storage_root)
    try:
        root.mkdir(parents=True, exist_ok=True)
        probe = root / ".doctor-write-test"
        probe.write_bytes(b"")
        probe.unlink()
    except Exception as exc:  # noqa: BLE001
        return Check(
            "storage",
            Health.FAIL,
            f"{root} is not writable ({type(exc).__name__})",
            fix=f"chmod u+w {root}",
        )
    free_mb = shutil.disk_usage(root).free // (1024 * 1024)
    if free_mb < 500:
        return Check(
            "storage",
            Health.FAIL,
            f"{root} writable but only {free_mb}MB free",
            fix="free some disk — screenshots and PDFs land here",
        )
    return Check("storage", Health.OK, f"{root} writable, {free_mb}MB free")


async def check_ollama() -> Check:
    """Whether the local model server is up.

    Optional: only the assistant (§14) and the local provider need it. Marked
    not-required so a crawl is not reported as blocked because a chat window
    would not work.
    """
    base = os.environ.get("OLLAMA_BASE_URL") or get_settings().ollama_base_url
    try:
        import httpx

        async with asyncio.timeout(CHECK_TIMEOUT_S):
            async with httpx.AsyncClient() as client:
                response = await client.get(f"{base}/api/tags")
        response.raise_for_status()
        models = [m.get("name", "?") for m in (response.json().get("models") or [])]
    except Exception as exc:  # noqa: BLE001
        return Check(
            "ollama",
            Health.FAIL,
            f"not reachable at {base} ({type(exc).__name__})",
            fix="ollama serve",
            required=False,
        )
    if not models:
        return Check(
            "ollama",
            Health.FAIL,
            "running, but no model is pulled",
            # The configured model, not a fixed name: a hint telling you to
            # pull llama3.1 while OLLAMA_MODEL asks for something else sends
            # you to fix the wrong thing.
            fix=f"ollama pull {get_settings().ollama_model}",
            required=False,
        )
    return Check("ollama", Health.OK, f"{len(models)} model(s): {', '.join(models[:4])}")


def check_llm_provider() -> Check:
    """Which provider tailoring will use, and whether that is a surprise.

    `stub` is the shipped default and returns canned text. It is correct for
    tests and wrong for a real run, and the difference is invisible until you
    read a tailored résumé and find it says nothing.
    """
    settings = get_settings()
    provider = settings.llm_provider
    if provider == "stub":
        return Check(
            "llm",
            Health.FAIL,
            "LLM_PROVIDER=stub — tailoring will return canned text, not a rewrite",
            fix="set LLM_PROVIDER=ollama in .env",
            required=False,
        )
    return Check("llm", Health.OK, f"LLM_PROVIDER={provider}")


async def run(*, include_optional: bool = True) -> Report:
    """Run every check. Order is cheap-first so failures surface fast."""
    checks: list[Check] = [
        check_storage(),
        check_weasyprint(),
        check_np_tagger(),
        await check_playwright_browser(),
        check_vault_key(),
        check_llm_provider(),
        await check_database(),
    ]

    # Only meaningful if the database answered at all.
    if checks[-1].ok:
        checks.append(await check_migrations())
    else:
        checks.append(Check("migrations", Health.SKIPPED, "not checked — no database connection"))

    if include_optional:
        checks.append(await check_ollama())

    return Report(checks=[c for c in checks if include_optional or c.required])
