"""Typed settings. Every knob v1 hardcoded (backend base URL, timeouts, TLS verify)
is a field here — see FEATURE-MAP D2: v1 shipped `verify=False` on every backend call
because the URL and TLS behaviour were baked into four copy-pasted functions.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CB_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    env: str = "local"
    service_name: str = "cb-service"
    log_level: str = "INFO"
    log_json: bool = True

    # postgres (single-node citus)
    pg_dsn: str = "postgresql://cookiebot:cookiebot@localhost:5432/cookiebot"
    pg_pool_min: int = 4
    pg_pool_max: int = 32
    pg_command_timeout: float = 10.0
    # Converge the schema during startup instead of relying on someone having run
    # `cb.py migrate`. Replicas serialise on a Postgres advisory lock, so this is
    # safe to leave on with N processes; turn it off where a separate migration
    # job owns the schema. See cb_core/migrations.py.
    auto_migrate: bool = True
    migrate_lock_timeout: float = 300.0
    migrations_dir: str = ""  # empty -> discovered from the cb-api package

    # valkey
    redis_dsn: str = "redis://localhost:6379/0"

    # v1 data import (cb_worker.importer). Exactly one of these is used: a live
    # v1 MongoDB, or a `mongodump` directory. Both empty means no import is
    # configured, which is the normal state once the cutover is done.
    mongo_uri: str = ""
    mongo_database: str = "cookiebot"
    mongo_dump_dir: str = ""
    import_batch_size: int = 500
    # Bulk loading is not a reply path. The 10s `pg_command_timeout` that keeps a
    # handler honest kills a batch insert instead — and on a Citus catalog the
    # first array-typed statement of a connection also pays a one-off type
    # introspection measured in seconds.
    import_command_timeout: float = 300.0

    # read-through caches (group config, admin sets). L1 is per process and short:
    # it only has to absorb the burst of messages a single update storm produces,
    # because pub/sub invalidation, not expiry, is what keeps it correct.
    config_cache_l1_seconds: int = 30
    config_cache_l2_seconds: int = 900
    admin_cache_seconds: int = 600
    # Language a group falls back to before it has ever run /config or /setlang.
    default_language: str = "en"
    # Telegram file_id for v1's `Static/remove_anonymous_tutorial.mp4`, which the
    # /config denial branch sends (util_config.feature line 13). v1 re-uploaded the
    # file from local disk on every rejection; a file_id is the same video without
    # the upload. Empty means "no asset configured" and the video is simply not
    # sent — a deployment that has not uploaded it must not point users at a URL
    # that may not exist.
    anonymous_tutorial_file_id: str = ""

    # blob storage — s3:// | gs:// | file:// | memory://
    storage_uri: str = "memory://"
    storage_signed_url_ttl: int = 3600

    # llm
    anthropic_api_key: str = ""
    openai_api_key: str = ""
    openai_base_url: str = ""  # set to point at ollama / vLLM / openrouter
    llm_timeout_seconds: float = 60.0
    # Server-side refusal fallback: a declined request is re-served by a fallback
    # model inside the same call instead of surfacing as a refusal.
    llm_refusal_fallback: bool = True
    # Let the Anthropic SDK resolve ambient credentials (env var or `ant auth login`
    # profile) when no key is set in config.
    llm_allow_ambient_credentials: bool = True
    # task -> {provider, model, max_tokens, effort, thinking, temperature, timeout, system}
    llm_tasks: dict[str, dict[str, object]] = Field(default_factory=dict)
    # x_conversational_ai: per-group rate limit on top of v1's per-user streak
    # counter and the tenant spend cap (design.md R3.4). ai_chat_group_limit
    # triggers within ai_chat_window_seconds before ai_rate_limited fires.
    ai_chat_group_limit: int = 20
    ai_chat_window_seconds: int = 60

    # x_speech_to_text — duration cap checked against message.voice.duration
    # before any download (D-ST-3), so an oversized note costs neither a
    # download nor a transcription. The transcription *timeout* is deliberately
    # not a separate setting: it is DEFAULT_TASKS["transcribe"].timeout,
    # already overridable through CB_LLM_TASKS.
    transcribe_max_duration_seconds: int = 300

    # util_youtube — v1 used google-api-python-client (Bot/SocialContent.py:20);
    # v2 calls the same REST endpoint directly over the httpx client already in
    # use everywhere else, rather than adding a second HTTP client library for
    # one feature (AGENTS.md §5).
    youtube_api_key: str = ""
    youtube_timeout_seconds: float = 5.0

    # telemetry
    otlp_endpoint: str = "http://localhost:4317"
    traces_enabled: bool = True
    trace_sample_ratio: float = 1.0
    metrics_port: int = 9101
    #: Ship log records to the OTLP collector alongside stdout, so a log line
    #: is reachable from the trace it belongs to. Off by default: stdout is the
    #: contract every deployment already relies on, and a collector that is not
    #: listening must never become a reason the service logs nothing.
    otlp_logs_enabled: bool = False

    # telegram
    bot_tokens: dict[str, str] = Field(default_factory=dict)
    webhook_secret: str = ""
    webhook_base_url: str = ""
    telegram_api_base: str = ""  # empty -> api.telegram.org; set to the mock in tests
    # Self-hosted Bot API server (github.com/tdlib/telegram-bot-api). Unlocks
    # 2 GB uploads, no download-size cap, higher rate limits, and local webhooks.
    # In local mode the server writes files to disk and getFile returns an
    # absolute path instead of a download URL, so the file base differs.
    telegram_api_local: bool = False
    telegram_api_file_base: str = ""
    # Where the local Bot API server writes files, as this process sees it.
    # Set when the server's filesystem is mounted at a different path (containers).
    telegram_files_root: str = ""
    # How updates arrive: webhook (production), polling (dev / self-hosted server
    # without a public URL), websocket (reserved — see docs/site/content/docs/multi-tenant.mdx).
    telegram_ingest: str = "webhook"
    telegram_polling_timeout: int = 30
    owner_id: int = 0

    @field_validator("trace_sample_ratio")
    @classmethod
    def _ratio(cls, v: float) -> float:
        if not 0.0 <= v <= 1.0:
            raise ValueError("trace_sample_ratio must be in [0, 1]")
        return v

    @field_validator("telegram_ingest")
    @classmethod
    def _ingest(cls, v: str) -> str:
        allowed = {"webhook", "polling", "websocket"}
        if v not in allowed:
            raise ValueError(f"telegram_ingest must be one of {sorted(allowed)}")
        return v

    @property
    def is_local(self) -> bool:
        return self.env == "local"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
