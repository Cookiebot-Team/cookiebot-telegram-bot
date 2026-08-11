# Handoff — M1 core moderation is ported, M2 fun is underway

Written for whoever picks this up next (human or agent). Read
[`AGENTS.md`](AGENTS.md) before writing code; this file says where we stopped and
what to do first.

---

## 0a. Most recent session (read before §0b)

Cutover tooling, two tenancy fields that had no reader, and — the bulk of it —
the features that were missing from the board rather than missing from the
code. PR #4.

### The board was understating the work by eight features

`docs/site/content/docs/feature-map.mdx` §4 listed eight v1 features with real
shipped code and no row in `scripts/spec.py`; `scripts/status.py`'s
`MISSING_V1_INVENTORY` named the same eight; nothing checked them, so they were
absent rather than red. `.specs/features/_pending/missing-spec-rows.md` had the
rows written and unapplied since the session that found them. They are applied
now: **62 features on the board, not 53.**

Then a second gap, found by comparing the `Cookiebot_functions.txt` that
`/commands` renders — ported byte-for-byte from v1 — against
`COMMAND_ALIASES`: six advertised commands had no handler at all. Four are
ported (`/analise`, `/desenterrar`, `/idade`, `/genero`, `/sorte`, `/reload` —
five, plus `/reload` which was not even in the eight). `/anything` and
`/drawingidea` are not: see §4.

### `/anything` is v1's catch-all, and that is a bigger job than it looks

`x_image_search` is not just `/qualquercoisa`. `COOKIEBOT.py:283-289` sends
**every unrecognised `/command`** to Google image search, with a per-user and a
global daily quota, an `avoid_search.txt` blocklist (49 entries, not yet ported
as package data) and safe-search keyed to the group's `sfw` flag. It also now
interacts with the dispatch gate below. Left planned deliberately.

### The dispatch gate, and the outage CI caught

`tenants.disabled_commands` was read in exactly one place — hiding a row from
`/commands` — so a "disabled" command still ran for anyone who typed it. It is
enforced at dispatch now, in an outer middleware that reads the already-parsed
command and costs nothing for non-commands.

The first version of it reused `/commands`' own predicate, where a command
absent from `command_catalog` means "not available". As a dispatch rule that
deleted every command the 29-row seed does not mention — `/giveaway`,
`/transcribe`, `/destroy`, every owner command. **It passed locally and failed
in CI**, because with no Postgres listening the gate's fail-open path ran
instead: the offline suite exercised the outage branch and never the rule.
Listing is an allowlist, dispatch is a denylist; `command_catalog.py` now has
both functions and says why. Three tests pin it, all of them database-free.

### Cutover

`python scripts/cb.py cutover` — preflight, schema, mongo, bucket, memes,
verify, in that order, with a `rich` progress bar per step and a summary table.
It composes the four existing tools rather than replacing them. Verified
against a `mongodump`-shaped source (18 rows, including the `"9999"` sentinel
and the unparseable-id skip) and a real 112 MB template copy; the second run
copies 0 and skips 801. `docs/site/content/docs/cutover.mdx` is the page.

Running the seeder for real is what turned up `fun_meme`'s phantom row: v1
deleted `Portuguese/photo_1893@29-11-2019_02-34-09.jpg` in `cf87b052` and never
regenerated its CSV, so v2 shipped a catalog entry whose bytes exist nowhere —
one guaranteed failure per seed run, and a template `/meme` could draw and then
fail to render.

### UAT has no bucket, and that is why /meme is inert there

`storageUri: memory://` is not a neutral default: media and templates land in a
process-local dict that a restart empties, and nothing reports it. The chart
now ships an optional MinIO, its bucket-creation Job, and the `AWS_*` env
obstore reads — `objectStorage.enabled`, off by default, on in the UAT values
in the infrastructure repo. The credential is referenced, never generated
(`scripts/cookiebot_env.py` writes it once).

**The bucket is deployed but the data move has not run.** That is the first
thing to pick up: §2 of this file plus `docs/cutover.mdx`.

### Tenancy

`llm_overrides` and `storage_prefix` had been on every tenant row since `0003`
with no reader anywhere. Both have one now — overrides merge field-by-field so
a tenant naming only `model` keeps the global everything-else, and a bad
override falls back and logs rather than costing the tenant the task. An empty
`storage_prefix` produces byte-identical keys, which is load-bearing:
`media_objects.blob_key` stores the string, not a formula.

## 0b. The session before that

Two features closed out and the database profiled end to end.
**46/53 done, 3 partial, 3 blocked, 1 planned.** Gate green:
`cb.py check` exit 0 (2 797 unit+acceptance), `test-integration` 179 passed,
`migrate-check` clean through the new `0008`.

### The state you inherit, before the two features

A large slice was already **uncommitted on disk** when this session started —
`core_botskins`, `core_musicdetection`, `fun_meme`, `x_distortion`,
`x_giveaways`, `util_birthday`'s daily broadcast, and all of
`x_owner_commands`' code. `scripts/spec.py` had been flipped for most of them
but `progress.json` had not been regenerated, so the board understated reality
by four features. If you are reading this before committing: that work is
still uncommitted, and it is now joined by this session's.

### `x_owner_commands` — the code was there, the paperwork was not

`handlers/owner.py`, `cb_core/ops.py`, `cb_worker/jobs/broadcast.py` and their
tests all existed and passed; the row said `PLANNED` and the contract the
handler's own docstring pointed at did not exist. Written:
`.specs/features/x_owner_commands/spec.md`,
`docs/contracts/x_owner_commands.md`, the feature page's prose, and the row
(now `DONE`, with `/groups` and `/unblacklist` added to its trigger list —
both dispatch in v1 and neither was listed).

Reading v1 for the contract turned up one defect the port had already fixed
without saying so, and one it had not noticed: `/leave`'s confirmation
(`COOKIEBOT.py:103`) interpolates `chat_id`, which in that branch is the
**owner's own private chat**, not the group being left — the one number the
message exists to report is the one it never contained. Both are D-OC-1/2 in
the contract.

### `x_webhub_login` — v1's login server, and D7 finally closed

New: `cb_api/auth.py` (Telegram's widget HMAC, the claim set),
`cb_api/keys.py` (the signing key), `cb_api/routers/login.py` (`/`, `/login`,
both `.well-known` documents), migration `0008` (`signing_keys`, a reference
table), the `webhub_*` settings block, `pyjwt[crypto]` on `cb-api`.
Contract: `docs/contracts/x_webhub_login.md`. **This is `cb-api`'s first real
endpoint** — the service was a health-check shell until now.

Four things worth knowing before touching it:

1. **D7 was worse than "the key resets on restart".** v1 generated the RSA key
   at module import and `run_api_server` starts **two gunicorn workers**
   (`Server.py:23-24,112`), so two keys signed tokens concurrently while
   `/.well-known/jwks.json` published only the key of whichever worker served
   that request. A consumer verifying against the JWKS failed about half the
   time, restart or no restart. v2 resolves one key: configured PEM, else one
   row in `signing_keys`, generated once and read back after the insert so a
   replica that loses the race adopts the winner's key.
2. **v1's `/login` could only ever accept its first bot token.**
   `validate_telegram_auth` does `auth_data.pop('hash')` on the caller's dict
   and the caller loops five tokens with that same dict (`Server.py:32,69-70`)
   — the second iteration sees no hash and returns `False`. Four of the five
   personas' users got `401` unconditionally. v2 does not mutate the payload;
   `test_the_payload_is_not_mutated` is the pin.
3. **`auth_date` enforcement is written and switched off**, and that is a
   decision someone has to make, not an oversight. v1 never checked it, so a
   captured widget payload mints tokens forever — but the shipped WebHub
   renews a session by **re-posting the payload it stored at first login**
   (`../COOKIEBOT-WebHub/src/lib/api/axios.ts`), so any real
   `CB_WEBHUB_AUTH_MAX_AGE_SECONDS` logs those sessions out. Closing the hole
   is a client change first. Recorded under "Open decision" in
   `.specs/features/x_webhub_login/spec.md`.
4. **`CB_WEBHUB_ISSUER` should be set before this is public.** Unset, the
   issuer is the request's base URL — v1's behaviour, and behind a proxy that
   is `X-Forwarded-Host`, so a caller picks the `iss` of the tokens this
   service mints. v2 cannot guess its own public URL, so the fallback is v1's
   and the setting is the fix.

`signing_keys` holds an unencrypted private key when no PEM is configured.
Deliberate, argued in the migration's docstring: the alternative for an
unconfigured deployment is not "no key at rest", it is D7 itself.

### The database is now profiled — `docs/db-profile.md`

`pg_stat_statements` over the whole test workload (28 856 statements), 920
`auto_explain` plans, and targeted `EXPLAIN (ANALYZE, BUFFERS)` against 200k
seeded users / 400k memberships. One real defect, fixed:

**All three birthday reads could not use `users_birthday_idx`.** It is partial
(`WHERE birthdate IS NOT NULL`) and none of them said so, so Postgres refused
it and fell back to a parallel sequential scan of the entire `users` table.
`birth_month` is `GENERATED ALWAYS AS (EXTRACT(MONTH FROM birthdate))` so the
implication is real, but the planner will not reason through a generated
column's expression. `/nextbirthday` measured **318 ms -> 38 ms**; the daily
sweep **681 ms -> 464 ms**. The fix is a redundant-looking `birthdate IS NOT
NULL` in `cb_core/birthdays.py`, and
`test_birthday_broadcast.py::TestPartialIndexIsReachable` is the regression —
it plans with `enable_seqscan = off` (propagated to the shards via
`citus.propagate_set_commands = 'local'`) and asserts the index name appears,
plus a third test proving the predicate-less statement fails that same
assertion so it cannot pass vacuously. **Check for this shape before adding
any partial index.** The schema's other three are reachable; verified.

The rest of the profile found nothing to fix: every reply-path statement is
`Task Count: 1` and index-backed, and the cross-shard statements that exist
are the ones already argued for in their contracts. What it does quantify is
that a cross-shard statement costs 10-30× the shard work it dispatches (86 ms
of coordinator for 8 × 0.5 ms of deletes), which is the number to have in mind
before putting one on a per-message path.

Two things left alone and written up rather than changed: `/ship`'s
`ORDER BY random()` reads a group's whole membership to pick two names (fine
at current sizes, quadratic-feeling at 10k members), and
`mediarestrict.enforce_media_restriction` compares a DB-generated `joined_at`
against the **application's** clock — a real hazard if an app host and the
database host disagree, but the six `core_mediarestrict` failures seen once at
the start of this session did not reproduce in four subsequent clean runs, and
changing a shipped feature's behaviour on a hunch is worse than recording it.

---

## 0c. The session before that

Four features ported: the publisher trio (all of v1's `Bot/Publisher.py`) and
`x_reverse_search`. **38/53 done, 5 partial, 3 blocked, 7 planned.**

### `x_reverse_search` — and a credential leak fixed

`/buscarfonte` (aliased `/searchsource`, `/buscarfuente`; none of the three
resolved before). The gateway keeps the utility gate, the reply requirement and
the file-id resolution; `cb_worker/jobs/reverse_search.py` calls SauceNAO.

**v1 leaks the bot token to a third party.** `reverse_search` builds
`https://api.telegram.org/file/bot{TOKEN}/{path}` and hands that URL to
SauceNAO, which fetches it — so the token lands in an external service's access
logs and any referer it forwards. Anyone holding it controls the bot. v2
downloads the bytes and uploads them; the URL is never constructed, and
`test_the_bot_token_is_never_sent_to_saucenao` asserts the outgoing request
carries a `file` part and no `url`, so reintroducing it fails the build. Now
FEATURE-MAP **D14**. If v1 is still running, that token should be considered
exposed and rotated.

### Three "planned" features are actually blocked — check before you start

`fun_partneredcons` and `x_custom_commands` were both marked planned and both
turn out to need the same private GCS bucket as `fun_death`. I found this only
because HANDOFF §1.7 says to verify rather than assume, and it is worth
repeating: **read the v1 source before scheduling a port.**
`x_custom_commands` is the worst of them — the command *names* are the
bucket's `Custom/` folder names (`Miscellaneous.py:23`), so without the export
there is not even a trigger list. One export of `cookiebot-bucket` now unblocks
four features: `Death/`, `Fight/English` + `Fight/Portuguese`, five
`Countdown/*`, and `Custom/`.

`scripts/spec.py`'s note on `x_custom_commands` ("the seed of tenant handler
packs") is still right, but the dependency runs the other way from what the M3
ordering implies: `platform_tenancy` should **not** wait on it.

### The publisher trio

```
ruff check + format --check   clean
mypy                          clean (112 source files)
pytest                        2522 passed, 45 skipped
migrate-check                 upgrade → downgrade → upgrade, all green
bench                         gate clear
scripts/cb.py check           exit 0
```

| Piece | Where |
|---|---|
| The schedule table (replaces `Publisher.db`) | migration `0005`, `cb_core/scheduled_posts.py` |
| Caption pipeline, keyboard, price conversion | `cb_core/publisher.py` |
| Pending-post cache (was a module dict) | `cb_core/pending_posts.py` |
| Render + fan-out, and the delivery cron | `cb_worker/jobs/publisher.py` |
| `/divulgar`, `/repost`, callbacks, reply relay | `cb_gateway/handlers/publisher.py` |
| The auto-forward prompt | `cb_gateway/handlers/postgetter.py` |
| `/deleteposts` | `cb_gateway/handlers/deletereposts.py` |
| Contracts | `docs/contracts/util_{postforwarder,postgetter,deletereposts}.md` |

Five things to know before building on it:

1. **Two registration-order constraints, both silent when wrong**, both now
   asserted in unit tests. `postgetter.router` must precede `fun_random.router`
   or every auto-forwarded ad also joins the group's random pool.
   `publisher.relay_router` must sit after `groupguardian`/`complaint` and
   before `chat_ai`, which is where v1's `elif` is — after `chat_ai`, the AI
   answers replies meant for a post's author.
2. **The publisher is inert until configured.** `CB_POSTMAIL_CHAT_ID`,
   `CB_POSTMAIL_CHAT_LINK` and `CB_APPROVAL_CHAT_ID` were hardcoded module
   constants in v1 (`Publisher.py:20-22`). Unset ⇒ `/divulgar` and `/repost`
   answer `publisher_unavailable`. v1's values are in `.env.example` for
   reference.
3. **Translation runs through `cb_core.llm.router()`'s new `translate` task**,
   not Google Cloud Translate. Same contract (pt + en captions, untranslated
   on failure — which v1 also does), different vendor, no new SDK. `cb-worker`
   now calls `init_llm()` at startup, which it never did before.
4. **Two statements deliberately fan out across shards** —
   `delete_by_requester` (the cancel) and `find_by_origin_title` (the reply
   relay). The rows a campaign owns are spread across every group it targeted,
   so no `group_id` predicate is correct. Both are index-backed single-table
   statements, both are commented, both are reached only from a human command.
5. **`price-parser` is a new dependency.** v1 parses ad prices with it
   (`Publisher.py:12,138`) and reimplementing its symbol/amount handling would
   silently change every converted caption.

**`pg_durable` was evaluated for these jobs and rejected.** Microsoft's
in-database durable-execution extension (PG 17/18 — this deployment is 17.2, so
the version fits). Four blockers: it is explicitly **preview**, with the
published image saying "do not use it in production"; it has no Citus story at
all, and every tenant table here is distributed; it needs
`shared_preload_libraries` plus a superuser background worker, which means a
custom image on top of stock `citusdata/citus:13.0`; and its steps are a SQL
DSL plus `df.http()`, while this codebase's jobs are aiogram calls, an LLM
router with budget enforcement, and Pillow. Its own README names the
disqualifier: don't use it when "you need arbitrary application logic that does
not map cleanly to SQL steps". **Stay on arq.** If more multi-step jobs appear,
the thing to evaluate is **DBOS** — same Postgres-backed checkpointed-replay
idea, but it decorates Python functions rather than requiring SQL; check its
Citus story first.

The evaluation did pay for itself: it surfaced a real bug in
`publisher_approve`. v1's `prepare_post` pops the pending post as its last act,
safe only because v1 ran it inline in a callback where nothing retries. In an
arq job it is not — a Telegram 5xx during the second Mural upload left the
retry with nothing to render, so it answered `publish_expired` and the campaign
vanished after the first caption had already posted. Fixed: read, and discard
only once the fan-out commits.

### Two pre-existing defects fixed on the way

- **`qa/conftest.py`'s per-scenario reset was silently disabled for two test
  modules.** It was an autouse fixture named `_clean`, and
  `qa/test_util_nextbirthday.py` and (initially) the new postgetter suite each
  defined their own autouse `_clean` — which shadows it for that module.
  Recorded Telegram calls survive into the next scenario, the admin caches keep
  the previous answer, and `group_configs` is never reseeded. Nothing errors;
  scenarios just start asserting against the one before them. Renamed to
  `_reset_scenario_state`, with the trap documented on the fixture. **This is
  the most likely cause of the six unreproducible `core_mediarestrict`
  failures** seen at the start of the session.
- **`qa/integration/test_llm_usage.py` compared the client's local date against
  a UTC rollup window**, so it was red on any host behind UTC between UTC
  midnight and local midnight.

And two tests that had quietly stopped testing anything:
`core_llm_provider.feature`'s "no configured model" scenario used the task name
`"translate"`, which this slice defined — it silently started exercising a
different error path. And `locales.missing_keys()` merged `lib.json` with
`cb.json`, so an assertion about **v1's** inherited drift changed every time
this bot added a string; it now reports on `lib.json` alone, with
`missing_cb_keys()` and an enumerated test covering v2's own deliberate
omissions (`publisher_ask_prompt` is absent from `es` on purpose — v1 prompts
Spanish groups in English).

---

## 0d. And the one before that

Verified green on this machine, last run:

```
ruff check + format --check   clean (201 files)
mypy                          clean (73 source files)
pytest -m "not integration"   1002 passed, 44 skipped
pytest -m integration         134 passed  (real Citus, via podman)
migrate-check                 upgrade → downgrade → upgrade, all green
bench                         cooldowns 1.85x compiled — gate clear
scripts/cb.py check           exit 0
```

**`fun_ship` is ported, and with it the member registry every remaining fun/util
feature was blocked on.** Nothing in v2 recorded who is in a group: v1 did it on
every message (`check_new_name`, `UserRegisters.py:64-88`) and v2 only had
`core_mediarestrict`'s join hook, which fires for people who join *after* the bot
— a small minority. New:

| Piece | Where |
|---|---|
| Registry repository | `cb_core/members.py` — `record`, `mark_left`, `random_usernames`, `count` |
| Its writer | `cb_gateway/handlers/members.py`, registered **first** in `build_router` (bookkeeping, always `SkipHandler`) |
| `/shippar` `/ship` `/shipp` | `cb_gateway/handlers/ship.py` |
| Contract | `docs/contracts/fun_ship.md` |

Three things worth knowing before you build on it:

1. **`group_members.joined_at` is now nullable (migration `0004`), and the join
   handler is its only writer.** "We have heard from this member" is not "we
   watched them join". Had the registry stamped `now()`, every long-standing
   member would have been media-restricted on their first message after a
   deploy — `core_mediarestrict` restricts anyone whose `joined_at` is inside the
   window, and its fail-open path for NULL is what makes that safe. The registry
   writes `first_seen_at` instead; `mediarestrict._record_join` fills `joined_at`
   in when the join really is witnessed.
2. **QA and v1 disagree about `/shipp @user1`.** The spec says the tagged user is
   shipped with a random second; v1 discards a lone argument entirely
   (`len(split()) >= 3`). Ported per v1, recorded in the contract, the feature
   file header and the FEATURE-MAP row.
3. `users` is a **reference table**, so every write replicates to every node.
   `cb_core.members` keeps a process-local identity cache to keep that off the
   per-message path; call `members.reset_cache()` in anything that writes those
   rows behind its back (the importer, tests).

**Mojo was evaluated against the Cython hot path and rejected**, with the numbers
in `docs/site/content/docs/architecture.mdx` §2 and the reproducible experiment
in `packages/cb-core/bench/mojo/` (`./setup_env.sh && ./run.sh`; not a build
target, not in CI). Short version: Mojo's compute is 5-13x faster than Cython's,
its per-call boundary is ~60 ns more expensive than a `cdef` method's, and these
modules do ~15 ns of work per call — so it loses on all three and would fail the
same 1.5x gate that already dropped `captcha`. It only wins if a whole
`getUpdates` batch crosses in one call *and* returns something small: building a
Python list from Mojo costs ~174 ns per item.

---

## 1. Where things stand

**M0 and M1 are complete bar one row; M2 and M3 are most of the way.** The
board is generated — `docs/site/content/progress.json`, rendered by the docs
site — and this section is the prose that goes stale first, so trust the
generated numbers over this paragraph if they disagree.

Verified on this machine, last run:

```
ruff check + format --check   clean (433 files)
mypy (four packages)          clean (147 files)
pytest -m "not integration"   2 676 passed, 9 failed *
migrate-check                 not run here (no local Postgres this session)
docs-sync --check             in sync with the spec
```

\* the nine are `qa/test_core_stickerspam.py` (8) and `qa/test_fun_complaint.py`
(1), which need Postgres and Valkey — `cb.py up` first, or read them as skipped.
CI runs them with the real infrastructure and is green.

Progress, generated by `python scripts/cb.py status`:

```
features   ████████████████████░░░░  52/62 done, 3 partial, 3 blocked, 4 planned
scenarios  ████████████████████████  153/63 of the v1 spec ported
```

Scenarios exceed 100% because each port added scenarios for v1 behaviour the QA
spec never covered — the spec described intent, not what v1 actually does.

### What landed this session

The three prerequisites (§3 of the previous handoff), then the ten features in
the order it recommended:

| Piece | Where | Note |
|---|---|---|
| Locale catalog | `cb_core/locales.py` + `locale_data/{en,pt,es}` | v1's files copied byte-identical (`diff -r` clean); found real v1 drift — `pt` is missing 4 keys, `es` 8 |
| Group config repo | `cb_core/group_config.py` | L1 → L2 → Postgres, pub/sub invalidation; replaces v1's manual `/reload` in five processes |
| Admin resolution | `cb_core/admins.py` | populates `group_admins`; **anonymous admins now succeed** instead of being told to disable a Telegram feature |
| `core_privacy`, `core_listcommand` | handlers | read-only ports |
| `util_config` | `handlers/config_menu.py` | 13 buttons, callback format and prompts reproduced verbatim |
| `core_rules`, `core_welcome` | handlers | first write paths; `/newrules` and `/newwelcome` are two-step reply flows in v1, not arguments |
| `core_groupguardian` | handlers | v1's captcha verified nothing — both buttons were free passes and the text check only counted digits |
| `core_stickerspam` | handlers | v1's counter was a per-process dict that never reset |
| `core_mediarestrict` | handlers | re-architected around `group_members.joined_at`; reactive, not native — see its contract |
| `util_doomlist` | handlers | CAS → local blacklist → burrbot, in v1's order, with timeouts and breakers |
| `core_setlang` | handlers | first-contact language derivation + `setMyCommands` |

Every port has a contract in `docs/contracts/<feature>.md` carrying its Phase 2
behaviour table (with v1 file:line) and its Phase 6 parity table.

### The join chain is order-dependent — read this before adding a handler

v1 dispatched a join through one `if/elif` chain, so exactly one branch ran: a
doomlist hit meant no captcha and no welcome, and a captcha meant no welcome
until it was solved. aiogram stops at the first router that handles an update,
which reproduces that — **but only if routers are registered in the right order
and every "not mine" path raises `SkipHandler` instead of returning**. The order
and the reasoning are in `cb_gateway/handlers/__init__.py`. Get it wrong and
nothing errors: one feature silently swallows every join.

### The database work is no longer unverified

The previous handoff listed migrations, integration tests and Citus topology as
never executed, because Docker was not running. They have now all run against a
real single-node Citus 13 (podman, `citusdata/citus:13.0`). Four things were
broken and are fixed:

| What | Symptom | Fix |
|---|---|---|
| Coordinator never registered as a worker | `create_distributed_table` → `replication_factor (1) exceeds number of worker nodes (0)` | `0001` now sets `shouldhaveshards` on the lone node; the old `EXCEPTION WHEN OTHERS` swallowed the failure |
| `computed_at = now()` in `DO UPDATE SET` | rollups → `functions used in the DO UPDATE SET clause of INSERTs on distributed tables must be marked IMMUTABLE` | `excluded.computed_at` in `cb_rollup_day` / `cb_rollup_llm_day`, `EXCLUDED.last_seen_at` in `MediaService` |
| Correlated `NOT EXISTS` from a reference table | media GC → `correlated subqueries are not supported when the FROM clause contains a reference table` | uncorrelated `NOT IN`, which Citus recursively plans |
| Whole migration ran in one transaction | downgrade → `cannot run function command because there was a parallel operation on a distributed table` | `migrations/env.py` sets `citus.multi_shard_modify_mode = sequential` |

Still not run here: `act` on the workflow (not installed; only needed if you
change `.github/workflows/ci.yml`).

### Importing v1's data

`cb_worker.importer` moves the Java backend's MongoDB into the v2 schema, from a
live server (`CB_MONGO_URI`) or a `mongodump` directory (`CB_MONGO_DUMP_DIR`) —
exactly one, never both:

```bash
docker compose --profile v1data up -d       # a Mongo to import from, if you want one
python scripts/cb.py import-mongo --dry-run
```

Every write is an upsert on the natural key, so it is safe to run repeatedly
while v1 still serves and again at cutover to catch the delta. Verified end to
end against a real dump and a real Citus: a second run rewrites nothing and
duplicates nothing.

Three conversions are load-bearing and unit-tested: every Mongo `_id` is a
**String** holding a Telegram id (unparseable ones are skipped and counted, never
guessed), `threadPosts`'s `"9999"` sentinel becomes `NULL`, and
`stickerSpamLimit` is a Java String against an `int` column. `language` is stored
verbatim (`"eng"`/`"pt"`/`"es"`) because `locales.resolve_language` normalises on
read and rewriting it would disagree with what `/config` writes today.

**`randomdatabase` is deliberately not imported.** v1 stored only a Telegram
`file_id` — no bytes, no hash — and `media_objects.content_hash`/`blob_key`/
`byte_size` are NOT NULL. A placeholder hash would corrupt the dedupe the whole
media layer rests on, so those documents are skipped with that reason in the
report. The random pool needs a backfill job that downloads from Telegram.

### Known gaps, deliberately left

1. **Captcha timeout does not kick — unblocked, still open.** `cb-worker`'s
   `expire_captchas` deletes expired rows; v1 also banned, messaged and
   scheduled a 30s unban. A newcomer who simply never answers is not removed.
   It needed the worker to hold a bot — `util_everyone` (gap 5, below) built
   that (`cb_core/bot.py` + `ctx["bot"]` in `cb_worker/main.py`,
   `docs/contracts/util_everyone.md`), so the mechanism now exists, but
   `expire_captchas` itself has not been changed to use it. Still a named
   follow-up.
2. **No private-chat dispatch — closed.** `.specs/features/private_dispatch/`
   built it: `cb_gateway/private_context.py` (`PrivateContext`/
   `private_context_for` — deliberately no `group_id` field, so a handler
   cannot pass it to `group_config`/`admins` and get a plausible-looking
   wrong answer for a chat that was never a group) and the
   `F.chat.type == ChatType.PRIVATE` handler pattern, mirroring the
   `F.chat.type != ChatType.PRIVATE` pattern already used everywhere else.
   `/commands`' DM branch was already ad hoc and correct
   (`core_listcommand.md`) — relocated to the shared pattern, not
   rebehaviored. `/privacy`'s DM branch had a **live bug**: no chat-type
   filter at all, so a DM `/privacy` fell through to `context_for` and
   queried `group_configs` (distributed on `group_id`) with a private
   chat's own id — fixed. Two remaining named follow-ups, not built:
   `/start`'s DM welcome screen (`pv_default_message`/`set_private_commands`,
   a separate, larger unit of work — different DM language convention,
   per-sender rather than hardcoded), and v1's owner-only ops commands
   (`/stop`, `/restart`, `/leave`, `/blacklist`, `/broadcast`, `/grupos`),
   which `.specs/features/private_dispatch/spec.md` recommends **not**
   porting at all — `os._exit`/`os.execl` process control and a single
   hardcoded owner id don't fit a stateless, horizontally-replicated,
   multi-tenant service. `util_birthday`/`util_nextbirthday`'s DM birthdate
   collection question is now settled (see gap 8) — it turned out not to
   need this mechanism at all, since v1's own collection code is dead.
3. **`/config`'s language button does not push `setMyCommands`.**
   `setlang.set_group_commands` exists and is tested; `config_menu.py` does not
   call it yet.
4. **`can_add_web_page_previews`** has no reactive equivalent in the media
   restriction port — see its contract.
5. **No gateway -> worker enqueue wiring — closed.** `util_everyone` built it:
   `cb_gateway/queue.py` (`enqueue`/`close`, one `arq` pool on the existing
   Redis DSN, never raises into a handler) and `cb_core/jobs.py` for the
   shared job-name constants. `util_calladms`'s DM-every-admin half used it
   next and is now ported too (`cb_worker/jobs/calladms.py`,
   `docs/contracts/util_calladms.md`); the captcha's 30s unban (gap 1, above)
   is the one remaining named follow-up.
6. **`randomdatabase` backfill** — see the import section above.
7. **`fun_death` is infrastructure-blocked, not just unstarted — and
   `fun_battle` shares the same blocker for two of its three shapes.** v1's
   image pools (`bloblist_death`, and `fun_battle`'s
   `bloblist_fighters_eng`/`bloblist_fighters_pt`) are live listings of the
   same private GCS bucket (`cookiebot-bucket`, different prefixes: `Death/`
   vs. `Fight/English`/`Fight/Portuguese`), never checked into the v1 repo,
   and this environment has no credential for it. `fun_death` is
   `Status.BLOCKED` in `scripts/spec.py` — `.specs/features/fun_death/` has
   the full evidence, the prerequisite (export the bucket's `Death/`
   prefix), and a design/tasks pair ready to execute once it lands.
   `fun_battle` is `Status.PARTIAL` instead of blocked outright: its
   two-people shape (explicit tags or `"random"`) needs no bucket at all and
   ships in this slice, with v1's `telegram.me`-scraping mechanism replaced
   by `cb_core.members.roster` + the Bot API's `getUserProfilePhotos` (design
   accepted in `.specs/features/fun_battle/spec.md` — no HTTP scrape, no
   OpenCV, no local temp files, which also fixed a real cross-request race
   that existed in v1). Its other two shapes reply v1's own
   `battle_no_picture` string until the `Fight/` prefix is exported — same
   prerequisite as `fun_death`'s, tracked together, not as a separate gap.
   `fun_meme` is suspected to have the same shape of blocker but has not
   been checked yet — do not assume, verify the same way before starting it.
8. **`util_birthday`'s daily, every-group broadcast — unverified, not
   built.** v1's `birthday()` serves two shapes that share a body: the
   manual, on-demand command (`/birthday`, built, `Status.PARTIAL` because
   of exactly this gap) and an unattended shape
   (`manual_chat_id=None`) that iterates every group and posts unprompted,
   with a pinned-message dedup check so it doesn't repost the same day
   twice. **Nothing in `../COOKIEBOT-Telegram-Group-Bot` calls `birthday()`
   that way** — no cron entry, no systemd timer, no scheduler of any kind
   found anywhere in the checkout. That is not evidence it doesn't happen in
   production — it could live in infrastructure config or a host-level cron
   entry entirely outside the three reference repos. **Someone with access
   to the live v1 deployment needs to confirm, before cutover, whether
   groups currently receive an automatic daily birthday post.** If they do,
   this is real, necessary work, not an optional enhancement — a `cb-worker`
   cron job reusing `util_birthday`'s own collage/roster/photo machinery.
   Full writeup: `docs/contracts/util_birthday.md`.

## 2. Resume in three commands

```bash
cd cookiebot-v2
python scripts/cb.py install
python scripts/cb.py up          # docker or podman, whichever you have
python scripts/cb.py check
```

You no longer have to remember `migrate`: every service converges the schema at
startup (`cb_core/migrations.py`). Run `cb.py migrate-check` anyway after
touching a revision — it is the only thing that exercises `downgrade`.

## 3. What the three shared pieces became

All built, all used by every handler below them:

- **`cb_core/locales.py`** — v1's `{eng,pt,es}` files copied verbatim into
  `cb_core/locale_data/`. `get(key, lang, **fmt)` for `lib.json`,
  `text(name, lang)` for whole files (`/commands`), `lines(name, lang)` for the
  pools (`death`, `sorte`, `ship_dynamics`, `answers`), `resolve_language()` for
  `pt-BR`-shaped codes. Never retype a string; add it to the data files.
- **`cb_core/group_config.py`** — `get_config(group_id)` / `set_config(...)`.
  Writes invalidate L2 and publish, so every replica drops its L1 copy. Defaults
  are v1's, from `Configurations.py:111` (the Java `Config.java` has none).
- **`cb_core/admins.py`** — `resolve_actor(bot, message)` returns
  `ActorCheck(user_id, is_admin, anonymous)`. Anonymous senders are admins:
  Telegram only allows `sender_chat` = the group for an admin.

Handlers reach all three through **`cb_gateway/context.py`**:
`context_for(bot, event)` → `ctx.config` / `ctx.lang` / `ctx.is_admin`, and
`t(ctx, key, **fmt)` for a localised string. Use it rather than the modules
directly.

## 4. What to port next

**This section was stale for a while — trust `python scripts/cb.py status`, not
prose.** `fun_random`, `util_embedder`, `fun_dice`, `fun_ship`, `fun_firecracker`,
`fun_complaint`, `util_everyone`, `util_calladms` (both the group ping and the
DM fan-out), `fun_battle`'s two-people shape, private-chat dispatch,
`util_youtube`, `util_birthday`/`util_nextbirthday`'s manual shape,
`x_conversational_ai` and `x_speech_to_text` have all landed since it was
written.

**`x_conversational_ai` + `x_speech_to_text` — done, one slice.** Both ship
together (`docs/contracts/x_conversational_ai.md`,
`docs/contracts/x_speech_to_text.md`): a new langchain-backed LLM provider
behind `cb_core.llm.router()` (only the `chat` task moved onto it —
`moderate`/`summarize`/`vision`/`transcribe` stay on the hand-rolled
providers, so `util_doomlist`'s live `moderate` calls are untouched), the
first-ever enforcement of `Tenant.monthly_llm_budget_usd` (a hard cap, in
both `complete()` and `transcribe()`, over budget refuses and an infra
failure fails open), v1's per-user AI-reply streak ported onto a new
`cache.bump_clamped` Lua primitive, and `x_speech_to_text`'s net-new
`/transcribe` command alongside the ported voice→AI sub-step. Neither
feature had a QA scenario anywhere — `qa/features/x_conversational_ai.
feature` (7 scenarios) and `qa/features/x_speech_to_text.feature` (5
scenarios) are authored, not ported. **Both acceptance suites need a live
Postgres for a non-obvious reason**: filters registered ahead of these
routers in `build_router` — `core_groupguardian`'s captcha-reply check
(ahead of `chat_ai`, queries the DB for every plain group text message) and
`core_mediarestrict`'s join-time lookup (ahead of `transcribe`, queries the
DB for every voice note) — raise instead of failing open when there is no
live pool, so the whole file skips cleanly rather than crashing when
Postgres is unreachable.

The next batch, in dependency order, now that the member registry exists:

| # | Feature | Why here |
|---|---|---|
| 1 | `util_everyone` | **done** — the registry it needed was built; batched roster read (`members.roster`, replacing v1's N+1), fan-out moved to `cb-worker` behind the new gateway→worker enqueue. Contract: `docs/contracts/util_everyone.md` |
| 2 | `util_birthday` — **partial**, `util_nextbirthday` — **done** | Both ship the manual command shape, reading `users.birthdate`/`birth_month`/`birth_day` populated by the Mongo importer (v1's own DM collection code turned out to be dead — its one call site is unreachable for a private chat — so there was nothing to port for live collection). `util_birthday` stays `partial`: the daily, every-group broadcast is an unverified, unresolved parity gap — see gap §1.8. Roster + `getUserProfilePhotos` for collage photos (`fun_battle`'s precedent, no scrape), Pillow for compositing (new dependency, nothing else in the tree does image compositing), `arq`'s `_defer_by` replacing v1's `threading.Timer` for the 900s follow-up. `docs/contracts/util_birthday.md`, `docs/contracts/util_nextbirthday.md`. |
| 3 | `fun_death` — **blocked, confirmed** | v1's image pool (`bloblist_death`, `Miscellaneous.py:17`) is a live listing of a private GCS bucket, never checked into `../COOKIEBOT-Telegram-Group-Bot`. Investigated this session: no `Death/` directory anywhere in the v1 checkout, no credential to the bucket anywhere in this repo or environment. `Status.BLOCKED` in `scripts/spec.py`; full evidence and the prerequisite (someone exports the bucket's `Death/` prefix) in `.specs/features/fun_death/spec.md`. `design.md`/`tasks.md` are written ahead of time so the port is mechanical once the export lands. |
| 3a | `fun_battle` — **partial, two-people shape done** | same bucket, different prefix (`Fight/English`/`Fight/Portuguese`) blocks its other two shapes — see gap §1.7. The shape that doesn't need the bucket (explicit tags or `"random"`) shipped this session, and dropped v1's `telegram.me` HTML scrape + a real temp-file race along the way (`docs/contracts/fun_battle.md`). |
| 3b | `fun_meme` | same shape of blocker suspected (a GCS-backed template pool), **confirmed partially different**: its `Bot/Static/Meme/` directory *does* exist in the v1 checkout (unlike `Death/`/`Fight/`), but at 112 MB — too large to vendor as package data the way `fun_complaint`'s 3.4 MB was. That sizing is `fun_meme`'s own design decision when it's picked up, not solved here. |
| 4 | `util_youtube` — **done** | search + reply moved to `cb-worker` (external API call, AGENTS.md §2.4); v1's `googleapiclient` call had no timeout at all, v2 bounds it. `docs/contracts/util_youtube.md` |
| 5 | `core_musicdetection` | first real media-processing port (audio fingerprinting); belongs in cb-worker |

Close the remaining gaps in §1 before or alongside these — the captcha timeout
one is user-visible, and the mechanism it needs (the worker holding a bot) now
exists.

## 5. Compatibility traps already identified

Do not rediscover these:

- **Trigger mismatches between QA and v1.** QA says `/config`, v1 ships
  `/configurar`; QA says `/deletereposts`, v1 ships `/deleteposts`; QA says
  `/ping everyone`, v1 ships `/everyone`; QA says `roll 6`, v1 ships `/dado` and
  `/d<N>`. All four are already in `COMMAND_ALIASES` — **both spellings must keep
  resolving**.
- **`/trex`** is specified in `fun_partneredcons.feature` but does not exist in
  v1. It is net-new, not a port.
- **20+ v1 features have no QA scenario at all** (giveaways, conversational AI,
  reverse search, distortion, owner commands…). See `docs/site/content/docs/feature-map.mdx` §4.
  Write the scenario as part of the port.
- **v1 defaults do NOT live in the Java backend.** The previous handoff said
  they did; `Config.java` is a bare Lombok class whose fields are all nullable,
  and a group the backend has never seen is served the tuple hardcoded at
  `Configurations.py:111`. Those numbers are now the SQL column defaults in
  migration `0001` and `GroupConfig.DEFAULTS`, and an integration test asserts
  the two transcriptions still agree — they had already drifted (a v2 group was
  getting a 120s captcha instead of 300s and no media restriction instead of
  600s).
- **Spanish aliases are easy to miss.** `/reglas`, `/nuevasreglas` and
  `/nuevabienvenida` all dispatch in v1 (`COOKIEBOT.py:264-268`) and were absent
  from `COMMAND_ALIASES` until this session. Grep the v1 dispatcher for all three
  spellings of every trigger you port.
- **Acceptance suites share one group.** `qa/conftest.py` reseeds `groups` and
  `group_configs` for `GROUP_ID` before every scenario, and clears the L1 *and*
  L2 caches, because the /config scenarios really do flip settings on it. Take
  update ids from `next_update_id()` — the dedupe middleware is real, and a
  reused id is dropped as a redelivery that reads as "the bot said nothing".

## 6. Decisions — all answered

Nothing here is open any more. Owner's answers, 2026-07-25:

1. **Locale storage** — **port the v1 flat files as-is.** Load
   `../COOKIEBOT-Telegram-Group-Bot/Bot/Static/locales/{eng,pt,es}/` into a frozen
   dict at startup, resolve by `group_configs.language`, fall back to `en`. Files
   stay the source of truth, so parity is a diff against v1 rather than an
   argument. A Postgres override layer for tenant branding comes later, on top —
   not instead. This unblocks §3.1.
2. ~~`textmatch` Cython margin~~ — dropped from `HOT_MODULES`. It measured
   1.48–1.55× against a 1.5 gate over eight runs; a marginal win is not worth a
   flapping CI gate. Two modules stay compiled (`cooldowns`, `dedupe`).
3. **OpenAI pricing** — **leave `None`.** No guessed numbers in
   `cb_core/llm/catalog.py`; OpenAI cost panels stay blank until someone supplies
   authoritative figures. Anthropic costs still report.
4. **Refusal fallback** — **stays on by default**
   (`CB_LLM_REFUSAL_FALLBACK=true`), production included.
5. **LICENSE** — **CC0 1.0**, matching v1. `LICENSE` is now in the tree, copied
   verbatim from `../COOKIEBOT-Telegram-Group-Bot/LICENSE`.
6. **Tenant billing** — **hard cap.** Over budget, LLM calls are refused and the
   user is told the quota is spent; no soft multiplier, no warn-and-continue.
   `docs/site/content/docs/multi-tenant.mdx` should be updated when the enforcement lands.

## 7. Keeping the status honest

The progress board and every feature page's frontmatter are generated, never
hand-edited. When a feature lands:

1. Flip its `status` in `scripts/spec.py`.
2. `python scripts/cb.py docs-sync` — regenerates `docs/site/content/progress.json`
   and rewrites each feature page's frontmatter. The prose in those pages is
   yours and is never touched.
3. `python scripts/cb.py check` — runs `status --check` (fails if a feature
   claims `done` without a ported, passing scenario) and `docs-sync --check`
   (fails if a page's frontmatter disagrees with the spec).

Those two checks are the guard against a site that drifts away from the code.

## 8. Environment notes from this session

- Python resolves to **3.14** (workspace requires ≥3.13); the compiled modules
  build as `cpython-314-darwin`.
- `uv` at `~/.local/bin/uv`. No Makefile — `scripts/cb.py` is the only runner.
- **podman, not Docker.** `cb.py up/down/selfhosted` pick whichever of
  `docker`/`podman` is on PATH. Two podman-specific things had to change:
  `docker-compose.yml` cannot use `${VAR:?message}` (podman-compose interpolates
  services behind inactive profiles too), and `act` needs `DOCKER_HOST` pointed
  at the podman socket — `cb.py workflow` prints how.
- `citusdata/citus:13.0` is **amd64 only**, so on Apple Silicon it runs emulated
  and everything database-shaped is ~20× slower. That is why the shard count is
  8 (`CB_CITUS_SHARD_COUNT`) and why the integration fixture uses a 60s command
  timeout: asyncpg's first array-typed statement on a connection triggers a
  recursive catalog introspection whose cost scales with the number of shard
  tables. Both are documented where they are set.
- `act` not installed.
- The **acceptance suite now uses real infrastructure** where the behaviour is
  the infrastructure: Postgres for `/rules`, `/newwelcome`, media restriction and
  the doomlist's blacklist, and Valkey (database index **15**, flushed per
  scenario) for the sticker counter. Both skip cleanly when unreachable, so
  `cb.py test` still works offline — but a full green run needs `cb.py up`.
- Compiled `.so`/`.c`/`.html` artifacts are gitignored — after a fresh clone run
  `cb.py cython` if you want the compiled path.
- A git repository since three sessions ago, on
  `github.com/Cookiebot-Team/cookiebot-telegram-bot`. Work goes on a branch and
  through a PR; `main` is what the UAT Argo Application tracks for the chart, so
  a chart change is live the moment it merges.
- **CI is not a formality here.** The acceptance suite runs against a real
  Postgres and Valkey in CI and against neither on a laptop, and the two
  disagree in exactly the places that matter: this session shipped a dispatch
  gate that was green offline and deleted half the bot's commands with a
  database attached (§0a). If a change touches anything that reads the database
  on the dispatch path, either run `cb.py up` first or wait for CI before
  believing a green run.
