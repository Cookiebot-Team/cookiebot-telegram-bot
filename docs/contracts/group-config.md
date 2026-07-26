# Contract: group configuration (v1 -> v2)

Phase 2 of `/migrate-feature` for `group_configs`. The table has existed since
`packages/cb-api/migrations/versions/0001_initial_schema.py:158-180` with nothing
reading or writing it. Every gated command listed in FEATURE-MAP (`functions_fun`,
`functions_utility`, `sfw`, `language`, sticker-spam limits, media restrict, captcha
timeout, doomlist) depends on this contract.

## Where v1's defaults actually live

The task brief for this port assumed the defaults live in the Java backend. They do
not: `Config.java` (`../COOKIEBOT-backend/src/main/java/com/cookiebot/cookiebotbackend/core/domains/Config.java:16-34`)
is a bare Lombok `@Data` class — every field is a boxed `Boolean`/`Integer`/`String`
with no initializer, and `ConfigService.updateConfig` (`ConfigService.java:51-103`)
only overwrites a field `if (config.getX() != null)`, i.e. the backend is a dumb
partial-update store with an implicit `null` "unset" state. It never manufactures a
default value. The one Mongo-shape quirk worth recording: `Config.java:23` types
`stickerSpamLimit` as `String`, while every producer/consumer of it (Python bot,
this contract) treats it as an integer — a latent type bug in the Java model,
inherited from Mongo's schemaless documents.

**The real source of truth is the Python bot**, `Configurations.py:111`:

```python
(
    FurBots,
    sfw,
    stickerspamlimit,
    limbotimespan,
    captchatimespan,
    funfunctions,
    utilityfunctions,
    language,
    publisherpost,
    publisherask,
    threadPosts,
    maxPosts,
    publisherMembersOnly,
) = 1, 1, 5, 600, 300, 1, 1, "pt", 0, 1, "9999", 9999, 0
```

These are the values served in-process when the backend has no config document yet
(`Configurations.py:103-114`), and they are the exact payload POSTed to create one
(`Configurations.py:116-119`) — so "v1's default" and "v1's seed value" are the same
13 numbers.

## Field mapping: v1 -> v2 column, with defaults

| v1 name (Java field / Python var) | v1 default (`Configurations.py:111`) | v2 column (`0001_initial_schema.py:158-180`) | v2 SQL default | Match? |
|---|---|---|---|---|
| `furbots` / `FurBots` | `1` (true) | `allow_furbots` | `true` | same |
| `sfw` | `1` (true) | `sfw` | `true` | same |
| `stickerSpamLimit` / `stickerspamlimit` | `5` | `sticker_spam_limit` | `5` | same |
| `timeWithoutSendingImages` / `limbotimespan` | `600` (seconds) | `media_restrict_seconds` | `0` | **mismatch** |
| `timeCaptcha` / `captchatimespan` | `300` (seconds; see below) | `captcha_timeout_seconds` | `120` | **mismatch** |
| `functionsFun` / `funfunctions` | `1` (true) | `functions_fun` | `true` | same |
| `functionsUtility` / `utilityfunctions` | `1` (true) | `functions_utility` | `true` | same |
| `language` | `"pt"` | `language` | `'en'` | **mismatch** (see below) |
| `publisherPost` / `publisherpost` | `0` (false) | `publisher_post` | `false` | same |
| `publisherAsk` / `publisherask` | `1` (true) | `publisher_ask` | `true` | same |
| `publisherMembersOnly` | `0` (false) | `publisher_members_only` | `false` | same |
| `threadPosts` | `"9999"` (sentinel: no topic) | `thread_posts` | `NULL` | same meaning, one sentinel (ETL converts) |
| `maxPosts` / `maxPosts` | `9999` | `max_posts` | `3` | **mismatch** |
| — (no v1 field) | — | `sticker_spam_window_s` | `60` | **v2-only** |
| — (no v1 field) | — | `doomlist_enabled` | `true` | **v2-only** |

Every v1 field has a v2 column. No v1 field is dropped.

### Mismatches, explained

- **`media_restrict_seconds` (0 vs v1's 600s / 10 min).** The 0001 migration's SQL
  `DEFAULT 0` effectively disables the "new members can't post media" limbo window
  out of the box, where v1 always shipped it on. Recorded here as a finding per the
  task brief; no migration is added by this change. `GroupConfig`'s in-code
  `DEFAULTS` (used on the fully-empty-database / DB-down fallback path) carries the
  **v1** value, `600`, so an existing v1 group backed by a real row is unaffected
  either way, and a brand-new row created by `set_config` without this column takes
  the SQL default (`0`) unless the caller passes it explicitly.
- **`captcha_timeout_seconds` (120 vs v1's 300s / 5 min).** Same shape of mismatch.
  Note v1 also had a unit-coercion quirk: `Configurations.py:134-135` treats any
  value `< 30` as *minutes* and multiplies by 60 (so an admin who typed `5` meaning
  "5 minutes" got 300s) — the seed default of `300` was already past that threshold
  so it round-trips unchanged. v2's `captcha_timeout_seconds` is seconds-only, no
  such coercion; this is an intentional simplification, not preserved.
- **`language` ('en' vs v1's `"pt"`).** This is the sharpest mismatch and is
  deliberate on the v2 side, not a bug: `settings.default_language` (`cb_core/settings.py:50`)
  is the multi-tenant default for a *brand-new* deployment, and this port's
  `GroupConfig.DEFAULTS.language` is sourced from that setting rather than
  hardcoded to v1's Portuguese-first default. Existing v1 groups keep whatever
  literal string is already in their row (`"pt"`, `"eng"`, or `"es"` — v1 never
  stores the ISO form `"en"`; see `docs/contracts/locales.md`), so nothing changes
  for a live group. Only a group with **no row and no tenant override** — i.e. one
  that has never run `/config` or `/setlang` under v1 either — gets the v2 default
  instead of v1's `"pt"`. Flagged for the owner to confirm; not fixed by adding a
  migration.
- **`thread_posts` ("9999" sentinel vs SQL `NULL`).** v1 uses the string `"9999"` as
  an "no forum topic configured" sentinel consumed by the publisher
  (`util_postgetter`, FEATURE-MAP). The v2 column is nullable with no SQL default.
  `GroupConfig.DEFAULTS.thread_posts` is `None`: v2 keeps one sentinel, not two, so
  the application-level fallback matches v1 exactly; a raw `INSERT ... DEFAULT`
  bypassing `set_config` would still get SQL `NULL`, which is a second, weaker
  finding recorded here rather than papered over.
- **`max_posts` (3 vs v1's 9999, i.e. "unlimited").** The SQL default of `3` is a
  real behavioural cap that v1 never had. `GroupConfig.DEFAULTS.max_posts = 9999`
  preserves v1's effectively-unlimited default on the fallback path; any row
  created without this column explicit gets the tighter SQL default of `3`
  instead. Recorded as a finding, no migration added.
- **`sticker_spam_window_s` (v2-only, default 60s).** v1's `sticker_anti_spam`
  (`Cooldowns.py:8-22`) has no time window at all: `last_used_sticker[chat_id]` is a
  plain unbounded counter that only resets when the process restarts (the key is
  never deleted), so once a chat crosses `stickerspamlimit` consecutive stickers,
  *every* sticker sent in that chat is deleted forever until the bot process
  restarts — a defect, not a feature (adjacent to FEATURE-MAP D6: unlocked,
  never-expiring per-process state). v2 introduces an actual fixed window via
  `cb_core.cache.incr_window`, gated by this new column, which is a deliberate fix.
- **`doomlist_enabled` (v2-only, default true).** v1's CAS/banlist join-gate
  (`GroupShield.py:172-229`, `check_cas`/`check_banlist`/`check_banlist_public`,
  wired at `COOKIEBOT.py:142`) is unconditional — there is no way for a group to
  opt out. v2 makes it configurable; default `true` preserves v1's always-on
  behaviour for every existing group.

## How v1 reads config, caches it, and "reloads"

- **Read path**: `get_config(cookiebot, chat_id, ignorecache=False, is_alternate_bot=0)`
  (`Configurations.py:103-137`) checks `cache_configurations[chat_id]` first
  (`Configurations.py:9`, a bare module-level `dict`, no lock, no TTL). On a cache
  miss it does a blind synchronous `GET configs/{chat_id}` against the Java backend
  (`get_request_backend`), seeds the row via `POST` if the backend 404s, and caches
  the resolved 13-tuple unconditionally — `Configurations.py:136`.
- **Every message pays for this**: `COOKIEBOT.py:113` calls `get_config` once per
  incoming update in a group, unpacking into the 13 positional locals used
  throughout the rest of the dispatcher (`funfunctions`/`utilityfunctions` gate the
  fun/utility command blocks at `COOKIEBOT.py:218` and `:252`; `stickerspamlimit`
  feeds `sticker_anti_spam` at `:180`; `captchatimespan` and `limbotimespan` gate
  join handling at `:141-150`).
- **`/reload` (and `/recarregar`)**, `COOKIEBOT.py:197-201`, is the *only* way to
  drop the cache: it calls `get_admins(..., ignorecache=True)` and
  `get_config(..., ignorecache=True)` **in the process that received the command**.
  Five bot processes (personas selected by CLI arg, `universal_funcs.py:39-52`) each
  hold an independent `cache_configurations` dict, so `/reload` only fixes the
  replica that happened to handle it — the other four keep serving the stale
  config until they separately restart or someone runs `/reload` against each of
  them. This is FEATURE-MAP D6 and is the failure mode this port replaces.
- There is no TTL anywhere in the v1 cache; a config can be arbitrarily stale
  indefinitely with no `/reload`.

## v2 design

Public API — `packages/cb-core/src/cb_core/group_config.py`:

```python
@dataclass(frozen=True, slots=True)
class GroupConfig:
    group_id: int
    allow_furbots: bool
    sticker_spam_limit: int
    sticker_spam_window_s: int
    media_restrict_seconds: int
    captcha_timeout_seconds: int
    functions_fun: bool
    functions_utility: bool
    sfw: bool
    language: str
    publisher_post: bool
    publisher_ask: bool
    publisher_members_only: bool
    thread_posts: str | None
    max_posts: int
    doomlist_enabled: bool

    def feature_enabled(self, area: str) -> bool: ...  # 'fun'|'utility'

DEFAULTS: GroupConfig
async def get_config(group_id: int) -> GroupConfig
async def set_config(group_id: int, **fields: object) -> GroupConfig
async def invalidate(group_id: int) -> None
async def start_invalidation_listener() -> None
async def stop_invalidation_listener() -> None
def cached_size() -> int
```

**Read path (replaces `Configurations.py:103-137` + the five-process `/reload`
problem):**

1. **L1** — a per-process `dict[group_id, (GroupConfig, monotonic_deadline)]`, TTL
   `settings.config_cache_l1_seconds`. Bounded staleness even with no invalidation
   message (unlike v1's unbounded cache).
2. **L2** — Valkey via `cb_core.cache.get_json`/`set_json`, TTL
   `settings.config_cache_l2_seconds`, key `cb:groupconfig:{group_id}`.
3. **Postgres** — one query, filtered on `group_id` (the distribution column):
   `groups` LEFT JOIN `group_configs` on `group_id`, `WHERE group_id = $1`. Single
   shard (both tables are colocated distributed tables — AGENTS.md §4 rule 4); the
   join also recovers the group's `tenant_id` without a second lookup. If the
   `group_configs` side is `NULL` (no row) the group gets `tenant.feature_defaults`
   layered over `DEFAULTS`; if the row exists (v2's schema makes every column
   `NOT NULL`, so a present row is always fully populated) it wins outright, same
   as v1 where a config document either exists complete or not at all.
4. **Merge order**: `DEFAULTS` < tenant `feature_defaults` (`cb_core/tenancy.py`,
   looked up via `tenancy.registry.by_id`, reference table, node-local) < the
   group's own row.
5. **DB unreachable or the query failing**: logged, `cb_core.metrics.config_fallback_total`
   counted, `DEFAULTS` (with the caller's `group_id`) served — never raises into a
   reply path.
6. Every layer hit/miss/error is counted through
   `cb_core.metrics.cache_lookups_total(cache="config", layer="l1"|"l2"|"db", outcome=...)`.

**Write path (replaces the "send /reload in every process" instruction at
`Configurations.py:209`):** `set_config` validates the kwargs against a whitelist of
the 15 real columns (never `group_id`), builds a parameterised
`INSERT ... ON CONFLICT (group_id) DO UPDATE` touching only the given columns, then
calls `invalidate`, which drops the local L1 entry, deletes the L2 key, and
publishes on `cb_core.cache.INVALIDATION_CHANNEL` via `publish_invalidation`. Every
replica's `start_invalidation_listener` subscription (`cb_core.cache.subscribe_invalidations`)
receives the key and drops its own L1 entry — the fix for v1's D6 without anyone
typing `/reload`.

## Findings summary (no migration added)

- `media_restrict_seconds`, `captcha_timeout_seconds`, `language`, `max_posts`: v2
  SQL column defaults in `0001_initial_schema.py` diverge from v1's true defaults.
  `GroupConfig.DEFAULTS` follows v1 on all of these except `language`, which is
  deliberately sourced from `settings.default_language` for greenfield tenants.
- `thread_posts`: v1's sentinel is the string `"9999"`; v2 uses `NULL` for the same
  meaning, in both the column and `GroupConfig.DEFAULTS`. The M4 Mongo ETL converts
  `"9999"` to `NULL` on import.
- `sticker_spam_window_s`, `doomlist_enabled`: v2-only columns, no v1 field. Both
  default to v1's actual (buggy, in the sticker case) always-on behaviour.
- No v1 field is missing a v2 column.
