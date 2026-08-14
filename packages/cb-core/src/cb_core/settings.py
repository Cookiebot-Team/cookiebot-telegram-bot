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

    # x_image_search — Google Programmable Search (Custom Search JSON API).
    # v1 used the `google_images_search` wrapper around the same endpoint
    # (`SocialContent.py:19-20`); v2 calls it over the shared httpx client, as
    # util_youtube already does with the YouTube Data API. Two credentials,
    # both required: the API key and the search-engine id (v1's `searchEngineCX`).
    # Empty key or cx -> the feature answers "no image found", the same
    # degradation an empty youtube_api_key gives /youtube.
    google_search_api_key: str = ""
    google_search_cx: str = ""
    # 5s, matching youtube's: this is an index lookup, not an upload.
    google_search_timeout_seconds: float = 5.0
    # v1's daily caps (`Cooldowns.py:6-7`), per user and across the whole bot.
    # v1 counted them in a per-process dict, so five processes meant five times
    # the global cap in practice; v2's counter is shared, which is what the
    # number always meant.
    image_search_daily_per_user: int = 15
    image_search_daily_total: int = 180

    # x_reverse_search — SauceNAO. v1 set no timeout at all (neither the call
    # site nor `saucenao_api`); 15s rather than youtube's 5s because SauceNAO
    # is hashing an uploaded image, not answering from an index. Empty key ->
    # the search degrades to reverse_no_found, as every other failure does.
    saucenao_api_key: str = ""
    saucenao_timeout_seconds: float = 15.0

    # util_birthday — the daily every-group broadcast (v1's `manual_chat_id=None`
    # shape). On by default because v1 does it: `COOKIEBOT.py:333-339` calls
    # `birthday()` unattended from the message handler's `finally` on the first
    # message of a new UTC day, so live groups receive it today and switching it
    # off silently would be the regression, not the safe choice. The switch
    # exists for a deployment that does not want it.
    birthday_broadcast_enabled: bool = True

    # core_musicdetection — off by default, and a second switch on top of the
    # optional `music` extra not being installed either (cb_worker/music.py):
    # the feature calls Shazam's unofficial endpoint, which a deployment must
    # opt into rather than out of. v1 called it inline with no timeout at all
    # (Bot/Audio.py:7-11), on every voice note.
    music_detection_enabled: bool = False
    music_detection_timeout_seconds: float = 20.0
    #: The recogniser's key. v1 called Shazam's unpublished endpoint through an
    #: unmaintained wrapper whose successor's Rust core segfaults on this
    #: workspace's Python — see cb_worker/music.py for the whole finding.
    #: Empty means the feature is inert, exactly like youtube_api_key.
    audd_api_key: str = ""

    # x_distortion — how many /destroy jobs one worker runs at once. v1 had a
    # hard bound of exactly one per media class, enforced by spinning on a
    # module global (FEATURE-MAP D3); this is the same bound as a real
    # semaphore, defaulted to 2 because the carve now runs off the event loop
    # and no longer burns a core while it waits.
    distortion_concurrency: int = 2

    # util_postforwarder / util_postgetter — v1 hardcoded one deployment's
    # channel ids as module constants (Bot/Publisher.py:20-22). v2 is
    # multi-tenant, so they are configuration, and the publisher is inert until
    # a deployment opts in by setting them: half-running a publisher network
    # without a Mural to render into is worse than not running one.
    postmail_chat_id: int = 0
    postmail_chat_link: str = ""
    approval_chat_id: int = 0
    #: v1 suppressed the author button for one hardcoded first name
    #: (`'Mekhy' not in origin_user['first_name']`, Publisher.py:197). The
    #: default reproduces that exactly; a deployment that is not that one can
    #: change it without editing code.
    publisher_hidden_author_names: tuple[str, ...] = ("Mekhy",)
    #: How long a submitted-but-not-yet-approved post stays in the shared cache.
    #: v1's `cache_posts` dict never expired, but it also never survived a
    #: restart, so a day is strictly more generous than what v1 delivered.
    publisher_pending_ttl_seconds: int = 86400
    #: exchangerate-api v6, for `convert_prices_in_text` (Publisher.py:167-168).
    #: Unset -> price conversion is skipped and captions keep their original
    #: amounts, which is also what v1 did whenever the call failed.
    exchangerate_api_key: str = ""
    exchangerate_timeout_seconds: float = 10.0  # v1's own timeout, :168

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

    # x_webhub_login — the web console's Telegram-login token exchange.
    #: PEM of the RSA private key that signs the console's JWTs. Unset means
    #: cb-api generates one **once** and persists it (`signing_keys`), which is
    #: what fixes v1's D7: v1 regenerated a key per process on every start, so
    #: with gunicorn's two workers half of all issued tokens failed to verify
    #: against the published JWKS at any moment. Set this and the table is
    #: never read — for a deployment that would rather no private key lived in
    #: its application database.
    webhub_jwt_private_key_pem: str = ""
    #: v1's literal (`Server.py:23`). A resource server that pinned the key id
    #: keeps working.
    webhub_jwt_kid: str = "cookiebot-2025"
    #: v1's 30 minutes (`Server.py:43`).
    webhub_token_ttl_seconds: int = 1800
    #: `iss`, and the base of the discovery document. Unset reproduces v1's
    #: `request.url_root` — which behind a proxy is whatever `X-Forwarded-Host`
    #: says, so anyone who can reach the service chooses the issuer. Set it.
    webhub_issuer: str = ""
    #: How old Telegram's `auth_date` may be. **0 reproduces v1**, which never
    #: checked it at all, so a captured widget payload mints tokens forever.
    #: Non-zero is the fix — but the shipped WebHub renews by re-posting the
    #: payload it stored at first login, so any real value logs those sessions
    #: out when their token expires. See `.specs/features/x_webhub_login/spec.md`.
    webhub_auth_max_age_seconds: int = 0
    #: Browser origins allowed to call `/login`. v1 shipped `origins: "*"`.
    webhub_allowed_origins: list[str] = Field(default_factory=list)

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
