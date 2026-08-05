"""The migration spec: one row per feature, single source of truth.

Spec-driven means the *spec* is the artifact everything else is derived from —
the status report, the milestone plan, and the tests that assert the two agree.
Nothing here is prose: `scripts/status.py` renders it, and `scripts/cb.py status`
cross-checks it against the QA repo, the v2 feature files and a real test run, so
a row that claims `DONE` without a passing scenario is reported as a lie.

Sources of truth this is transcribed from:
  ../Cookiebot-QA/features/                  intended behaviour
  ../COOKIEBOT-Telegram-Group-Bot/Bot/       observable v1 behaviour
  ../COOKIEBOT-backend/src/main/java/        stored shapes
  docs/site/content/docs/feature-map.mdx                        the mapping, with file:line refs
"""

from __future__ import annotations

from enum import StrEnum

import msgspec


class Status(StrEnum):
    DONE = "done"  # implemented, scenarios green
    PARTIAL = "partial"  # implemented, spec incomplete or scenarios missing
    PLANNED = "planned"  # scheduled in a milestone, not started
    BLOCKED = "blocked"  # cannot start until something else lands


class Layer(StrEnum):
    GATEWAY = "gateway"  # a Telegram-facing handler
    CORE = "core"  # shared runtime capability
    WORKER = "worker"  # background job
    API = "api"  # HTTP surface
    PLATFORM = "platform"  # infrastructure, not user-visible


class Feature(msgspec.Struct, frozen=True):
    id: str  # matches the QA feature file stem where one exists
    area: str  # core | fun | util | platform
    title: str
    milestone: str  # M0..M4
    status: Status
    layer: Layer = Layer.GATEWAY
    v1_source: str = ""  # file:line in the v1 repo
    triggers: tuple[str, ...] = ()  # every alias that must keep working
    notes: str = ""


# fmt: off
FEATURES: tuple[Feature, ...] = (
    # ---------------------------------------------------------------- platform
    Feature("platform_skeleton", "platform", "Workspace, 3 services, CI", "M0", Status.DONE,
            Layer.PLATFORM, notes="uv workspace, granian, ruff, pytest"),
    Feature("platform_observability", "platform", "OTel traces, Prometheus, structlog", "M0", Status.DONE,
            Layer.PLATFORM, notes="trace_id stored on message_events"),
    Feature("platform_citus", "platform", "Citus schema, colocation, UUIDv7", "M0", Status.DONE,
            Layer.PLATFORM, notes="topology asserted by qa/integration/test_citus_topology.py"),
    Feature("platform_cython", "platform", "Compiled hot path with benchmark gate", "M0", Status.DONE,
            Layer.PLATFORM, notes="cooldowns 1.9x compiled; dedupe/textmatch/captcha ship pure per the gate"),
    Feature("platform_qa_harness", "platform", "Mock Telegram + executable Gherkin", "M0", Status.DONE,
            Layer.PLATFORM, notes="v1 QA repo had 63 scenarios, 0 executable"),
    Feature("platform_storage", "platform", "Blob storage: GCS, S3, local, memory", "M0", Status.DONE,
            Layer.CORE, notes="obstore; content-addressed; per-group reference rows"),
    Feature("platform_llm", "platform", "Multi-provider LLM with configurable models", "M0", Status.DONE,
            Layer.CORE, notes="anthropic + openai-compatible; per-task routing; cost metering"),
    Feature("platform_selfhosted_api", "platform", "Self-hosted Telegram Bot API server", "M0", Status.DONE,
            Layer.GATEWAY, notes="local mode + polling ingest; websocket reserved"),
    Feature("platform_tenancy", "platform", "Multi-tenant registry and schema", "M1", Status.PARTIAL,
            Layer.CORE, notes="tenants table + registry landed; handler packs not wired"),
    # The three M1 prerequisites. Almost every M1 handler needs all three, so they
    # are built once rather than three-quarters of each inside three ports.
    Feature("platform_locales", "platform", "String catalog ported from v1 locales", "M1",
            Status.DONE, Layer.CORE, "Bot/Static/locales/{eng,pt,es}", (),
            "strings must match v1 verbatim - port the files, do not rewrite the copy"),
    Feature("platform_group_config", "platform", "Group config repository with shared cache", "M1",
            Status.DONE, Layer.CORE, "Configurations.py:9-12", (),
            "table exists, nothing reads it; replaces v1's manual /reload (D6)"),
    Feature("platform_admin_resolution", "platform", "Admin resolution and cache", "M1",
            Status.DONE, Layer.CORE, "Configurations.py:104-114", (),
            "group_admins is never populated; must handle anonymous admins"),
    Feature("platform_migration_etl", "platform", "Mongo -> Citus backfill", "M4", Status.PARTIAL,
            Layer.WORKER, "COOKIEBOT-backend/core/domains/*.java", (),
            "configs/rules/welcomes/users/blacklist/groups import, idempotent; randomdatabase needs a Telegram-download backfill"),
    Feature("util_isalive", "util", "Health check from chat", "M0", Status.DONE,
            Layer.GATEWAY, "Miscellaneous.py:65-69", ("/isalive", "/tavivo")),
    Feature("core_listcommand", "core", "List available commands", "M1", Status.DONE,
            Layer.GATEWAY, "Miscellaneous.py:124-127", ("/commands", "/comandos")),
    Feature("core_privacy", "core", "Privacy policy", "M1", Status.DONE,
            Layer.GATEWAY, "Miscellaneous.py:60-63", ("/privacy", "/privacidade", "/privacidad")),
    Feature("core_rules", "core", "Group rules and /newrules", "M1", Status.DONE,
            Layer.GATEWAY, "GroupShield.py:49-63", ("/rules", "/regras", "/newrules", "/novasregras")),
    Feature("core_welcome", "core", "Welcome message and /newwelcome", "M1", Status.DONE,
            Layer.GATEWAY, "GroupShield.py:140-171", ("/newwelcome", "/novobemvindo")),
    Feature("core_groupguardian", "core", "Join captcha", "M1", Status.DONE,
            Layer.GATEWAY, "GroupShield.py:231-265", (),
            "captcha challenge module already compiled; needs handler + DB wiring"),
    Feature("core_stickerspam", "core", "Anti sticker spam", "M1", Status.DONE,
            Layer.GATEWAY, "Cooldowns.py:12-22", (),
            "SlidingWindow compiled; needs shared counter via cache.incr_window"),
    Feature("core_mediarestrict", "core", "Media restriction for new members", "M1", Status.DONE,
            Layer.GATEWAY, "COOKIEBOT.py:167-172"),
    Feature("util_config", "util", "Admin configuration menu", "M1", Status.DONE,
            Layer.GATEWAY, "Configurations.py:139-167", ("/config", "/configure", "/configurar"),
            "QA says /config, v1 ships /configurar - both must resolve"),
    Feature("util_doomlist", "util", "Block listed users from joining", "M1", Status.DONE,
            Layer.GATEWAY, "GroupShield.py:172-229", (),
            "external cas.chat + burrbot need timeout and circuit breaker"),
    Feature("core_setlang", "core", "Language selection", "M1", Status.DONE,
            Layer.GATEWAY, "Configurations.py:242-251", (),
            "QA describes a web settings page; v1 only has the in-chat menu"),
    Feature("core_botskins", "core", "Per-event bot skins", "M1", Status.PARTIAL,
            Layer.GATEWAY, "universal_funcs.py:39-52", (),
            "one process serves every skin; per-event asset packs pending"),

    # --------------------------------------------------------------------- fun
    Feature("fun_dice", "fun", "Roll an n-sided die", "M2", Status.DONE,
            Layer.GATEWAY, "Miscellaneous.py:160-183", ("/dice", "/dado", "/d20"),
            "QA says 'roll 6'; v1 ships /dado and /d<N> - alias table already covers both"),
    Feature("fun_ship", "fun", "Ship two members", "M2", Status.DONE,
            Layer.GATEWAY, "UserRegisters.py:216-250", ("/ship", "/shippar", "/shipp"),
            "landed the member registry (cb_core/members.py) it needs; QA's single-tagged-user "
            "scenario describes behaviour v1 never had - see docs/contracts/fun_ship.md"),
    Feature("fun_death", "fun", "Random cause of death", "M2", Status.BLOCKED,
            Layer.GATEWAY, "Miscellaneous.py:335-357", ("/death", "/morte", "/muerte"),
            "image pool only ever lived in v1's private GCS bucket, never checked in - see .specs/features/fun_death/spec.md"),
    Feature("fun_meme", "fun", "Meme generator", "M2", Status.PLANNED,
            Layer.WORKER, "SocialContent.py:224-277", ("/meme",),
            "image compositing is a worker job, not a reply-path call"),
    Feature("fun_battle", "fun", "Battle poll", "M2", Status.PARTIAL,
            Layer.GATEWAY, "SocialContent.py:294-379", ("/battle", "/batalha", "/batalla"),
            "two-people shape ships (roster + getUserProfilePhotos, no scrape); "
            "one-tag/self shapes blocked on the Fight/ GCS export, same as fun_death"),
    Feature("fun_random", "fun", "Random media from the group", "M2", Status.DONE,
            Layer.GATEWAY, "SocialContent.py:198-206", ("/random", "/aleatorio"),
            "MediaService.random() done and tested; handler not written"),
    Feature("fun_firecracker", "fun", "Firecracker sequence", "M2", Status.DONE,
            Layer.GATEWAY, "Miscellaneous.py:226-238", ("/firecracker", "/rojao", "/fogos")),
    Feature("fun_complaint", "fun", "Complaint bit", "M2", Status.DONE,
            Layer.GATEWAY, "Miscellaneous.py:240-259",
            ("/complaint", "/milton", "/reclamacao", "/reclamação", "/queja")),
    Feature("fun_partneredcons", "fun", "Partnered convention posters", "M2", Status.BLOCKED,
            Layer.GATEWAY, "Miscellaneous.py:261-323",
            ("/bff", "/patas", "/fursmeet", "/trex", "/furcamp", "/pawstral"),
            "same GCS blocker as fun_death: every branch reads a Countdown/* prefix "
            "(Miscellaneous.py:18-22) and the single send_photo is unconditional. "
            "/trex is spec'd in QA but missing from v1 - net-new"),

    # -------------------------------------------------------------------- util
    Feature("util_birthday", "util", "Today's birthdays", "M2", Status.PARTIAL,
            Layer.WORKER, "Birthdays.py:14-61", ("/birthday", "/aniversario", "/cumpleanos"),
            "manual command only - the daily every-group broadcast is an unverified, unresolved "
            "parity gap, see docs/contracts/util_birthday.md"),
    Feature("util_nextbirthday", "util", "Upcoming birthdays", "M2", Status.DONE,
            Layer.GATEWAY, "Birthdays.py:104-117", ("/nextbirthday", "/proximosaniversarios"),
            "not group-scoped, matching v1 exactly - see docs/contracts/util_nextbirthday.md"),
    Feature("util_everyone", "util", "Ping every member", "M2", Status.DONE,
            Layer.WORKER, "UserRegisters.py:97-146", ("/everyone", "@everyone"),
            "batched roster read replaces v1's per-user backend call; fan-out moved to cb-worker"),
    Feature("util_calladms", "util", "Call the admins", "M2", Status.DONE,
            Layer.WORKER, "UserRegisters.py:168-203", ("/adm", "@admin", "/report"),
            "group ping on the reply path; DM fan-out to every admin in cb-worker"),
    Feature("util_embedder", "util", "Rewrite social links", "M2", Status.DONE,
            Layer.GATEWAY, "SocialContent.py:79-84", (),
            "rewrites the 3 hosts v1 actually rewrites; vm.tiktok.com short links need a redirect resolve"),
    Feature("util_youtube", "util", "YouTube search", "M2", Status.DONE,
            Layer.WORKER, "SocialContent.py:172-189", ("/youtube",),
            "search + reply moved to cb-worker; v1's googleapiclient call had no timeout at all"),
    Feature("core_musicdetection", "core", "Identify music in voice notes", "M3", Status.PLANNED,
            Layer.WORKER, "Audio.py:6-20", (),
            "ShazamAPI is unofficial - feature-flag it behind a breaker"),
    Feature("util_postforwarder", "util", "Cross-group post forwarding", "M3", Status.DONE,
            Layer.WORKER, "Publisher.py:46-92",
            ("/divulgar", "/publish", "/publicar", "/repost", "/repostar", "/reenviar"),
            "scheduled_posts replaces Publisher.db; approve press now authorised by chat"),
    Feature("util_postgetter", "util", "Receive forwarded posts", "M3", Status.DONE,
            Layer.GATEWAY, "Publisher.py:46-55", (),
            "auto-forward prompt; must stay registered ahead of fun_random"),
    Feature("util_deletereposts", "util", "Delete scheduled reposts", "M3", Status.DONE,
            Layer.GATEWAY, "Publisher.py:316-327", ("/deletereposts", "/deleteposts", "/apagarposts"),
            "QA says /deletereposts, v1 ships /deleteposts"),

    # ---------------------------------- shipped in v1, never specified in QA
    Feature("x_giveaways", "util", "Giveaways", "M3", Status.PLANNED,
            Layer.GATEWAY, "Giveaways.py:25-173", ("/giveaway",), "no QA scenario exists - write one"),
    Feature("x_conversational_ai", "fun", "Conversational AI replies", "M3", Status.DONE,
            Layer.GATEWAY, "NaturalLanguage.py:65-77", (),
            "langchain provider behind the router, tenant budget cap, v1's per-user "
            "streak on a new cache.bump_clamped primitive, per-group rate limit; "
            "QA authored, not ported (7 scenarios) - see docs/contracts/x_conversational_ai.md"),
    Feature("x_speech_to_text", "util", "Voice transcription", "M3", Status.DONE,
            Layer.GATEWAY, "Audio.py:22-32", (),
            "shape (a) ports the voice-to-AI sub-step; shape (b) is a net-new "
            "/transcribe command with no v1 equivalent; QA authored, not ported "
            "(5 scenarios) - see docs/contracts/x_speech_to_text.md"),
    Feature("x_reverse_search", "util", "Reverse image search", "M3", Status.DONE,
            Layer.WORKER, "SocialContent.py:113-142",
            ("/searchsource", "/buscarfonte", "/buscarfuente"),
            "v1 handed SauceNAO a Telegram file URL carrying the bot token (D-RS-1); "
            "v2 uploads the bytes instead"),
    Feature("x_distortion", "fun", "Media distortion", "M3", Status.PLANNED,
            Layer.WORKER, "Distortioner.py:114-156", ("/destroy", "/zoar"),
            "v1 busy-waits on a module global; replace with a worker semaphore"),
    Feature("x_owner_commands", "util", "Owner-only operations", "M3", Status.PLANNED,
            Layer.GATEWAY, "COOKIEBOT.py:83-105",
            ("/grupos", "/broadcast", "/leave", "/blacklist", "/stop", "/restart")),
    Feature("x_custom_commands", "fun", "Per-group custom commands", "M3", Status.BLOCKED,
            Layer.GATEWAY, "Miscellaneous.py:145-158", (),
            "same GCS blocker as fun_death, and worse: the command *names* are the "
            "bucket's Custom/ folder names (Miscellaneous.py:23), so without the "
            "export there is not even a trigger list. Still the seed of tenant "
            "handler packs once the assets land"),
    Feature("x_webhub_login", "platform", "Telegram-login JWT for the web console", "M4", Status.PLANNED,
            Layer.API, "Server.py:25-52", (),
            "v1 regenerates the signing key on every restart (D7) - persist it"),
    Feature("x_analytics_api", "platform", "Per-group analytics endpoints", "M4", Status.PLANNED,
            Layer.API, "", (), "rollup tables exist; no HTTP surface yet"),
)
# fmt: on

MILESTONES: dict[str, str] = {
    "M0": "Skeleton, observability, storage, LLM, self-hosted API",
    "M1": "Survival core — moderation, config, captcha, tenancy",
    "M2": "Fun and utility commands",
    "M3": "Publisher, AI, giveaways, owner tooling",
    "M4": "Analytics surface, web console, data migration",
}

#: Defects carried from v1 that must each become a regression test.
#: Keys match docs/site/content/docs/feature-map.mdx §6.
DEFECTS: dict[str, tuple[str, bool]] = {
    "D1": ("Write calls share the read memo-cache; duplicate writes silently no-op", True),
    "D2": ("verify=False on every backend call", True),
    "D3": ("Busy-wait spin lock serialises all media distortion", False),
    "D4": ("Fixed temp filenames raced across 50 threads", True),
    "D5": ("Shared unlocked SQLite connection", True),
    "D6": ("Unbounded, unlocked, never-expiring caches", True),
    "D7": ("JWT signing key regenerated on every restart", False),
    "D8": ("sleep() in a loop over every group, on a worker thread", False),
    "D9": ("deleteAdmins missing @RequestBody", True),
    "D10": ("No index on Event.groupId, User.username, User.birthdate", True),
    "D11": ("No pagination anywhere", True),
    "D12": ("Actuator health and metrics exposed anonymously", True),
    "D13": ("CORS allowed-origins '*' with credentials", True),
}


def by_status() -> dict[Status, list[Feature]]:
    out: dict[Status, list[Feature]] = {s: [] for s in Status}
    for f in FEATURES:
        out[f.status].append(f)
    return out


def by_milestone() -> dict[str, list[Feature]]:
    out: dict[str, list[Feature]] = {m: [] for m in MILESTONES}
    for f in FEATURES:
        out.setdefault(f.milestone, []).append(f)
    return out
