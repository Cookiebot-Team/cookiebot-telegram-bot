# Contract: util_doomlist (v1 -> v2)

Phase 2/6 of `/migrate-feature` for the join-time ban gate. QA:
`../Cookiebot-QA/features/util_doomlist.feature`. FEATURE-MAP row: `util_doomlist`,
status `⚠ 2 external deps in join hot path`.

v1 handlers: `check_cas`/`check_banlist`/`check_banlist_public`
(`../COOKIEBOT-Telegram-Group-Bot/Bot/GroupShield.py:193-229`), dispatched from
`../COOKIEBOT-Telegram-Group-Bot/Bot/COOKIEBOT.py:142` as part of the join
`elif` chain. Backend read via `get_request_backend`
(`../COOKIEBOT-Telegram-Group-Bot/Bot/universal_funcs.py:96-104`).

## Scope decision: what is and is not "doomlist"

`GroupShield.py:172-191` defines `check_human` (kick a joiner with no username
**and** zero profile photos — a bot-suspicion heuristic) in the same file,
immediately before the three functions this port covers, and it is chained
into the *same* `elif` at `COOKIEBOT.py:142`:

```python
elif check_human(cookiebot, msg, chat_id, language) or check_cas(cookiebot, msg, chat_id, language) or check_banlist(cookiebot, msg, chat_id, language) or check_banlist_public(cookiebot, msg, chat_id, language):
```

`check_human` is **not** ported here. `docs/FEATURE-MAP.md`'s own `util_doomlist`
row cites only `check_cas`/`check_banlist`/`check_banlist_public`; "listed on the
Doomlist" (the QA spec's own wording) describes a user matching a *list*
(CAS/blacklist/public raid feed), not "has no username or avatar yet". It belongs
wherever `core_groupguardian`'s bot-suspicion heuristics land — a different
feature, different owner, different QA scenario.

Also **not** ported: the `(funfunctions or is_alternate_bot) and random.randint(1,
10) == 1` "silence_scammer.jpg" photo `COOKIEBOT.py:143-145` sends after *any* of
the four checks fires. It is dispatcher-level cosmetic flair shared across all
four checks (including the excluded `check_human`), needs a static asset this
task does not own, and has no bearing on "was the listed user prevented from
joining" — the actual acceptance criterion. Same call `core_welcome.py` made
about the pixel-art welcome card.

## Phase 2 — v1 behaviour contract

| Aspect | v1 behaviour (file:line) |
|---|---|
| Trigger (join) | Telegram's `new_chat_members`; v1 reads only the deprecated singular `new_chat_participant` (`== new_chat_members[0]`), same quirk `core_welcome.py` already documents. Only the first joiner in a batch is ever checked. |
| Precondition: self-join only | The whole check chain lives behind `elif msg['from']['id'] != msg['new_chat_participant']['id']: ... else: welcome_message(...)` at `COOKIEBOT.py:136`, i.e. **the checks only run when the joiner added themself** (`msg['from']['id'] == new_chat_participant['id']`, true for invite-link / "join group" joins). If an existing member *adds* someone else, v1 skips straight to `welcome_message` — a blacklisted user added by another member is never checked. Preserved exactly; see "Known defects" below for why this is not "fixed". |
| Precondition: not the bot itself, not another bot | `new_chat_participant['id'] == myself['id']` is handled earlier (bot-onboarding, `COOKIEBOT.py:122`) and never reaches this chain. A self-joining bot account is not a shape v1's `elif` models (the `is_bot` branch only exists inside the *not-self-join* arm, `:137-139`); this port defers rather than guesses. |
| Feature gate | **None in v1** — the chain at `COOKIEBOT.py:142` is unconditional for every self-joining, non-bot user. v2 adds `group_configs.doomlist_enabled` (migration 0001, `NOT NULL DEFAULT true`) as an opt-out that did not exist in v1; default `true` reproduces v1's always-on behaviour for every existing group (`docs/contracts/group-config.md`'s own note on this column). |
| Order of checks | `check_human() or check_cas() or check_banlist() or check_banlist_public()` — Python `or` short-circuits left to right. Excluding `check_human` (see Scope above), this port preserves the remaining relative order: **CAS -> local/backend blacklist -> public raid list (burrbot)**. This matters observably: a user listed on more than one source sees the *first* matching source's text, so reordering "cheapest first" would change what a doubly-listed user is shown. |
| Check 1: CAS | `GroupShield.py:193-204`. `GET https://api.cas.chat/check?user_id={id}`, `timeout=2` (no `verify=False` here — this one specific call in v1 already keeps TLS verification on; `verify=False` only afflicts the backend calls, D2). Response body is JSON; hit == `bool(json.loads(r.text)['ok'])`. Any exception (`except Exception`) is swallowed and treated as **not listed**. |
| Check 1 hit action | `ban_and_blacklist(cookiebot, chat_id, user_id)` (`universal_funcs.py:315-318`): `POST blacklist/{id}` against the Java backend (persists the hit globally), **then** `cookiebot.kickChatMember(chat_id, user_id)` — no `until_date`, i.e. Telegram's permanent-ban semantics (`kickChatMember`/`banChatMember` are the same Bot API method under telepot's old name). Then `i18n.get("ban_cas", lang=language)` sent via `send_message` (non-reply). |
| Check 2: local/backend blacklist | `GroupShield.py:206-215`. Three independent sub-checks, any one hit blocks: (a) `GET blacklist/{id}` -> `is_blacklisted['id'] == str(user_id)`; (b) if the joiner has a Telegram username, `GET blacklist/username/{username}` -> `is_blacklisted_username['id'] == username`; (c) `any(ch in fullname for ch in ['卐','ζ','𝛇'])` where `fullname = f"{first_name} {last_name}"` if `last_name` present else `first_name` — **no network call**, a pure string check on every self-joining user regardless of (a)/(b). Both backend reads go through `get_request_backend` (`universal_funcs.py:96-104`): `requests.get(f'https://backend.cookiebotfur.net/{route}', auth=HTTPBasicAuth(...), verify=False, timeout=60)` — **FEATURE-MAP D2** (TLS verification disabled) applies to this call, not to CAS or burrbot. |
| Check 2 hit action | `cookiebot.kickChatMember(chat_id, user_id)` directly (**no** `ban_and_blacklist`, no re-persistence — a user already in the blacklist, or merely matching a forbidden glyph, is not written anywhere new). `i18n.get("ban", lang=language)` sent via `send_message`. |
| Check 3: public raid list (burrbot) | `GroupShield.py:217-229`. `POST https://burrbot.xyz/noraid.php`, body `data={"id": str(user_id)}`, **no `timeout=` at all** — `requests` has no implicit timeout, so a stalled connection blocks this thread forever. The response body is malformed JSON with doubled quotes (observed in production; the code works around it rather than treating it as an error): `json.loads(r.text.replace('""', '"'))['raider']`. Any exception -> **not listed**. |
| Check 3 hit action | `cookiebot.kickChatMember(chat_id, user_id)` directly, no persistence. `i18n.get("ban", lang=language)` sent via `send_message` — **the same text as check 2**; only the CAS hit gets its own string. |
| Success output (CAS hit) | English: `"Banned the new user for <b> being flagged by the anti-spam system CAS (https://cas.chat/) </b>"` (`Static/locales/eng/lib.json:11`, key `ban_cas`). pt/es equivalents exist; all three ported verbatim into `cb_core/locale_data/*/lib.json`. |
| Success output (blacklist / burrbot hit) | English: `"Banned the new user for <b> being reported in other chats </b>"` (key `ban`, same file, line 12). |
| No hit | Falls through to `captcha_message` (if `captchatimespan > 0` and the bot is an admin) or `welcome_message` (`COOKIEBOT.py:147-150`) — this feature does nothing observable. |
| Persistence | Only the CAS hit writes anywhere: `POST blacklist/{id}` (Java backend, Mongo `blacklist` collection). v2: `INSERT INTO blacklist (subject_id, kind, reason, source) VALUES ($1,'user',...,'cas') ON CONFLICT (subject_id) DO NOTHING` — the reference table migration 0001 already creates for exactly this purpose (`source` column's own comment lists `cas` as a value). |
| Side effects | None beyond the kick + one `sendMessage`. |
| External calls | `api.cas.chat` (2s timeout, TLS verified), `burrbot.xyz` (no timeout — a defect), and (in v1 only) the Java backend for the blacklist read (60s timeout, TLS **not** verified, D2). v2 replaces the backend read with a local reference-table query — no network call, no breaker needed for it (see "v2 architecture" below). |
| Admin / known-member exemption | **None, and none is possible.** This handler only ever runs for a brand-new join (`new_chat_members`), so the joiner cannot already be a group admin of *this* group, and v1 applies no "already known elsewhere" exemption either — every self-join is checked regardless of any global role. |
| Known defects | (1) A member added by an existing member (not self-joining) bypasses every check, including a global CAS/blacklist hit — preserved, since "fixing" it would mean gating on `from_user` in a way v1 never did and no QA scenario asks for. (2) Check 2 and check 3 share one message string, so a user cannot tell from the text alone whether they were blocked by the local blacklist or by burrbot — preserved (cosmetic, v1-native). (3) burrbot has no timeout at all — **this port fixes it** (AGENTS.md: "no bare `httpx.get` with no timeout" is a hard rule, not a preference), see "Fail-open reasoning" below. |

## v2 architecture: breaker, timeouts, TLS, fail-open

This is the first ported feature with external dependencies on the join path, so
the decisions here set precedent.

- **Every outbound call has an explicit timeout.** CAS keeps v1's own `2.0s`
  (`httpx.Timeout(2.0)`). Burrbot gets an explicit `2.0s` too — v1 had none,
  which is a real defect (a stalled TCP connection to a third-party PHP script
  would hang that request thread forever); the same 2s budget is used because
  both are best-effort join gates of equal importance, and a slower value on
  either would let a single flaky vendor delay every join in every group.
- **TLS verification stays on for both.** Neither v1 call ever set `verify=False`
  (that defect, D2, is specific to `get_request_backend`, which this port does
  not call — see below). `doomlist.py`'s `httpx.AsyncClient()` uses httpx's
  default (`verify=True`); no exception is requested or needed for either host.
- **`cb_core.breaker.Breaker`** (default `threshold=5, cooldown=30.0`), one
  instance per dependency (`_cas_breaker`, `_burrbot_breaker`), same pattern as
  `cb_core.llm.router`'s per-provider breakers. When a breaker is open,
  `allow(now)` returns `False` and the call is skipped entirely — no request
  attempted, and the join is **not** blocked for that reason (see fail-open).
- **Metrics**: `external_dep_up.labels(dep="cas"|"burrbot").set(1|0)` and
  `external_dep_duration.labels(dep=..., outcome="ok"|"error").observe(...)` per
  attempt, matching `metrics.py`'s existing label vocabulary (`dep`, `outcome`).
  No `group_id`/`user_id` label anywhere (AGENTS.md §7).
- **The local blacklist check needs no breaker.** v1's equivalent read went over
  HTTP to the Java backend (`verify=False`, `timeout=60` — D2, quoted above);
  v2 has its own copy of that data in the `blacklist` reference table (migration
  0001), replicated to every node, so this becomes one in-process `SELECT`
  against Postgres, already timeout-bounded by `pg_command_timeout` and already
  covered by `AGENTS.md §4`'s query rules. Nothing to fail open on beyond what
  `cb_core.db` already does (a DB outage there fails the whole message path, same
  as every other DB-backed handler in this codebase, e.g. `group_config`'s
  documented "must never break a reply" fallback for *reads* it can default —
  there is no sane default for "is this exact id banned", so a lookup failure
  here surfaces the same as any other handler's DB failure, not specially).
- **Username lookup, adapted to the v2 schema.** v1's `blacklist/username/{username}`
  backend query has no v2 equivalent field (`blacklist.subject_id` is `bigint`
  only, migration 0001 — no username column, and this port does not own that
  migration). v2 joins the reference `users` table instead:
  `blacklist b JOIN users u ON u.user_id = b.subject_id WHERE lower(u.username) =
  lower($username)`. This is exact when the blacklisted account's username is
  already recorded in `users` (true for anyone the bot has ever seen post), and is
  the only way to express "blacklisted by username" without a schema change this
  task is not scoped to make — recorded as a finding, not silently narrowed.

### Fail-open reasoning (why a down dependency never blocks a join)

Both CAS and burrbot fail open: a timeout, connection error, non-2xx, or
malformed response body is treated as **"not listed"**, exactly like v1's own
bare `except Exception: return False`. This is not a new decision so much as
formalising what v1 already did, plus fixing the one place v1 forgot to (burrbot's
missing timeout). Reasoning, made explicit because AGENTS.md demands it:

1. **A join must never hang.** AGENTS.md §4 rule 4 ("nothing slow on the reply
   path") and rule the task brief adds specifically for this port both point the
   same way: a webhook handler that blocks on a third party risks Telegram
   redelivering the update and the gateway falling behind for every group, not
   just the one joining member.
2. **A join must never be blocked because a third party was unavailable.**
   Failing *closed* (treating "CAS didn't answer" as "banned") would let an
   unrelated outage turn `util_doomlist` into "nobody can join any group" —
   a strictly worse failure mode than the one it exists to prevent (a handful of
   known bad actors getting through while CAS/burrbot are down, same exposure
   window v1 always had).
3. **The breaker bounds the cost of a real outage.** Five consecutive failures
   open the breaker for 30s; every join during that window skips the network
   call for that dependency entirely (a few milliseconds of `allow()`, not a
   timeout), rather than every single join re-paying the full 2s timeout.
4. **The local blacklist check is unaffected by either breaker.** Because it is
   a direct Postgres read (no HTTP, no breaker) checked independently of CAS and
   burrbot, a genuinely dangerous, already-known-bad account (or one persisted by
   an earlier CAS hit) is still blocked even while both external services are
   down — only *newly*-CAS-flagged or *newly*-burrbot-flagged accounts get
   through during an outage, and only for its duration.

## Files changed

- `packages/cb-gateway/src/cb_gateway/handlers/doomlist.py` — the handler.
- `qa/features/util_doomlist.feature` — v1 QA scenario, verbatim, plus new
  scenarios for CAS/burrbot/forbidden-chars/gate-off/non-self-join/dependency-down.
- `qa/test_util_doomlist.py` — acceptance step definitions.
- `packages/cb-gateway/tests/test_doomlist.py` — unit coverage: breaker
  behaviour, timeout handling, response parsing (including the malformed-quote
  burrbot body), fail-open on every failure mode.
- `qa/integration/test_doomlist_blacklist.py` — the local blacklist/`users`-join
  query against a real database.
- This file.

## Wiring note (not this task's file ownership)

`doomlist.router` must be registered in `handlers/__init__.py`'s `build_router()`
**before** `welcome.router` (and before whatever router ends up owning
`core_groupguardian`'s captcha), because v1 only reaches welcome/captcha when
none of the ban checks fired (`COOKIEBOT.py:142`'s `elif` chain). This handler
raises `aiogram.dispatcher.event.bases.SkipHandler` on every non-hit path
(disabled gate, non-self-join, bot account, no list matched) specifically so a
later router still receives the event — mirroring the same ordering problem
`docs/contracts/core_welcome.md` already flags from the other side. Until this
router is registered, `qa/test_util_doomlist.py`'s scenarios exercise the real
handler logic but cannot pass end to end against the shared `dispatcher` fixture
— the same documented, accepted state as `qa/test_core_rules.py` and
`qa/test_core_welcome.py` for their own routers.

## Phase 6 — parity table

| Aspect | Result |
|---|---|
| Self-join precondition | same |
| Bot-itself / bot-account precondition | same |
| Check order (CAS -> local blacklist -> burrbot) | same |
| CAS endpoint, timeout, response field (`ok`) | same |
| Burrbot endpoint, request shape, malformed-JSON workaround, response field (`raider`) | same |
| Burrbot timeout | changed (bug fixed) — v1 had none (hang risk); v2 sets 2.0s, matching CAS's budget |
| TLS verification | same — both v1 calls already verified; v2 keeps `verify=True`, no exception |
| Local blacklist by id | same (Postgres reference table instead of an HTTP call to the same data) |
| Local blacklist by username | changed (intentional, schema-forced) — joins through `users.username` since `blacklist` has no username column in the owned migration; exact when the account has ever been seen, recorded as a finding |
| Forbidden-character check | same, verbatim glyphs |
| Action on hit (ban_chat_member / kick) | same (Telegram's old `kickChatMember` and current `banChatMember` are the same permanent-removal method) |
| CAS-hit persistence to blacklist | same (v1: Java backend `POST`; v2: local reference-table insert) |
| Local-blacklist / burrbot hit persistence | same (none) |
| User-facing text (`ban_cas`, `ban`) | same, ported verbatim from the existing locale catalog |
| `doomlist_enabled` gate | new (v2-only), default `true` = v1's always-on behaviour |
| Circuit breaker on CAS/burrbot | new (v2-only) — v1 had none; does not change the happy-path outcome, only bounds outage cost |
| Non-self-join bypass (existing member adds a blacklisted user) | same (preserved defect, see "Known defects") |
| `check_human` heuristic | not ported here — different feature, see "Scope decision" |
| `silence_scammer.jpg` cosmetic photo | not ported — cosmetic, different concern, needs an asset this task doesn't own |
