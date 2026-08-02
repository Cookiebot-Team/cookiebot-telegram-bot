# Contract: `util_everyone` (v1 -> v2)

Phase 2/6 of `/migrate-feature` for `/everyone`, bare `@everyone`, and QA's
`/ping everyone` spelling. QA: `../Cookiebot-QA/features/util_everyone.feature`.
FEATURE-MAP row: `util_everyone`. Spec/design:
`.specs/features/util_everyone/{spec,design}.md`. Files owned by this port:
`packages/cb-core/src/cb_core/members.py` (`MemberRef`, `roster`),
`packages/cb-core/src/cb_core/jobs.py` (new),
`packages/cb-core/src/cb_core/bot.py` (new, shared bot construction),
`packages/cb-gateway/src/cb_gateway/queue.py` (new),
`packages/cb-gateway/src/cb_gateway/handlers/everyone.py` (new),
`packages/cb-worker/src/cb_worker/jobs/everyone.py` (new),
`packages/cb-worker/src/cb_worker/main.py` (registration + startup bot),
`qa/features/util_everyone.feature`, `qa/test_util_everyone.py`, and the unit
and integration tests listed in the Tests table below.

## The prerequisite this port had to build first

Two mechanisms this codebase had been deferring for several feature slices
(HANDOFF §1 gaps 1 and 5) are load-bearing for `/everyone` and are built here
as general infrastructure, not as private helpers — see "The enqueue
mechanism" and "A worker that holds a bot" below.

## Phase 1 — where v1 lives

- Handler: `../COOKIEBOT-Telegram-Group-Bot/Bot/UserRegisters.py:97-146`, `everyone`.
- Dispatch: `../COOKIEBOT-Telegram-Group-Bot/Bot/COOKIEBOT.py:272-273` —
  `elif msg['text'].startswith(("/everyone", "@everyone")):` — reached from the
  group-message branch, **not** nested inside the `utilityfunctions` gate its
  neighbours share.
- Locale strings: `Bot/Static/locales/{eng,pt,es}/lib.json` — `everyone_no`,
  `everyone_len`, `everyone_call`. All three already live in
  `cb_core/locale_data/`.

## Phase 2 — v1 behaviour contract

| Aspect | v1 behaviour (file:line) |
|---|---|
| Triggers | `/everyone` and bare `@everyone` — one `startswith(("/everyone", "@everyone"))` check (`COOKIEBOT.py:272-273`) |
| Preconditions | reached from the group-message branch. **`utilityfunctions` is not checked** for this command, unlike its neighbours (`COOKIEBOT.py:272-273`) |
| Admin gate | rejected with `everyone_no` when `listaadmins` is non-empty **and** the sender has a `username` **and** that username is not in `listaadmins` **and** the message has no `sender_chat` (`UserRegisters.py:99-102`). Anonymous admins (`sender_chat`) pass. A sender with no username passes. An empty `listaadmins` — e.g. `getChatAdministrators` failed — skips the gate entirely. |
| Cooldowns / quotas | none |
| Success output | ① `sendChatAction typing` (`:98`) ② react `🫡` (`:111`) ③ one or more group messages, HTML: the **first** chunk is prefixed `Number of known users: {min(len(usernames), getChatMembersCount(chat_id))}\n` (`:112`, hardcoded English, never localised, first chunk only), then `@username ` per member, space separated ④ a private message to each resolved member: `everyone_call` with the chat title, carrying an inline "Show message" button linking to `https://t.me/c/{chat_id without the -100 prefix}/{message_id}` (`:139-146`) |
| Failure output | fewer than 2 names in members ∪ admins ⇒ `everyone_len`, no ping and no fan-out (`:107-110`). Non-admin ⇒ `everyone_no` and return (`:99-102`). |
| Chunking | manual against Telegram's 4096-char cap: append a new chunk when `len(current) + len(username) + 2 > 4096` (`:113-120`), inside a `try/except TypeError: pass` that is dead defensive code |
| Persistence | registry hygiene mid-loop: for each username, if the backend lookup returns ≠ 1 result **or** the live `getChatMember` status is `left`/`kicked`, `DELETE registers/{chat_id}/users` with `{"user": username}` and skip that member (`:128-135`) |
| Side effects | `time.sleep(0.1)` between DMs; each DM in a bare `try/except Exception: pass` (`:139-146`). Every 10th successful DM forwards the triggering message to the bot owner via `forwardMessage(ownerID, chat_id, msg['message_id'])` (`:137-138`). |
| External calls | Telegram: `getChatMembersCount`, `getMe`, `getChat`, `getChatMember` (once per member), `forwardMessage`, `sendMessage` ×N. Backend: `GET registers/{chat_id}` for the roster, then **one `GET users?username=` per member** (`:129`), then `DELETE registers/{chat_id}/users` for stale entries. |

### Backend surface being replaced

| v1 call | Java side | Mongo |
|---|---|---|
| `GET registers/{id}` | `RegisterResource.findById:38-42` -> `RegisterService.findById:33-35` | `registers`, `{id, users:[{user, date, accountId}]}` |
| `GET users?username=` | `UserResource.findAll:33-38` -> `UserService.findAll:26-38` -> `UserRepository.findByUsername:13` (derived, **unindexed**) | `users`, `{id, username, firstName, lastName, languageCode, birthdate}` |
| `DELETE registers/{id}/users` | `RegisterResource.deleteUser:72-76` -> `RegisterService.deleteUser:82-99` (`$pull`) | `registers` |

No batch endpoint exists in v1 — no `findByUsernameIn`, no array parameter, no
`$lookup`. The N+1 was structural in v1 and disappears in v2 because
`group_members` already carries the user id: `cb_core.members.roster(group_id)`
is one statement, `WHERE group_members.group_id = $1 AND left_at IS NULL`
joined to the `users` reference table, `ORDER BY user_id` — a single-shard
read (`qa/integration/test_everyone.py` asserts `Task Count: 1`).

## Defects — verdict per item

| id | Defect | Verdict |
|---|---|---|
| D-EV-1 | N+1 backend calls — one `GET users?username=` per member (`:129`) | **fix** — `members.roster` reads the whole roster in one query |
| D-EV-2 | Admin gate skipped when the admin list is empty | **fix** — see below |
| D-EV-3 | A caller with no username passes the gate regardless of admin status | **fix** — see below |
| D-EV-4 | `Number of known users:` header hardcoded English, first chunk only | **preserve** |
| D-EV-5 | Every 10th DM forwards the triggering group message to the bot owner | **drop** — see below |
| D-EV-6 | Dead `try/except TypeError: pass` around the chunk length check | **drop** |

### D-EV-2 / D-EV-3 — the gate now fails closed

v1's gate only rejects when *all four* hold: the admin list is non-empty, the
message carries `from.username`, that username is not in the admin list, and
there is no `sender_chat` (`UserRegisters.py:99-102`). Two independent bugs
follow from that conjunction: a failed `getChatAdministrators` call leaves
`listaadmins` empty, which skips the `len(listaadmins) > 0` check entirely and
turns `/everyone` into a free-for-all (**D-EV-2**); and a caller with no
`username` always passes regardless of admin status, because the condition
requires `from.username` to exist before it can even be compared (**D-EV-3**).
`/migrate-feature` Phase 2's rule is that a silent-failure bug gets fixed, not
preserved as a quirk, so v2 (`cb_gateway/handlers/everyone.py`) fails
**closed** instead: `ctx.is_admin` (`cb_core/admins.py:resolve_actor`) is
already `False` both for "confirmed non-admin" and for "no admin status could
be established", and `True` for an anonymous `sender_chat` sender — matching
v1's one deliberate bypass. One `if not ctx.is_admin` covers both v1 gaps at
once; there is no "admin list empty" or "no username" special case left to
reproduce, and an admin-resolution outage now denies the ping instead of
allowing it. This is a deliberate, user-visible behavioural divergence from
v1: a group whose `getChatAdministrators` call happens to fail will see
`everyone_no` in v2 where v1 would have let anyone ping everyone.

### D-EV-5 — the owner forward is dropped, not ported

v1 forwarded the triggering group message to a hardcoded bot-owner Telegram id
every 10th successful DM (`UserRegisters.py:137-138`,
`forwardMessage(ownerID, chat_id, msg['message_id'])`). This is undisclosed
exfiltration of group content: no group configuration controls it, no group
member is told it happens, and it has no user-facing trace anywhere in the
product. It is not preserved, not made configurable, and not replaced with an
equivalent — `cb_worker/jobs/everyone.py`'s fan-out loop has no owner id and
no `forwardMessage` call at all. This is a deliberate, permanent behavioural
divergence from v1, called out here because it removes an existing (if
covert) capability rather than fixing a bug nobody could observe.

## The enqueue mechanism (`cb_gateway/queue.py` + `cb_core/jobs.py`)

This is the general mechanism the codebase had been deferring — `calladms.py`
and `groupguardian.py` both say, in comments, that it does not exist yet
(HANDOFF §1 gap 5). `/everyone` is its first consumer, but it is written as
shared infrastructure for every future gateway → worker fan-out, and this is
its one canonical description — see `docs/site/content/docs/architecture.mdx`
§2 for a pointer back here rather than a second write-up.

**Surface** (`cb_gateway/queue.py`):

```python
async def enqueue(job: str, *args: object, **kwargs: object) -> bool
async def close() -> None
```

- One lazily created `arq.connections.ArqRedis` pool, built from the same
  Redis/Valkey DSN `cb_core.settings.get_settings().redis_dsn` already
  provides for the group-config pub/sub and the cooldown store — no second
  settings mechanism, no second URL (AGENTS.md §8).
- `enqueue` **never raises into the caller.** By the time a handler calls it,
  the user has already gotten their reply; a broker outage must not turn a
  successful reply into a 500 or a retried update. A failure — pool
  construction, `enqueue_job` raising — is caught, logged with `structlog`
  (`event="queue.enqueue", job=job, error=str(exc)`), counted, and swallowed;
  `enqueue` returns `False`. Callers that care whether the job landed can
  branch on the return value; `everyone.py` does not, because there is nothing
  useful to do differently on the reply path if it fails.
- One counter, `cb_gateway_enqueue_total{job,outcome}`, `outcome` in
  `ok|error`. `job` is a bounded set of string constants — never a group or
  user id (AGENTS.md §7 cardinality rule).
- Job names are shared constants in `cb_core/jobs.py`
  (`EVERYONE_FANOUT = "everyone_fanout"`), imported by both `cb-gateway` (at
  the enqueue call site) and `cb-worker` (`WorkerSettings.functions`), so a
  rename on one side cannot silently desynchronise the other — the failure
  mode without this would be a job sitting in the queue until arq's retry
  limit, with no signal at either call site pointing at why.
- `close()` is called from `cb_gateway/main.py`'s existing shutdown path, next
  to `cache.close_cache()`.

Tests: `packages/cb-gateway/tests/test_queue.py` — `enqueue` returns `True`
against a fake pool, returns `False` and logs when the pool raises, and never
propagates an exception out of either branch.

## A worker that holds a bot (`cb_core/bot.py`, `cb_worker/main.py`)

HANDOFF §1 gaps 1 and 5 both stalled on the same missing piece: `cb-worker`
could receive no work from the gateway (fixed above) and, separately, had no
way to talk to Telegram at all — it only ever ran cron jobs against Postgres.

Bot construction (`build_api_server`, `build_bot`) moved out of
`cb_gateway.bots` into `cb_core.bot`, so both services call the same function
rather than the worker importing a gateway module (design R3.2 — a worker
importing `cb_gateway` would be a layering violation `cb_gateway/main.py`'s
own docstring warns against). `cb_worker/main.py`'s `startup()` now builds one
`aiogram.Bot` via `build_bot(_primary_token(settings), settings)` and stores
it on `ctx["bot"]`; `shutdown()` closes its session. Critically, this goes
through the **same** `Settings`-driven endpoint resolution as the gateway,
including `platform_selfhosted_api`'s base URL (`cb_core.bot.build_api_server`)
— a worker that resolved its own bot independently could silently fall back to
`api.telegram.org` while the gateway (and every group's webhook) talks to a
self-hosted `telegram-bot-api` server, which is exactly the drift this
shared constructor exists to make impossible. `packages/cb-worker/tests/
test_startup.py` asserts the worker's bot session follows the self-hosted
setting.

`cb_worker/jobs/everyone.py`'s `everyone_fanout` is the first job to use
`ctx["bot"]`. **This unblocks two follow-ups, not built in this slice**
(design R3.3, open decision 5):

1. **`util_calladms`'s DM half** — the group ping is done
   (`docs/contracts/util_calladms.md`); DMing each admin needs exactly this
   enqueue + worker-bot pair and was left as a named follow-up when that
   contract was written.
2. **The captcha's 30-second unban** (HANDOFF §1 gap 1) — `expire_captchas`
   currently only deletes expired rows; v1 also banned, messaged, and
   scheduled a kick 30 seconds later. The worker can now hold a bot to do
   that; it does not yet.

## Phase 6 — parity table

| Aspect | Verdict |
|---|---|
| Triggers (`/everyone`, `@everyone`, `/ping everyone`) | **same intent, mismatch recorded** — `/everyone` and `@everyone` are byte-identical to v1; `/ping everyone` is QA's spelling and has no v1 equivalent at all, so it is not aliasable through `COMMAND_ALIASES` (that table maps one v2 canonical name to v1 spellings of *the same command word*, not to an unrelated two-word QA phrase). Recorded as a trigger mismatch in `docs/site/content/docs/feature-map.mdx`'s `util_everyone` row and in the feature file's own header comment; the acceptance step definitions send the real v1 trigger. |
| `utilityfunctions` not gating this command | **same** — v1's own dispatcher never checks it here either; preserving an absent gate is not a divergence |
| Admin gate — happy path | **same** — admin passes, non-admin with a resolvable status is refused, anonymous `sender_chat` passes |
| Admin gate — unresolvable status | **changed (bug fixed)** — D-EV-2/D-EV-3: v2 fails closed, v1 failed open |
| Roster source | **changed (intentional)** — one `members.roster(group_id)` query instead of v1's `GET registers/{id}` + N × `GET users?username=` (D-EV-1) |
| `everyone_len` threshold | **same** — fewer than 2 known usernames |
| `typing` action, `🫡` reaction | **same** |
| Ping text, first-chunk header | **same, byte-identical** — including the hardcoded English and the first-chunk-only placement (D-EV-4) |
| Chunk boundary arithmetic | **same, byte-identical** — `len(current) + len(username) + 2 > 4096`, off-by-two included |
| Dead `try/except TypeError` guard | **dropped (D-EV-6)** — a `str.len()` never raises `TypeError`; nothing observable changes |
| DM fan-out location | **changed (intentional)** — v1 DMs from the same handler thread that answered the group; v2 enqueues `jobs.EVERYONE_FANOUT` with scalars only and returns, fan-out runs in `cb-worker` (AGENTS.md §2 "nothing slow on the reply path") |
| Registry hygiene on `left`/`kicked` | **changed (intentional)** — v1 `DELETE`s the register row; v2 calls `members.mark_left`, so `first_seen_at` survives a rejoin (same policy `fun_ship`'s contract already set) |
| DM body, deep-link button | **same** — `everyone_call`, `Show message` label read verbatim from v1, same `https://t.me/c/{id}/{message_id}` construction |
| 0.1s pause between DMs | **same** |
| Each DM individually failure-suppressed | **same** — "blocked by user" is the routine outcome in both |
| Every-10th-DM owner forward | **dropped (D-EV-5)** — see above; no v2 equivalent |

## Citus notes

- `members.roster` is single-shard: `group_id` leads the predicate and `users`
  is a reference table, so the join is node-local (AGENTS.md §4.4).
  `qa/integration/test_everyone.py` asserts `Task Count: 1` alongside
  `qa/integration/test_citus_topology.py`'s existing assertions for other hot
  queries.
- The DM fan-out job re-reads `members.roster(group_id)` inside `cb-worker`
  rather than trusting a member list shipped through the arq payload (design
  R4.7 / open decision 2): the enqueued job carries only `group_id, chat_id,
  message_id, chat_title, lang` — small scalars — so a job that runs late
  DMs the membership as it stands *then*, and the broker payload stays small
  regardless of group size.

## Tests

| Layer | File |
|---|---|
| Unit — triggers incl. bare `@everyone`, `ping_chunks` boundary/header, `known` clamping | `packages/cb-gateway/tests/test_everyone.py` |
| Unit — `enqueue` success/failure/never-raises | `packages/cb-gateway/tests/test_queue.py` |
| Unit — deep-link URL, `left`/`kicked` -> `mark_left`, a raising send not aborting the loop | `packages/cb-worker/tests/test_everyone_fanout.py` |
| Unit — worker bot session follows the self-hosted base URL | `packages/cb-worker/tests/test_startup.py` |
| Unit — `roster` ordering, cache/degradation behaviour | `packages/cb-core/tests/test_members.py` |
| Integration — real Citus: `roster` ordering, excludes left members, single-shard plan | `qa/integration/test_everyone.py` |
| Acceptance — the two QA scenarios plus the net-new `everyone_len` scenario, enqueue asserted against a fake queue | `qa/features/util_everyone.feature`, `qa/test_util_everyone.py` |
