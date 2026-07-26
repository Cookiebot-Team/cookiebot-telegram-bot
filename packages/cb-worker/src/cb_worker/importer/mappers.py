"""Mongo document -> target-table rows, one pure function per v1 collection.

No I/O anywhere in this file — every function is `(Document, MappedRows) -> None`,
so the shape rules below are unit-testable with a plain dict and no database
(`packages/cb-worker/tests/test_importer_mappers.py`). Shapes are transcribed from
the Java `@Document` entities in
`../COOKIEBOT-backend/src/main/java/com/cookiebot/cookiebotbackend/core/domains/`,
which AGENTS.md names as the source of truth for stored data; defaults come from
`cb_core.group_config.DEFAULTS`, which already encodes v1's true defaults
(`Configurations.py:111` — the Java `Config` entity itself carries none, see
`docs/contracts/group-config.md`).

Every Mongo `_id` (or, for `groups`, the `groupId` field — `Group.java` has no
`@Id`, so its real Mongo `_id` is an unrelated auto-generated `ObjectId`) is a
Telegram id transcribed as a Python `str`. A value that will not parse as `int`
is skipped and counted via `out.skip(...)`, never guessed at.

Row tuples are documented column-by-column at each `out.add(table, ...)` call;
that comment is the contract `loader.py` writes its `INSERT` column lists
against. Time-stamped bookkeeping columns with a sensible `DEFAULT` in the DDL
(`updated_at`, `synced_at`, `created_at`) are deliberately left out of every row
so a mapper stays a pure, deterministic function of its input document — a
loader binding `now()` once per statement (as `group_config.set_config` already
does, for the same Citus-non-IMMUTABLE-function reason) is where "when we saw
this row" belongs, not here.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime
from typing import Any

from cb_core.group_config import DEFAULTS
from cb_worker.importer import Document, MappedRows

# ----------------------------------------------------------------------- helpers


def _doc_id_str(doc: Document) -> str:
    """Best-effort human-readable id for a `Skipped` record, never raises."""
    raw = doc.get("_id")
    return "" if raw is None else str(raw)


def _parse_bigint(value: Any) -> int | None:
    """Every v1 id is transcribed as a `str` holding a Telegram id (bigint here).

    `int()` also happily accepts an already-numeric value (a dump that stored a
    raw BSON int32/int64 instead of the Java `String` type would still work),
    but never a float-looking string ("123.0") — v1 never produces one, so
    treating it as invalid rather than truncating is the honest choice.
    """
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _bool_or_default(value: Any, default: bool) -> bool:
    """`null`/absent -> v1 default. Mongo is schemaless, so a stray string is
    handled explicitly rather than falling into Python's `bool("false") is True`
    trap.
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("true", "1"):
            return True
        if lowered in ("false", "0"):
            return False
        return default
    return bool(value)


def _int_or_default(value: Any, default: int) -> int:
    """`null`/absent -> v1 default. Also covers `stickerSpamLimit`, which
    `Config.java:23` types as `String` while every consumer treats it as an int
    (`docs/contracts/group-config.md`) — `int()` parses the numeral either way.
    A value that is present but not a number is data corruption, not an
    intentional "unset"; falling back to the same default as "absent" is safer
    than propagating a `NULL` mypy/Postgres wouldn't accept into a `NOT NULL`
    column.
    """
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _convert_thread_posts(value: Any) -> str | None:
    """v1's `"9999"` sentinel ("no forum topic configured") becomes v2's `NULL`
    — one sentinel, not two (`docs/contracts/group-config.md`,
    `GroupConfig.DEFAULTS.thread_posts`). Anything else is a real topic id and
    is stored verbatim as text (the v2 column is `text`, not `int` — no numeric
    parsing needed or wanted).
    """
    if value is None:
        return DEFAULTS.thread_posts
    if str(value) == "9999":
        return None
    return str(value)


def _convert_birthdate(value: Any) -> date | None:
    """`User.birthdate` is a Java `LocalDate` (`User.java:26`). Spring Data
    Mongo's JSR-310 converter stores a `LocalDate` as a BSON UTC datetime (start
    of day in the JVM's system default zone) — never a BSON date-only or string
    type — so PyMongo hands it back as a naive `datetime.datetime`. `.date()`
    on that is the correct read; a raw ISO string is also accepted (e.g. a
    `mongoexport` JSON dump), for robustness, since nothing here can tell which
    tool produced the document. Anything else (or an unparsable string) is
    treated as absent rather than guessed at — `users.birthdate` is nullable in
    v2, so `NULL` is a safe, honest fallback, unlike the NOT NULL config columns
    above.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            return None
    return None


# ------------------------------------------------------------------------ configs


def map_configs(doc: Document, out: MappedRows) -> None:
    """`configs` -> `group_configs`.

    Row: (group_id, allow_furbots, sticker_spam_limit, sticker_spam_window_s,
    media_restrict_seconds, captcha_timeout_seconds, functions_fun,
    functions_utility, sfw, language, publisher_post, publisher_ask,
    publisher_members_only, thread_posts, max_posts, doomlist_enabled) —
    `group_configs`' own column order (`0001_initial_schema.py:180-201`), minus
    `updated_at` (see module docstring).

    `sticker_spam_window_s` and `doomlist_enabled` have no v1 field at all
    (`Config.java` carries neither) — every row gets `DEFAULTS`' value for both,
    which is v1's actual always-on behaviour for doomlist and the new fixed
    window for sticker spam (`docs/contracts/group-config.md`).
    """
    group_id = _parse_bigint(doc.get("_id"))
    if group_id is None:
        out.skip("configs", _doc_id_str(doc), "_id is not a parseable integer chat id")
        return

    out.add(
        "group_configs",
        (
            group_id,
            _bool_or_default(doc.get("furbots"), DEFAULTS.allow_furbots),
            _int_or_default(doc.get("stickerSpamLimit"), DEFAULTS.sticker_spam_limit),
            DEFAULTS.sticker_spam_window_s,
            _int_or_default(doc.get("timeWithoutSendingImages"), DEFAULTS.media_restrict_seconds),
            _int_or_default(doc.get("timeCaptcha"), DEFAULTS.captcha_timeout_seconds),
            _bool_or_default(doc.get("functionsFun"), DEFAULTS.functions_fun),
            _bool_or_default(doc.get("functionsUtility"), DEFAULTS.functions_utility),
            _bool_or_default(doc.get("sfw"), DEFAULTS.sfw),
            doc.get("language") if doc.get("language") is not None else DEFAULTS.language,
            _bool_or_default(doc.get("publisherPost"), DEFAULTS.publisher_post),
            _bool_or_default(doc.get("publisherAsk"), DEFAULTS.publisher_ask),
            _bool_or_default(doc.get("publisherMembersOnly"), DEFAULTS.publisher_members_only),
            _convert_thread_posts(doc.get("threadPosts")),
            _int_or_default(doc.get("maxPosts"), DEFAULTS.max_posts),
            DEFAULTS.doomlist_enabled,
        ),
    )


# -------------------------------------------------------------------------- rules


def map_rules(doc: Document, out: MappedRows) -> None:
    """`rules` -> `group_rules`. Row: (group_id, body).

    `group_rules.body` is `NOT NULL` (`0001_initial_schema.py:206-215`) and
    `Rule.rules` (`Rule.java:22`) carries no default, so a document missing the
    text entirely cannot be mapped — it is skipped rather than inserting a
    placeholder string that would look like real rules content.
    """
    group_id = _parse_bigint(doc.get("_id"))
    if group_id is None:
        out.skip("rules", _doc_id_str(doc), "_id is not a parseable integer chat id")
        return

    body = doc.get("rules")
    if body is None:
        out.skip("rules", str(group_id), "rules text missing (group_rules.body is NOT NULL)")
        return

    out.add("group_rules", (group_id, str(body)))


# ----------------------------------------------------------------------- welcomes


def map_welcomes(doc: Document, out: MappedRows) -> None:
    """`welcomes` -> `group_welcomes`. Row: (group_id, body).

    Same NOT NULL reasoning as `map_rules`: `Welcome.message` (`Welcome.java:23`)
    has no default and `group_welcomes.body` is `NOT NULL`
    (`0001_initial_schema.py:219-228`).
    """
    group_id = _parse_bigint(doc.get("_id"))
    if group_id is None:
        out.skip("welcomes", _doc_id_str(doc), "_id is not a parseable integer chat id")
        return

    body = doc.get("message")
    if body is None:
        out.skip("welcomes", str(group_id), "message missing (group_welcomes.body is NOT NULL)")
        return

    out.add("group_welcomes", (group_id, str(body)))


# -------------------------------------------------------------------------- users


def map_users(doc: Document, out: MappedRows) -> None:
    """`users` -> `users`. Row: (user_id, username, first_name, last_name,
    language_code, birthdate) — `users`' column order
    (`0001_initial_schema.py:73-88`), minus `is_bot`/`created_at`/`updated_at`
    (no v1 source; `birth_month`/`birth_day` are `GENERATED` columns, never
    inserted).

    `username`/`firstName`/`lastName`/`languageCode` (`User.java:22-25`) may all
    be absent — the v2 columns are nullable `text`, so they are passed through
    verbatim, `None` included.
    """
    user_id = _parse_bigint(doc.get("_id"))
    if user_id is None:
        out.skip("users", _doc_id_str(doc), "_id is not a parseable integer user id")
        return

    out.add(
        "users",
        (
            user_id,
            doc.get("username"),
            doc.get("firstName"),
            doc.get("lastName"),
            doc.get("languageCode"),
            _convert_birthdate(doc.get("birthdate")),
        ),
    )


# ----------------------------------------------------------------------- blacklist


def map_blacklist(doc: Document, out: MappedRows) -> None:
    """`blacklist` -> `blacklist`. Row: (subject_id, kind, reason, source).

    `Blacklist.java:21` is nothing but an `@Id` — v1 merges banned users
    (`/blacklist` command) and banned chats (`leave_and_blacklist`,
    `COOKIEBOT.py:96,118,126`) into the same collection with no field
    distinguishing the two. Telegram's own id scheme is the only legitimate
    signal left: user ids are always positive, group/supergroup/channel ids are
    always negative — so `kind` is derived from the sign of the parsed id, not
    invented. `reason` has no v1 source at all (`None`); `source` is v1's
    `blacklist` column default, `'manual'` — the collection's real provenance
    (manual command vs. auto-blacklist-on-leave) is likewise not recorded
    anywhere in Mongo, so this is the honest "unknown, default bucket" choice,
    not a claim that every imported row was a manual `/blacklist` call.
    """
    subject_id = _parse_bigint(doc.get("_id"))
    if subject_id is None:
        out.skip("blacklist", _doc_id_str(doc), "_id is not a parseable integer id")
        return

    kind = "chat" if subject_id < 0 else "user"
    out.add("blacklist", (subject_id, kind, None, "manual"))


# -------------------------------------------------------------------------- groups


def map_groups(doc: Document, out: MappedRows) -> None:
    """`groups` -> `groups` (one row) + `group_admins` (one row per admin).

    `groups` row: (group_id, title, image_url) — `groups`' column order
    (`0001_initial_schema.py:142-153`), minus `username`/`chat_type`/`skin`/
    `tenant_id`/`joined_at`/`left_at`: none of those has a v1 source (`Group.java`
    carries only `groupId`, `name`, `imageUrl`, `adminUsers`), so they are left
    for the DDL's own defaults.

    `group_admins` row per entry of `adminUsers`: (group_id, user_id, role,
    anonymous) — `group_admins`' column order (`0001_initial_schema.py:252-262`),
    minus `synced_at`. `Group.adminUsers` is a bare `Set<String>`
    (`Group.java:28`); v1 tracks the creator/administrator distinction only in an
    ephemeral, never-persisted list (`listaadmins_status`, `Configurations.py`),
    so Mongo alone cannot say which admin (if any) is the creator. `role` is
    hardcoded to `'administrator'` — it is both the SQL column default and the
    safer of the two guesses (most entries in the set are not the creator, and
    nothing here claims to know one way or the other). `anonymous` has no v1
    source either and is hardcoded `False`, matching the SQL default.

    Unlike every other collection, the natural key here is the `groupId` field,
    not Mongo's own `_id`: `Group.java` has no `@Id`, so its real `_id` is an
    unrelated, Spring-auto-generated `ObjectId`.
    """
    group_id = _parse_bigint(doc.get("groupId"))
    if group_id is None:
        out.skip("groups", _doc_id_str(doc), "groupId is not a parseable integer chat id")
        return

    out.add("groups", (group_id, doc.get("name"), doc.get("imageUrl")))

    for admin in doc.get("adminUsers") or ():
        admin_id = _parse_bigint(admin)
        if admin_id is None:
            out.skip(
                "groups",
                str(group_id),
                f"adminUsers entry {admin!r} is not a parseable integer user id",
            )
            continue
        out.add("group_admins", (group_id, admin_id, "administrator", False))


# -------------------------------------------------------------------- randomdatabase


def map_randomdatabase(doc: Document, out: MappedRows) -> None:
    """`randomdatabase` has no v2 destination — every document is skipped.

    v1's random-media pool is only ever a pointer (`RandomDatabase.java`):
    `{_id: chat_id, idMessage, idMedia}`, never bytes — `random_media`
    (`SocialContent.py:198-206`) forwards the still-live source message instead
    of re-sending stored content. v2's `media_objects` requires `content_hash`,
    `blob_key` and `byte_size` `NOT NULL`
    (`0002_media_and_llm_usage.py:80-99`) because the whole media layer dedupes
    by content hash; inventing a hash or a blob key for bytes we never
    downloaded would silently corrupt that dedupe for every future write to the
    same group. Backfilling this collection for real needs a separate job that
    downloads the referenced Telegram message/file and writes genuine blobs
    (`docs/contracts/fun_random.md`'s re-architecture notes) — not a pure,
    I/O-free mapper.
    """
    out.skip(
        "randomdatabase",
        _doc_id_str(doc),
        "no bytes/content_hash in v1 source; media_objects requires them NOT NULL "
        "-- needs a Telegram-download backfill job, not an ETL mapper",
    )


# -------------------------------------------------------------------- stickerdatabase


def map_stickerdatabase(doc: Document, out: MappedRows) -> None:
    """`stickerdatabase` has no v2 destination — every document is skipped.

    `StickerDatabase.java` stores only a Telegram sticker `file_id`, feeding a
    different v1 feature (`reply_sticker`, `SocialContent.py:218-221` — replying
    to the bot gets a random pooled sticker back) than the photo/video
    `/random` pool. `docs/contracts/fun_random.md` explicitly scopes
    `add_to_sticker_database` out as "a different feature and a different
    table"; no v2 table for a sticker `file_id` pool exists yet, so every
    document is skipped and counted rather than silently dropped or written
    somewhere that doesn't fit.
    """
    out.skip(
        "stickerdatabase",
        _doc_id_str(doc),
        "no v2 table for a sticker file_id pool exists yet (see docs/contracts/fun_random.md)",
    )


# ----------------------------------------------------------------------------- registry

MAPPERS: dict[str, Callable[[Document, MappedRows], None]] = {
    "configs": map_configs,
    "rules": map_rules,
    "welcomes": map_welcomes,
    "users": map_users,
    "blacklist": map_blacklist,
    "groups": map_groups,
    "randomdatabase": map_randomdatabase,
    "stickerdatabase": map_stickerdatabase,
}

__all__ = [
    "MAPPERS",
    "map_blacklist",
    "map_configs",
    "map_groups",
    "map_randomdatabase",
    "map_rules",
    "map_stickerdatabase",
    "map_users",
    "map_welcomes",
]
