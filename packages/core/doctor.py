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
import re
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

    Every read happens inside the `try`, not just `urlparse`. The parse is lazy
    — `.port`, `.hostname` and `.password` are properties that do the work, and
    `.port` raises on anything that is not an integer in range. With only the
    parse guarded, `DATABASE_URL=...@localhost:notaport/...` made this raise
    from inside the caller's own `except` block, so `make doctor` ended in a
    traceback instead of a report. That is the one input where it is least
    affordable: the caller's `fix` string is "check DATABASE_URL's port", so
    the check that exists to name a bad port was the thing a bad port broke.
    """
    try:
        parsed = urlparse(url)
        if parsed.password is None:
            return url
        scheme = parsed.scheme
        host = parsed.hostname or ""
        port = f":{parsed.port}" if parsed.port else ""
        user = f"{parsed.username}:" if parsed.username else ""
    except ValueError:
        # Not "show the raw string" — the parser failed, so nothing has been
        # redacted and the password is still in there.
        return "<unparseable>"
    return f"{scheme}://{user}***@{host}{port}{parsed.path}"


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


#: A Fernet key is 32 bytes, URL-safe base64 encoded: always 44 characters.
FERNET_KEY_LENGTH = 44

#: Cap on how many stored credentials a check tries to decrypt. Enough to
#: tell "wrong key" from "one corrupt file" without reading a large vault.
_DECRYPT_SAMPLE = 50


def vault_key_problems(raw: str) -> list[str]:
    """What is wrong with a VAULT_KEY's *shape*, without repeating any of it.

    The live defect was a 91-character value. "Not a valid Fernet key" is
    true and useless: the likely causes — a pasted comment, quotes, two keys
    run together — each have a different fix, and none of them can be
    diagnosed from the message. Every string returned here describes the
    value; none contains a character of it.
    """
    problems: list[str] = []
    if raw != raw.strip():
        problems.append("has leading or trailing whitespace")
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        problems.append("is wrapped in quotes — .env values are read literally")
        value = value[1:-1]
    if "#" in value:
        problems.append(
            "contains '#' — an inline comment on the VAULT_KEY line is read as part of it"
        )
    if any(ch.isspace() for ch in value):
        problems.append("contains whitespace")
    if len(value) != FERNET_KEY_LENGTH:
        problems.append(f"is {len(value)} characters; a Fernet key is exactly {FERNET_KEY_LENGTH}")
    if re.search(r"[^A-Za-z0-9_\-=]", value.replace("#", "").replace(" ", "")):
        problems.append("contains characters outside URL-safe base64")
    return problems


def stored_credentials(root: str | Path | None = None) -> list[Path]:
    """Encrypted credential files in the vault root.

    Their existence is what makes a key precious: with none stored, a new key
    loses nothing; with any, it strands them.
    """
    location = Path(root or get_settings().vault_root)
    if not location.is_dir():
        return []
    return sorted(location.glob("*.enc"))


def _env_key_lines(env_file: Path) -> int:
    if not env_file.is_file():
        return 0
    pattern = re.compile(r"^\s*(?:export\s+)?VAULT_KEY\s*=")
    return sum(1 for line in env_file.read_text().splitlines() if pattern.match(line))


def _replacement_advice(stored: int) -> str:
    """The one decision that cannot be undone, stated before anyone makes it."""
    if stored:
        return (
            f"do NOT generate a new key: {stored} stored credential(s) in the vault were "
            "encrypted with the original, and no other key can read them. Restore the original "
            "VAULT_KEY from wherever you kept it (password manager, backup of .env)."
        )
    return (
        "no credentials are stored yet, so a new key loses nothing: run `make vault-key` and "
        "put its output on the existing VAULT_KEY= line in .env — one line, no quotes, no comment"
    )


def check_vault_key(*, env_file: Path | None = None) -> Check:
    """Whether the vault key exists, parses, and reads what is stored under it.

    Never reports the key, or any part of it. Nothing here writes a key either:
    replacing one strands every credential encrypted under the old one, and
    whether that loses anything is a fact about the vault this check can read
    and the owner must decide on.
    """
    settings = get_settings()
    raw = settings.vault_key or os.environ.get("VAULT_KEY")
    stored = stored_credentials(settings.vault_root)
    lines = _env_key_lines(env_file or Path(".env"))
    duplicate = (
        f" .env has {lines} VAULT_KEY lines; only one is read — delete the others."
        if lines > 1
        else ""
    )

    if not raw:
        return Check(
            "vault",
            Health.FAIL,
            "VAULT_KEY is not set — ATS credentials cannot be stored or read" + duplicate,
            fix=_replacement_advice(len(stored)),
            required=False,
        )
    try:
        from cryptography.fernet import Fernet, InvalidToken

        fernet = Fernet(raw.encode())
    except Exception:  # noqa: BLE001 - the exception text can echo the key
        shape = vault_key_problems(raw)
        detail = "VAULT_KEY is set but is not a valid Fernet key"
        if shape:
            detail += ": it " + "; it ".join(shape)
        return Check(
            "vault",
            Health.FAIL,
            detail + "." + duplicate,
            fix=_replacement_advice(len(stored)),
            required=False,
        )

    unreadable = 0
    sample = stored[:_DECRYPT_SAMPLE]
    for path in sample:
        try:
            fernet.decrypt(path.read_bytes())
        except (InvalidToken, OSError):
            unreadable += 1
    if sample and unreadable == len(sample):
        return Check(
            "vault",
            Health.FAIL,
            f"VAULT_KEY is valid but decrypts none of {len(sample)} stored credential(s) — "
            "it is not the key they were written with." + duplicate,
            fix="restore the original VAULT_KEY; do not delete the stored files, and do not "
            "generate a new key",
            required=False,
        )
    if unreadable:
        return Check(
            "vault",
            Health.FAIL,
            f"{unreadable} of {len(sample)} stored credential(s) cannot be decrypted "
            "(corrupt, or written under another key)." + duplicate,
            fix="re-enter those ATS credentials; the rest are readable",
            required=False,
        )
    detail = "key present and parseable"
    if stored:
        detail += f"; reads {len(sample)} stored credential(s)"
    return Check("vault", Health.OK, detail + duplicate)


#: The three settings an IMAP poll cannot run without. The port has a default
#: that is right for every provider worth naming, so it is not one of them.
_IMAP_REQUIRED = ("IMAP_HOST", "IMAP_USERNAME", "IMAP_PASSWORD")

#: Gmail over IMAP takes a 16-character app password, never the account's.
_GMAIL_APP_PASSWORD_LENGTH = 16


def check_inbox() -> Check:
    """Whether the recruiter-reply tracker has a mailbox to poll.

    Phase 6 routes replies back onto applications and the whole chain is inert
    without one, but nothing said so: an unconfigured inbox looked exactly like
    a quiet one. That is the dead-board failure again — zero messages reads
    identically to nothing new since the last poll.

    Does no network. `make doctor` is used as a precondition in scripts, and a
    check that dials an unreachable host hangs the thing it was meant to
    protect. So this answers "is it configured", and says only that.

    Never reports the password, or its length when it is wrong. §2.7 has no
    exception for diagnostic output, which is pasted into chat windows more
    readily than anything else here.
    """
    settings = get_settings()
    values = {
        "IMAP_HOST": settings.imap_host,
        "IMAP_USERNAME": settings.imap_username,
        "IMAP_PASSWORD": settings.imap_password,
    }
    missing = [name for name in _IMAP_REQUIRED if not values[name]]
    gmail_steps = (
        " For Gmail: enable 2-Step Verification, create an app password at "
        "https://myaccount.google.com/apppasswords, and use IMAP_HOST=imap.gmail.com."
    )

    if len(missing) == len(_IMAP_REQUIRED):
        return Check(
            "inbox",
            Health.FAIL,
            "no mailbox configured — recruiter replies are not ingested",
            fix="set IMAP_HOST, IMAP_USERNAME and IMAP_PASSWORD in .env, then restart the "
            "worker." + gmail_steps,
            required=False,
        )

    if missing:
        # The worst of the three states: it looks set up and cannot connect.
        return Check(
            "inbox",
            Health.FAIL,
            f"partly configured — {', '.join(missing)} not set",
            fix=f"set {', '.join(missing)} in .env, then restart the worker.",
            required=False,
        )

    if not 0 < settings.imap_port < 65536:
        return Check(
            "inbox",
            Health.FAIL,
            "IMAP_PORT is not a valid port",
            fix="set IMAP_PORT=993 (IMAP over TLS) unless your provider says otherwise",
            required=False,
        )

    host = (settings.imap_host or "").lower()
    password = (settings.imap_password or "").replace(" ", "")
    if "gmail" in host and len(password) != _GMAIL_APP_PASSWORD_LENGTH:
        return Check(
            "inbox",
            Health.FAIL,
            "IMAP_PASSWORD does not look like a Gmail app password — Gmail refuses the account "
            "password over IMAP",
            fix="create an app password at https://myaccount.google.com/apppasswords and put "
            "it in IMAP_PASSWORD (spaces are ignored)",
            required=False,
        )

    detail = f"configured for {settings.imap_username} at {settings.imap_host}:{settings.imap_port}"
    if not settings.inbox_alias_base:
        return Check(
            "inbox",
            Health.FAIL,
            detail + "; INBOX_ALIAS_BASE is not set, so replies can attach to applications "
            "but never conclude one",
            fix="set INBOX_ALIAS_BASE to the mailbox address (e.g. you@gmail.com) and set the "
            "candidate's email mode to managed",
            required=False,
        )
    return Check("inbox", Health.OK, detail)


async def check_registry() -> Check:
    """Whether the `companies` table agrees with `seeds/companies.yaml`.

    Reads both and writes neither — `registry_health` says why the repair
    stays a deliberate act. Optional: a registry out of step stops the crawl
    finding boards, not the apply loop.
    """
    from packages.core.db import get_sessionmaker
    from packages.crawler.extract import SeedFileError, load_retired, load_seed
    from packages.crawler.registry_health import SYNC_FIX, RegistryHealth, diagnose_registry

    try:
        seeds = load_seed()
        retired = load_retired()
    except SeedFileError as exc:
        return Check(
            "registry",
            Health.FAIL,
            f"seeds/companies.yaml cannot be read: {exc}",
            fix="fix the file; `git diff seeds/companies.yaml` shows what changed",
            required=False,
        )

    async def _diagnose() -> RegistryHealth:
        async with get_sessionmaker()() as session:
            return await diagnose_registry(session, seeds, retired)

    try:
        health = await asyncio.wait_for(_diagnose(), timeout=CHECK_TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001 - a diagnostic reports, never raises
        return Check(
            "registry",
            Health.SKIPPED,
            f"could not compare the registry with the database ({type(exc).__name__})",
            required=False,
        )
    if health.ok:
        return Check("registry", Health.OK, health.summary())
    return Check("registry", Health.FAIL, health.summary(), fix=SYNC_FIX, required=False)


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
        check_inbox(),
        check_llm_provider(),
        await check_database(),
    ]

    # Only meaningful if the database answered at all.
    if checks[-1].ok:
        checks.append(await check_migrations())
        checks.append(await check_registry())
    else:
        checks.append(Check("migrations", Health.SKIPPED, "not checked — no database connection"))

    if include_optional:
        checks.append(await check_ollama())

    return Report(checks=[c for c in checks if include_optional or c.required])
