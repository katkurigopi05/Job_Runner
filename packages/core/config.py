"""Settings loaded from the environment. See .env.example."""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = "postgresql+asyncpg://jobrunner:jobrunner@localhost:5432/jobrunner"

    #: Nothing submits without approval unless this is flipped AND the profile
    #: opts in above its match threshold. CLAUDE.md §2.3.
    auto_submit: bool = False
    min_match_score: float = 0.75

    llm_provider: str = "stub"
    #: Daily ceiling on calls to a provider that leaves the machine. 0 means
    #: unlimited. Local providers are never counted — see packages/llm/quota.py
    #: on why exceeding this refuses instead of downgrading.
    llm_daily_remote_calls: int = 200

    #: Provider credentials. Read here rather than straight from os.environ so
    #: that `.env` works — pydantic-settings loads that file into *this
    #: object*, never into the process environment, so a provider calling
    #: `os.environ.get("GEMINI_API_KEY")` cannot see a key the owner put in the
    #: documented place and is told the variable "is not set". A real
    #: environment variable still wins, which is how CI and one-off runs
    #: override without editing a file.
    gemini_api_key: str | None = None
    #: Overrides GeminiProvider.DEFAULT_MODEL. Models retire — when one
    #: does, every call 404s and this is the one-line fix.
    gemini_model: str | None = None
    #: Seconds between calls to one remote provider. Sized for a free tier;
    #: set to 0 in tests, where the sleeps are pure wall clock.
    llm_call_interval_s: float = 4.0
    anthropic_api_key: str | None = None

    #: Stable across restarts so a worker recognizes its own abandoned lease.
    #: Defaults to the hostname when unset.
    worker_id: str | None = None
    #: How long a claimed task stays owned without a heartbeat.
    lease_seconds: int = 300

    #: Managed inbox — the mailbox recruiter replies land in.
    imap_host: str | None = None
    imap_port: int = 993
    imap_username: str | None = None
    imap_password: str | None = None
    #: The bare address aliases are built from: you@gmail.com.
    inbox_alias_base: str | None = None

    vault_key: str | None = None
    #: Kept out of storage_root on purpose — see packages/core/vault.py.
    vault_root: str = "./.secrets"

    #: Floor, not a default — the crawler refuses to go below this.
    crawler_min_delay_s: int = 60

    #: The owner's search is United States only, California first. On by
    #: default because it is a standing preference rather than a per-search
    #: one — §1 calls filters the owner's input, and this is that input stated
    #: once instead of retyped on every request. `?us_only=false` overrides it
    #: for a single call, which is how you look outside without editing a file.
    search_us_only: bool = True

    #: Which embedder scores the feed — "lexical" or "sentence-transformers".
    #: Read here rather than from os.environ for the same reason the provider
    #: credentials above are: `.env` is loaded into *this object*, never into
    #: the process environment, so `packages/matching/embed.py` calling
    #: `os.environ.get` could not see a value the owner had put in the place
    #: `.env.example` documents. It silently kept scoring lexically, which is
    #: the failure this setting exists to end. A real environment variable
    #: still wins.
    embedding_backend: str = "lexical"

    storage_root: str = "./storage"

    #: Read-only token. Raises the API limit from 60/hour to 5000/hour and
    #: allows private repositories to be listed.
    github_token: str | None = None
    github_username: str | None = None
    #: Screenshots of long job descriptions can be large; cap what a single
    #: audit artifact may occupy.
    storage_max_file_mb: int = 10

    #: Refuse non-loopback callers. Only turn this off if you have put your
    #: own authentication in front of the API.
    allow_non_local: bool = False

    @property
    def async_database_url(self) -> str:
        """Normalize a psycopg-style URL to the asyncpg driver."""
        url = self.database_url
        if url.startswith("postgresql://"):
            return url.replace("postgresql://", "postgresql+asyncpg://", 1)
        return url


@lru_cache
def get_settings() -> Settings:
    return Settings()
