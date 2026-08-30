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
    #: OpenRouter reaches many upstream models through one key. Read the same
    #: way as the keys above, so `.env` works.
    #:
    #: Setting it does not change what "auto" picks: this provider is left out
    #: of `router.QUALITY_ORDER` on purpose, so it answers only when named by
    #: `LLM_PROVIDER` or one of the per-task settings below. OpenRouter forwards
    #: to an upstream provider the audit trail cannot see, and for a cloaked
    #: `stealth/*` route that provider is undisclosed by design — §2.8 wants the
    #: one permitted upload to be auditable, so a route whose recipient cannot
    #: be named has to be chosen deliberately rather than inherited from a key
    #: being present.
    openrouter_api_key: str | None = None
    #: Overrides OpenRouterProvider.DEFAULT_MODEL. Pre-release `stealth/*`
    #: routes are withdrawn without notice, at which point every call 404s and
    #: this is the one-line fix.
    openrouter_model: str | None = None
    #: Where Ollama listens. Same reason as the keys above: `.env.example`
    #: documents this key, so reading it only from os.environ meant a `.env`
    #: pointing at another host was ignored and the caller fell back to
    #: localhost. It matters more here than for a credential — CLAUDE.md §14
    #: pins the assistant to Ollama by name and refuses to fall back to a
    #: cloud provider, so an unreadable base URL is an assistant that is down
    #: rather than one that quietly costs money.
    ollama_base_url: str = "http://localhost:11434"
    #: Which local model answers. Benchmarked against this project's own
    #: tasks rather than chosen by reputation: on the 30 labeled recruiter
    #: emails behind Gate 6 it classified 30/30 where the next best managed
    #: 28, it answers the assistant's probes from the context it was handed,
    #: and it redirects a salary question to the profile instead of advising
    #: on one — which `qwen2.5-coder` did not. It is also the most concise of
    #: the six, and CHAT_SYSTEM asks for brief.
    #:
    #: A field rather than a Python default because the value was previously
    #: written into three files and settable in none.
    ollama_model: str = "llama3.1"

    #: Which model the `ollama_cloud` provider asks for — one Ollama hosts on
    #: its own servers rather than this machine.
    #:
    #: Unset by default, and that is what keeps it opt-in. `_configured`
    #: reads this field, so naming a model here is what makes the provider
    #: reachable at all — the equivalent of pasting an API key for the others,
    #: except that Ollama needs no key once the local daemon is signed in.
    #: Which is precisely why it is kept out of `router.QUALITY_ORDER`: with
    #: no key to be absent, a provider in the quality order would become the
    #: "auto" default for every task the moment this line existed, and §2.8
    #: permits that upload without permitting it to happen unnoticed.
    #:
    #: Must carry a "cloud" marker; `OllamaCloudProvider` refuses a local tag
    #: so the audit trail cannot claim a résumé left when it did not.
    ollama_cloud_model: str | None = None

    #: Only needed when OLLAMA_BASE_URL points at ollama.com directly. The
    #: local daemon proxies cloud models once it is signed in, so the common
    #: case sends no credential at all.
    ollama_api_key: str | None = None

    #: Where to send "an application is waiting on you". Comma separated, any
    #: of: log, desktop, webhook. Unset means the log line only, which is the
    #: shipped default and sends nothing anywhere.
    #:
    #: `webhook` is the one that leaves the machine. It is opt-in by naming it,
    #: and `packages/core/notify.py` documents exactly what the payload
    #: carries — an id, a status, the company and role, and a localhost link.
    #: Never the résumé, the answers, or the posting body.
    notify_backends: str | None = None

    #: Where the `webhook` backend POSTs. The owner's own endpoint — ntfy, a
    #: Telegram bot, a Slack hook — so a phone alert costs this project no
    #: dependency and no money.
    notify_webhook_url: str | None = None

    #: Base URL a notification links back to, so the owner can act on it.
    #: Localhost because §1 says that is where the dashboard lives.
    dashboard_url: str = "http://localhost:3001"

    #: Which provider answers each §7 task that is allowed a choice.
    #:
    #: "auto" keeps `router.best_available()` — the strongest configured
    #: provider — which is what shipped. Naming one ("ollama", "gemini",
    #: "anthropic", "openrouter") pins that task, so local tailoring no longer
    #: requires deleting the API key that every other task wants kept.
    #:
    #: Only these three are settable, and that is a §2.8 boundary rather than
    #: an oversight: inbound-email classification and the assistant read
    #: recruiter mail and chat context, which §2.8 does not permit uploading
    #: and §14 pins to Ollama in code. A setting that could send them to a
    #: third party would be a way to opt out of a non-negotiable.
    llm_task_tailor: str = "auto"
    llm_task_cover_letter: str = "auto"
    llm_task_open_ended: str = "auto"
    #: On a spent quota or an unreachable remote provider, answer with the
    #: local model instead of refusing.
    #:
    #: §7 says nothing falls back to the stub, and that still holds — the stub
    #: returns canned text and putting that on a real application is the
    #: failure it was written to make visible. A local model is a real answer,
    #: and `QuotaExceeded` already tells the owner to "run a local provider",
    #: which is this, automated. The audit trail records which one answered.
    llm_fallback_local: bool = True

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
