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

    #: Stable across restarts so a worker recognizes its own abandoned lease.
    #: Defaults to the hostname when unset.
    worker_id: str | None = None
    #: How long a claimed task stays owned without a heartbeat.
    lease_seconds: int = 300

    vault_key: str | None = None
    #: Kept out of storage_root on purpose — see packages/core/vault.py.
    vault_root: str = "./.secrets"

    #: Floor, not a default — the crawler refuses to go below this.
    crawler_min_delay_s: int = 60

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
