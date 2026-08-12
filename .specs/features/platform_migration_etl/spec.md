# platform_migration_etl — Specify

**Feature id:** `platform_migration_etl` · **Milestone:** M4 · **Kind:** state report
**Status:** `partial` — seven of eight v1 Mongo collections import fully and
idempotently; the remaining one is a reasoned, permanent skip, not a gap
anyone forgot.

This is not a build spec. It records what exists, what doesn't, and why.

## What is actually implemented today

`cb_worker.importer` (`packages/cb-worker/src/cb_worker/importer/`) moves the
Java backend's MongoDB into the v2 Citus schema, from a live server
(`CB_MONGO_URI`) or a `mongodump` directory (`CB_MONGO_DUMP_DIR`) —
`importer/source.py`. Driven via `python scripts/cb.py import-mongo --dry-run`.

- Seven collections have a working mapper and load fully:
  `configs`, `rules`, `welcomes`, `users`, `blacklist`, `groups`,
  `stickerdatabase` — `importer/mappers.py:141-335,366-390` (`map_configs` …
  `map_groups`, `map_stickerdatabase`). `stickerdatabase` is the newest of the
  seven: it lands in `sticker_pool` (migration `0009_sticker_pool.py`), the
  global reference table `x_sticker_autoreply` reads from — see "What is
  missing" below for why this one is a reference table and `randomdatabase`'s
  target could never be.
- Every write is an idempotent upsert on the natural key.
  `importer/loader.py`'s module docstring states the per-table
  `update_columns` contract precisely: which columns a re-run is allowed to
  overwrite (v1-sourced fields, since v1 is still the sole writer pre-cutover)
  and which are v2-owned lifecycle state a re-run must never touch (`groups.chat_type`/
  `skin`/`joined_at`/`left_at`/`tenant_id`, `*.created_at` timestamps that
  should only ever be set once by Postgres's own `DEFAULT now()`).
- `importer/runner.py` orders collections so the FK from
  `group_configs`/`group_rules`/`group_welcomes`/`group_admins` to `groups`
  is always satisfiable (`ensure_group_stubs`, inserted immediately before any
  child table write, regardless of which collections were actually
  requested), and a collection that raises is caught, logged and counted as a
  whole-collection skip rather than losing the report for the others.
- Verified end to end against a real dump and a real Citus cluster —
  HANDOFF.md:150-158: "a second run rewrites nothing and duplicates nothing."
- Three load-bearing conversions are unit tested: Mongo `_id` (always a
  string) parsed to a bigint Telegram id, unparseable ones skipped and
  counted rather than guessed; `threadPosts`'s `"9999"` sentinel converted to
  `NULL`; `stickerSpamLimit` (a Java `String`) converted to the `int` column —
  HANDOFF.md:160-165.

## What is missing

- **`randomdatabase` — every document is skipped, by design.**
  `mappers.map_randomdatabase` (`mappers.py:339-360`) skips and counts every
  row rather than writing anything. The reason is structural, not
  procedural: v1's `RandomDatabase.java` stores only a Telegram
  `{_id: chat_id, idMessage, idMedia}` pointer — never bytes, never a hash —
  and `media_objects.content_hash`/`blob_key`/`byte_size` are `NOT NULL`
  (`packages/cb-api/migrations/versions/0002_media_and_llm_usage.py:80-99`)
  because the entire media layer dedupes by content hash. Inventing a
  placeholder hash to satisfy the constraint would silently corrupt that
  dedupe for every future write to the same group — worse than not
  importing at all.
- No worker job triggers an import automatically. This is deliberate, not an
  omission — the import is meant to run manually and repeatedly while v1
  still serves, and once more at cutover to catch the delta
  (HANDOFF.md:144-158), not on a schedule.

## Why `stickerdatabase` could be finished and `randomdatabase` could not

Both collections looked identical at first glance — `{_id: <string>}`-shaped
Mongo pointers with no v2 destination — but the two `_id`s mean different
things, and that difference is the whole reason one now imports and the
other still cannot. `RandomDatabase.java`'s `_id` is a chat id; the actual
payload (`idMessage`, `idMedia`) is a *reference* to a still-live Telegram
message, never bytes, and `media_objects.content_hash`/`blob_key`/`byte_size`
are `NOT NULL` because the whole media layer dedupes by content hash —
writing a row here without ever downloading the referenced file would mean
inventing a hash, which would silently corrupt dedupe for every future write
to the same group. `StickerDatabase.java`'s `_id` is different in kind, not
just in content: it *is* the payload already, a Telegram sticker `file_id`
requiring no further download and no content hash at all — `sticker_pool`
(migration `0009`) has exactly one column, `file_id`, so there was never a
`NOT NULL` constraint standing between this collection and a working mapper,
only the missing table. `x_sticker_autoreply` built that table (a global
reference table, not a per-group one like `media_objects` — see the
migration's own docstring for why a sticker pool does not carry the same
cross-group leak risk a photo pool does, and why a per-group table would
have had nowhere to put a collection whose v1 rows carry no `group_id` at
all), which is what unblocked the mapper here.

## Why `randomdatabase` stopped there

The `randomdatabase` skip is already fully reasoned in two places —
`mappers.py`'s own docstring and HANDOFF.md:167-171 — and both predate this
document. `platform_migration_etl` is correctly `Status.PARTIAL`: 7 of 8
collections work and are verified against a real dump and a real cluster,
and the 1 that doesn't is skipped because writing it would violate a `NOT
NULL` constraint that protects real data, not because nobody got to it.
Nothing here is nobody-has-got-to-it — it's a decision with a paper trail.

## What it would take to finish, and what blocks it

- **`randomdatabase`**: a backfill worker job that downloads the message/file
  each pointer references from Telegram, computes the real content hash, and
  writes a genuine `media_objects` row — a job with real I/O and real
  network calls, not something that belongs in a pure ETL mapper
  (`mappers.py:339-355`'s own conclusion). Not blocked on anything external;
  it just hasn't been built, and `docs/contracts/fun_random.md`'s
  re-architecture notes are the design starting point.

## v1 equivalent

`RandomDatabase.java` (`../COOKIEBOT-backend`), pointer document
`{_id: chat_id, idMessage, idMedia}`; consumed in v1 by
`../COOKIEBOT-Telegram-Group-Bot/Bot/SocialContent.py:198-206`
(`random_media`, which forwards the still-live source message rather than
resending stored content — the same feature as v2's `fun_random`, which is
`Status.DONE` and does not need this import to work today). `StickerDatabase.java`
similarly, consumed by `SocialContent.py:208-222` (`add_to_sticker_database`/
`reply_sticker`, now `x_sticker_autoreply`, also `Status.DONE` and likewise
independent of whether this import has ever run — the importer backfills v1's
history into the same table the live handler writes to going forward).
