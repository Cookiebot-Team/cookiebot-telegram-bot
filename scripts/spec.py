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
    Feature("platform_tenancy", "platform", "Multi-tenant registry and schema", "M1", Status.DONE,
            Layer.CORE, notes="tenants table + registry + dispatch gate + llm_overrides/storage_prefix; "
            "handler packs read per update by cb_gateway/packs.py, first family legacy_custom"),
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
            "configs/rules/welcomes/users/blacklist/groups/stickerdatabase import, idempotent "
            "(stickerdatabase -> the new global sticker_pool reference table, migration 0009); "
            "randomdatabase alone still needs a Telegram-download backfill - its rows have no "
            "content_hash/blob_key and media_objects requires both NOT NULL"),
    Feature("util_isalive", "util", "Health check from chat", "M0", Status.DONE,
            Layer.GATEWAY, "Miscellaneous.py:65-69", ("/isalive", "/tavivo")),
    Feature("core_listcommand", "core", "List available commands", "M1", Status.DONE,
            Layer.GATEWAY, "Miscellaneous.py:124-127", ("/commands", "/comandos")),
    Feature("core_privacy", "core", "Privacy policy", "M1", Status.DONE,
            Layer.GATEWAY, "Miscellaneous.py:60-63", ("/privacy", "/privacidade", "/privacidad")),
    Feature("core_reload", "core", "Reload cached admins and settings", "M1", Status.DONE,
            Layer.GATEWAY, "COOKIEBOT.py:197-201", ("/reload", "/recarregar"),
            "v2's caches invalidate themselves (D6), which is why nobody ported this - but the "
            "trigger is still advertised in the Cookiebot_functions.txt this repo ships verbatim, "
            "so typing it answered nothing. Does a real invalidation, not a stub: admins.refresh "
            "re-reads getChatAdministrators, which is the one thing no invalidation of ours could "
            "have known about. QA authored, not ported"),
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
    Feature("core_botskins", "core", "Per-event bot skins", "M1", Status.DONE,
            Layer.GATEWAY, "universal_funcs.py:39-52", (),
            "one process serves every skin; cb_core/skins.py adds the two behavioural forks "
            "v1 keys on is_alternate_bot (intro animation, fun-override flair) plus the "
            "per-skin asset override tree; all 5 personas configured (0007). Handler packs "
            "landed with x_custom_commands - see cb_gateway/packs.py"),

    # --------------------------------------------------------------------- fun
    Feature("fun_dice", "fun", "Roll an n-sided die", "M2", Status.DONE,
            Layer.GATEWAY, "Miscellaneous.py:160-183", ("/dice", "/dado", "/d20"),
            "QA says 'roll 6'; v1 ships /dado and /d<N> - alias table already covers both"),
    Feature("fun_ship", "fun", "Ship two members", "M2", Status.DONE,
            Layer.GATEWAY, "UserRegisters.py:216-250", ("/ship", "/shippar", "/shipp"),
            "landed the member registry (cb_core/members.py) it needs; QA's single-tagged-user "
            "scenario describes behaviour v1 never had - see docs/contracts/fun_ship.md"),
    Feature("fun_death", "fun", "Random cause of death", "M2", Status.DONE,
            Layer.GATEWAY, "Miscellaneous.py:335-357", ("/death", "/morte", "/muerte"),
            "unblocked by cb_worker.bucket_export + cb.py legacy-catalog, which turned v1's "
            "GCS Death/ prefix into a small package-data catalog (cb_core.legacy_assets) over "
            "content-addressed bytes in cb_core.storage - fun_meme's split, not fun_complaint's "
            "vendoring, since 21.5MB is past what belongs in the wheel. gif-vs-photo dispatch "
            "reads the catalog's source_path (v1's original filename), not the storage key's "
            "own extension, on purpose - see death.py. D-DE-1 (dropped skull-emoji prefix for "
            "a target with no username) preserved verbatim; D-DE-3 (v1's ValueError on an "
            "empty bucket listing) fixed - legacy_assets.choose() returns None and the handler "
            "answers nothing, the only state a real deployment can still be in before "
            "legacy-catalog has run. See docs/contracts/fun_death.md"),
    Feature("fun_meme", "fun", "Meme generator", "M2", Status.DONE,
            Layer.WORKER, "SocialContent.py:224-277", ("/meme",),
            "v1's 97kB metadata CSV ships as package data, its 110MB of templates go to "
            "object storage via `cb.py meme-seed`; Pillow compositing in cb-worker; v1's "
            "roster fallback was dead code and its empty-pool branch a NameError, both "
            "fixed - see docs/contracts/fun_meme.md; QA authored (4 scenarios)"),
    Feature("fun_battle", "fun", "Battle poll", "M2", Status.DONE,
            Layer.GATEWAY, "SocialContent.py:294-379", ("/battle", "/batalha", "/batalla"),
            "all three shapes: two people (roster + getUserProfilePhotos, no scrape), "
            "one tag and self against the exported Fight/ pools"),
    Feature("fun_random", "fun", "Random media from the group", "M2", Status.DONE,
            Layer.GATEWAY, "SocialContent.py:198-206", ("/random", "/aleatorio"),
            "MediaService.random() done and tested; handler not written"),
    Feature("fun_firecracker", "fun", "Firecracker sequence", "M2", Status.DONE,
            Layer.GATEWAY, "Miscellaneous.py:226-238", ("/firecracker", "/rojao", "/fogos")),
    Feature("fun_complaint", "fun", "Complaint bit", "M2", Status.DONE,
            Layer.GATEWAY, "Miscellaneous.py:240-259",
            ("/complaint", "/milton", "/reclamacao", "/reclamação", "/queja")),
    Feature("fun_partneredcons", "fun", "Partnered convention posters", "M2", Status.DONE,
            Layer.GATEWAY, "Miscellaneous.py:261-323",
            ("/bff", "/patas", "/fursmeet", "/trex", "/furcamp", "/pawstral"),
            "hardcoded dates and captions verbatim, +365 wraparound preserved, ungated "
            "like v1; /trex is net-new and sends a Countdown/Trex poster with no caption"),

    # -------------------------------------------------------------------- util
    Feature("util_birthday", "util", "Today's birthdays", "M2", Status.DONE,
            Layer.WORKER, "Birthdays.py:14-61", ("/birthday", "/aniversario", "/cumpleanos"),
            "both v1 shapes: the manual command, and the daily every-group broadcast - whose "
            "caller turned out to be COOKIEBOT.py:333-339 (the message handler's finally, on "
            "the first update of a new UTC day), not a scheduler. v2 runs it as a cron with "
            "one deferred job per group instead of v1's sleep(3) loop (D8)"),
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
    Feature("core_musicdetection", "core", "Identify music in voice notes", "M3", Status.DONE,
            Layer.WORKER, "Audio.py:6-20", (),
            "passive, off by default, breakered cb-worker job; shazamio-core segfaults on "
            "py3.14 so the recogniser is AudD's documented API behind a swappable seam - "
            "see docs/contracts/core_musicdetection.md; QA authored (5 scenarios)"),
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
    Feature("x_giveaways", "util", "Giveaways", "M3", Status.DONE,
            Layer.GATEWAY, "Giveaways.py:25-173", ("/giveaway",),
            "two distributed tables replace Giveaways.db; v1's /giveaway never completed "
            "(json.loads on a de-quoted callback payload) and its enter button was "
            "admin-only - both fixed, see docs/contracts/x_giveaways.md; "
            "QA authored, not ported (10 scenarios)"),
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
    Feature("x_distortion", "fun", "Media distortion", "M3", Status.DONE,
            Layer.WORKER, "Distortioner.py:114-156", ("/destroy", "/zoar", "/destruir"),
            "branch chain on the reply path, carve + ffmpeg in cb-worker behind a real "
            "semaphore (D3) with per-call temp dirs (D4); v1's video/GIF arms are "
            "unreachable and stay disabled; seam carving over numpy replaces ImageMagick "
            "liquid_rescale - see docs/contracts/x_distortion.md; QA authored (12 scenarios)"),
    Feature("x_owner_commands", "util", "Owner-only operations", "M3", Status.DONE,
            Layer.GATEWAY, "COOKIEBOT.py:83-105",
            ("/grupos", "/groups", "/broadcast", "/leave", "/blacklist", "/unblacklist",
             "/stop", "/restart"),
            "private-chat only and gated on CB_OWNER_ID; /grupos is one paged message "
            "instead of v1's one getChat + one sendMessage per group (D11) and "
            "/broadcast is a cb-worker fan-out instead of a sleep(0.5) loop on the "
            "handler thread (D8); /stop and /restart answer a refusal rather than "
            "os._exit-ing one of N replicas - see docs/contracts/x_owner_commands.md; "
            "QA authored (9 scenarios)"),
    Feature("x_custom_commands", "fun", "Per-group custom commands", "M3", Status.DONE,
            Layer.GATEWAY, "Miscellaneous.py:145-158", (),
            "53 exported Custom/ folders are the trigger list, matched by a filter rather "
            "than COMMAND_ALIASES because the names are data; gated per tenant by "
            "cb_gateway/packs.py, which is what finally reads tenants.handler_pack"),
    Feature("x_age_guess", "fun", "Age guess (agify.io)", "M3", Status.DONE,
            Layer.GATEWAY, "Miscellaneous.py:185-202", ("/idade", "/age", "/edad"),
            "GET agify.io?name=, timeout+Breaker per doomlist.py's pattern; fun-gated with "
            "v1's fun_off reply. Argument comes from ParsedCommand.args rather than v1's "
            "replace-chain, which crashed on a lone trailing space; an agify timeout/error/"
            "malformed body/open breaker answers the same not_know text as count == 0, "
            "since v1 never handles that case at all (silence). QA authored, not ported"),
    Feature("x_gender_guess", "fun", "Gender guess (genderize.io)", "M3", Status.DONE,
            Layer.GATEWAY, "Miscellaneous.py:204-224", ("/genero", "/gênero", "/gender"),
            "GET genderize.io?name=, same timeout+Breaker/argument/failure handling as "
            "x_age_guess. A null gender with a non-zero count (should be unreachable behind "
            "count == 0, but v1's f\"gender.{genero}\" would build the dead key "
            "'gender.None' if it were) renders the dormant 'gender.unknown' entry that v1's "
            "own lib.json already ships (en only, never read by any v1 code path) instead "
            "of crashing or going silent. QA authored, not ported"),
    Feature("x_unearth", "fun", "Unearth a random old message", "M3", Status.DONE,
            Layer.GATEWAY, "Miscellaneous.py:325-333", ("/desenterrar", "/unearth"),
            "forwards a random message_id in [1, current], fun-gated with v1's fun_off reply. "
            "v1 wrote a 100-attempt retry and then returned inside its own except, so it tried "
            "once and answered nothing whenever that id was deleted; the retry is real here, "
            "bounded at 8. QA authored, not ported"),
    Feature("x_fortune_cookie", "fun", "Fortune cookie", "M3", Status.DONE,
            Layer.GATEWAY, "Miscellaneous.py:359-375", ("/sorte", "/fortunecookie", "/suerte"),
            "animated GIF + locale-random fortune line from sorte.txt (locales.lines) plus "
            "six lucky numbers, one per tens-decade; fun-gated with v1's fun_off reply. "
            "v1's time.sleep(3) between sending the animation and deleting it blocked the "
            "whole process, so the delete-then-answer tail now runs as a background "
            "asyncio.Task (complaint.py's _schedule_tail idiom) instead, keeping v1's exact "
            "user-visible order without holding the reply path open. QA authored, not ported"),
    Feature("x_image_search", "util", "Image search (qualquer coisa)", "M3", Status.PLANNED,
            Layer.GATEWAY, "SocialContent.py:144-170", ("/qualquercoisa", "/anything", "/cualquiercosa"),
            "Google Custom Search Image API, sfw-gated; no QA scenario exists - write one"),
    Feature("x_drawing_idea", "fun", "Drawing idea prompt", "M3", Status.DONE,
            Layer.GATEWAY, "Miscellaneous.py:137-143", ("/ideiadesenho", "/drawingidea", "/ideadibujo"),
            "3,435 exported references; the caption's id is the index drawn, so the "
            "catalog's sort order is the contract. Scenarios authored locally - QA has none"),
    Feature("x_analysis", "util", "Message analysis (reply_to_message dump)", "M3", Status.DONE,
            Layer.GATEWAY, "Miscellaneous.py:71-81", ("/analise", "/analisis", "/analysis"),
            "dumps the replied-to message's fields back to chat, ungated exactly as v1 "
            "dispatches it (COOKIEBOT.py:202); truncates at 4000 chars, where v1 sent the "
            "whole dump and Telegram rejected anything over 4096 - so the command did "
            "nothing on exactly the messages worth analysing. QA authored, not ported"),
    Feature("x_sticker_autoreply", "fun", "Sticker DB auto-reply", "M3", Status.DONE,
            Layer.GATEWAY, "SocialContent.py:208-222", (),
            "passive: pools an sfw group's alphanumeric-set-name, non-banned-emoji stickers "
            "into a new GLOBAL sticker_pool reference table (migration 0009, not per-group like "
            "fun_random - full reasoning in that migration's docstring), then replies with one "
            "at random to any sticker/document/animation sent in reply to the bot. Deviations: "
            "(1) 'reply is from the bot' now checks reply_to_message.from_user.id == bot.id, not "
            "v1's literal first_name == 'Cookiebot' (wrong for every other persona this codebase "
            "ships); (2) pooling has no funfunctions gate, matching v1's real asymmetry exactly "
            "(only sfw + sender-has-username) - only the reply side is fun-gated; (3) write is a "
            "Valkey-fronted ON CONFLICT DO NOTHING upsert, since a reference-table write is 2PC "
            "replicated to every node and most sends repeat a pack already pooled. Also unblocks "
            "the stickerdatabase importer collection (map_stickerdatabase mapped it to skip "
            "for want of a destination table; now maps every row - see platform_migration_etl). "
            "QA authored, not ported"),
    Feature("x_webhub_login", "platform", "Telegram-login JWT for the web console", "M4", Status.DONE,
            Layer.API, "Server.py:25-52", (),
            "D7 fixed: the RSA key is configured or generated once into signing_keys "
            "(migration 0008), so it survives a restart and every replica shares it - "
            "v1 generated one per gunicorn worker per start and published only the "
            "answering worker's in its JWKS. Also D-WL-2: v1's pop('hash') meant only "
            "the first of its five bot tokens could ever sign anyone in. auth_date "
            "enforcement is written but off by default (the WebHub renews by replaying "
            "the payload) - see docs/contracts/x_webhub_login.md. No QA scenario: the "
            "feature has no Telegram surface"),
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
