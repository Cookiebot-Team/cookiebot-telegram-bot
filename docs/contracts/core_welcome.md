# Contract: core_welcome (v1 -> v2)

Phase 2/6 of `/migrate-feature` for the join-greeting + `/newwelcome` pair. QA:
`../Cookiebot-QA/features/core_welcome.feature`. FEATURE-MAP row: `core_welcome`,
status `✅`.

v1 handlers: `welcome_message`/`substitute_user_tags`/`welcome_card`
(`../COOKIEBOT-Telegram-Group-Bot/Bot/GroupShield.py:38-171`), `new_welcome_message`/
`update_welcome_message` (`../COOKIEBOT-Telegram-Group-Bot/Bot/Configurations.py:253-267`),
dispatched from `../COOKIEBOT-Telegram-Group-Bot/Bot/COOKIEBOT.py:121-150` (join
event) and `:264-265`, `:290-292` (command + reply).

## Phase 2 — v1 behaviour contract

| Aspect | v1 behaviour (file:line) |
|---|---|
| Triggers (`/newwelcome`) | `msg['text'].startswith(("/novobemvindo", "/newwelcome", "/nuevabienvenida"))` — `COOKIEBOT.py:264`. All three aliases (`newwelcome`, `novobemvindo`, `nuevabienvenida`) are registered in `cb_core/textmatch.py:COMMAND_ALIASES` (confirmed present as of this port — an earlier draft of this contract flagged `nuevabienvenida` as missing; it was added by another agent working on that file in parallel). |
| Trigger (join) | Telegram's `new_chat_members` array; v1 reads it only through the **deprecated singular** `new_chat_participant`/`new_chat_member` fields, which Telegram still sends alongside the array set to `new_chat_members[0]` for backward compatibility (verified against `telepot` 12.7's `all_content_types` ledger, which lists `new_chat_member` and has no `new_chat_participant` key of its own — the value telepot reads is passed through raw from Telegram's JSON). **Only the first joiner in a batch join is ever processed** by any of v1's join-handling code; the rest are silently ignored. See "multiple joiners" row below. |
| Preconditions (`/newwelcome` command) | **None.** `new_welcome_message` (`Configurations.py:265-267`) sends the prompt to *anyone* who types the command — no admin check at the entry point. The admin check happens only when someone replies to that prompt (`update_welcome_message`). |
| Preconditions (the reply that sets it) | `str(msg['from']['id']) not in listaadmins_id and 'sender_chat' not in msg and int(msg['from']['id']) != ownerID` -> rejected (`Configurations.py:254`). The reply is only captured at all if `msg['reply_to_message']['text']` equals the exact prompt string **and** the reply doesn't start with `/` (a `/`-prefixed reply falls into the command dispatch `if` instead of the reply-matching `elif`, `COOKIEBOT.py:186,290`) — so replying with e.g. `/hello` never sets the welcome message, it gets treated as an (unrecognised) command. |
| Cooldowns / quotas | None. |
| Success output (prompt) | Exact text, **not localised** (always English, regardless of group language): `"If you are an admin, REPLY THIS MESSAGE with the message that will be displayed when someone joins the group.\n\nYou can include <user> to be replaced with the user name"`, sent as a reply to the `/newwelcome` message. |
| Success output (save confirmation) | `react_to_message(msg, '👍')` (a reaction on the admin's reply), then `"Welcome message updated! ✅"` as a reply to that same message, then the bot's own original prompt message is deleted (`Configurations.py:261-263`). |
| Failure output (reply, not admin) | `"You are not a group admin!"` — **hardcoded English, not localised** (no `language=` kwarg passed to `send_message`, whose default `language="pt"` skips the `translate()` call; `Publisher.py`'s three-language variant of this string is a *different* feature, `/repost`/`/deleteposts`, not this one). |
| Success output (join, default text) | No group_welcomes row (`'error'=="Not Found"` or key missing): `i18n.get("welcome_user", user=<chat title>)` if the update carries a chat title (always true for a group), else `i18n.get("welcome")`. Ported verbatim as `welcome_user`/`welcome` in `cb_core/locale_data/*/lib.json`. |
| Success output (join, custom text) | The stored `message` with literal `\n` unescaped to a real newline, then **placeholder substitution** (table below). Sent via `send_photo(..., caption=welcome, reply_markup=<Rules button>)` if the pixel-art welcome card renders; **on any exception in card rendering, the actual fallback that fires 100% of the time in a headless/no-CV2 deployment** is `send_message(cookiebot, chat_id, welcome)` — a **plain, non-reply** message, default `parse_mode='HTML'`, **no reply_markup** (the Rules button is dropped in this path — `GroupShield.py:162-167`). |
| Failure output (join, HTML parse error) | `send_message`'s outer `except telepot.exception.TelegramError` retries with `text.replace('\\', '').replace('>', '')` and **no `parse_mode`** (plain text, no HTML) — `universal_funcs.py:208-210,218-220`. If that also fails, the whole update is swallowed by `thread_function`'s top-level `except Exception: send_error_traceback(...)` (`COOKIEBOT.py:329-330`) — the group gets **total silence**, only the bot owner is mailed a traceback. |
| Failure output (join, empty stored body) | If `welcome['message']` exists but `len(...) == 0`, v1's branching (`Configurations.py`/`GroupShield.py:154-159`, transcribed) leaves `welcome` bound to the **raw backend dict**, not a string. `send_photo`/`send_message` then receive a dict as `caption`/`text`, which raises, caught by the *inner* `except Exception: send_message(cookiebot, chat_id, welcome)` fallback — which **also** passes the same dict, raising again, this time uncaught, swallowed by the outer handler exactly as above (silence to the group, traceback to the owner). Practically unreachable via `/newwelcome` itself (Telegram will not let an admin reply with empty text), only reachable by writing an empty string directly through the Java backend's `PUT /welcomes/{id}`. |
| Bot itself joining | `msg['new_chat_participant']['id'] == myself['id']` (`COOKIEBOT.py:122`) -> a *different* flow entirely (send owner a notice, maybe auto-leave if blacklisted/short title, send the "thanks for adding me" caption, no group_welcomes lookup at all). **Not part of `core_welcome`** — a bot-onboarding concern. This port's handler must simply not fire a welcome for this case. |
| Another bot joining (not self) | `msg['new_chat_participant']['is_bot']` and `from.id != new_chat_participant.id` -> `i18n.get("new_bot_participant")` sent **as a reply** to the join message, **instead of** the welcome text (`COOKIEBOT.py:136-139`). Ported verbatim as `new_bot_participant` in the locale catalog. |
| Multiple joiners in one update | Only `new_chat_members[0]` is ever read (see "Trigger (join)" above) — the 2nd, 3rd, ... joiners in the same service message get no welcome, no bot-participant check, nothing. Confirmed by reading `telepot` 12.7's source directly (downloaded and inspected for this port), not inferred. |
| `limbotimespan` / media restriction | `welcome_message` (`GroupShield.py:145-152`) calls `restrictChatMember` twice (grant-then-immediately-restrict-with-`until_date`) and sends `i18n.get("restrict_message", time=...)` **before** the welcome text, when `limbotimespan > 0` (== `configs.timeWithoutSendingImages`, the same field `core_mediarestrict` owns per `group_configs.media_restrict_seconds`). **This belongs to `core_mediarestrict`, not this feature** — see the "Boundary" section below. |
| Persistence | `PUT /welcomes/{chat_id}` else `POST` (create-or-update), body `{"message": <raw text>}` — Java backend, MongoDB `welcomes` collection. v2: `group_welcomes(group_id, body, updated_by, updated_at)`, upserted on `group_id` (`packages/cb-api/migrations/versions/0001_initial_schema.py:217-228`). |
| Side effects | Rules inline button (`callback_data=f'RULES {language}'`) attached to the image-card send only, dropped on the plain-text fallback (see above) — the callback *handler* for `RULES ...` belongs to `core_rules`, not built here. |
| External calls | None for the plain-text path (the image path calls `getUserProfilePhotos`, `getChat`, downloads two images, composites with OpenCV — out of scope, see "Boundary"). |
| Known defects | Hardcoded exclusion list of 5 specific v1-production chat IDs where the bot never welcomes (`GroupShield.py:141`) — instance-specific magic numbers tied to the original deployment's own community groups, not a generalisable behaviour; **deliberately not ported** (no current v2 group can match those literal ids in a way that means anything, and a real per-group "disable welcome" toggle is a config feature, not a hardcoded id list). |

### Placeholder table (the full contract)

`substitute_user_tags` (`GroupShield.py:38-47`) is the entire template-substitution
contract. It resolves the **first joiner only** (see above) to either `@username`
(if the user has a Telegram username) or their bare `first_name` (if not) — there
is **no distinction** between the ten spellings; every one of them expands to
that same single value.

| Placeholder | Expansion | Notes |
|---|---|---|
| `{user}` | `@username` or `first_name` | plain text, never an HTML mention entity |
| `{username}` | same | |
| `{mention}` | same | despite the name, not a clickable mention |
| `$user` | same | |
| `$username` | same | |
| `$(user)` | same | |
| `$(username)` | same | |
| `<user>` | same | the one form the prompt text itself advertises |
| `<username>` | same | |
| `<name>` | same | |
| any other placeholder, e.g. `{chat}`, `%s`, `{{user}}` | **left completely unchanged**, verbatim | no error, no warning; `str.replace` only ever touches the ten known tags |

**Verified defect, preserved:** `$username` (and only `$username`) is corrupted
if present, because `$user` is checked first and is a literal substring of it
with no closing delimiter to disambiguate — see "A second verified placeholder
defect" below for the full trace. `{username}`/`<username>`/`$(username)` do
not have this problem (their delimiters break the collision).

Additional rules baked into the same code path:

- Substitution is a straight `str.replace` over **every occurrence** of a
  matched tag anywhere in the text, not a single replacement and not
  word-bounded (`<user>fan` -> `@joefan`, no space inserted).
- Before substitution, every literal two-character sequence `\n` (backslash +
  `n`, as typically produced by a JSON round-trip through the old REST API) is
  unescaped to a real newline. This runs even if no placeholder is present.
- Malformed HTML produced by an admin's custom text (e.g. a stray `<` that
  doesn't close, or a tag Telegram's HTML parser rejects) triggers v1's
  parse-error retry: strip every `\` and every `>` from the text and resend
  with **no** `parse_mode` (plain text, no entity parsing at all). If that
  second attempt also 400s, the update is swallowed entirely (see the failure
  table row above).

## Boundary with `core_mediarestrict` (explicitly out of scope here)

v1's `welcome_message` does two unrelated things in one function:
1. If `limbotimespan > 0`, immediately `restrictChatMember` the newcomer (mute
   media/other-message-types with an `until_date`) and send the localised
   `restrict_message` text.
2. Send the welcome text.

`group_configs.media_restrict_seconds` is the same config field
(`timeWithoutSendingImages`/`limbotimespan`), and migration 0001's own comment on
`group_members_joined_idx` ("has this member been here longer than the limit?")
shows v2 has **already chosen a different mechanism**: check `group_members.joined_at`
against `media_restrict_seconds` when the member later *posts media*, not mute
them natively at join time. That re-architecture, the `restrictChatMember` call,
and the `restrict_message` text are **`core_mediarestrict`'s responsibility**,
owned by another agent. This port's join handler:

- does **not** call `restrictChatMember`,
- does **not** send `restrict_message`,
- does **not** write to `group_members` (this feature's persistence is scoped to
  `group_welcomes` only, per the task brief),
- **does** still send the welcome text itself — v1 sends both the restriction
  notice *and* the welcome on every restricted join, so dropping the welcome
  half here would be a real regression, not a scope trim.

Also out of scope, noted so the router-wiring owner (`handlers/__init__.py`,
not touched by this task) is aware: v1's dispatcher only reaches `welcome_message`
at all when captcha is *not* active for the join (`captchatimespan > 0 and bot is
admin` -> `captcha_message` instead, `COOKIEBOT.py:147-150`) and when none of
`check_human`/`check_cas`/`check_banlist`/`check_banlist_public` already kicked
the user. This handler has no way to know about those other features' state
(no shared "join already handled" signal exists yet), so **if `core_groupguardian`
(captcha) and `util_doomlist` end up registered as separate routers on the same
`new_chat_members` event, whoever wires `handlers/__init__.py` needs to ensure
only one of them fires per join** (aiogram stops at the first router whose
filter matches and handler completes) — this port does not attempt to solve
that ordering problem, it only guarantees this handler's own filter is narrow
(non-bot, non-self joiner) and correct in isolation.

Also out of scope: the pixel-art welcome-card image (`GroupShield.py:65-116`,
`welcome_card`) — OpenCV compositing of the user's avatar into the group photo.
AGENTS.md §4 forbids image compositing on the gateway's synchronous reply path
("ffmpeg, image compositing ... -> enqueue to cb-worker"), and `cb-worker` is not
in this task's file ownership. This port always takes v1's own fallback branch
(plain `send_message`, no image, no Rules button) — which, per the trace above,
is also what a real v1 deployment falls back to whenever `welcome_card` errors.
A future worker job to render and replace the plain message with the card is
follow-up work, not a behaviour this port removes (v1's *fallback* behaviour is
fully preserved; only the *happy-path* image is deferred).

## Phase 2/6 — QA vs. v1 conflict (recorded per AGENTS.md §1)

`Cookiebot-QA/features/core_welcome.feature` scenario 2 ("User tries to use
`/newwelcome` command but is not an admin") describes an immediate rejection +
tutorial video off the bare command. That text and video
(`Static/remove_anonymous_tutorial.mp4`) belong to `/configurar`'s anonymous-admin
defect (see `docs/contracts/admins.md`), not to `/newwelcome`. v1's real
`/newwelcome` code path has **no admin check on the command itself** — anyone
gets the prompt; the rejection ("You are not a group admin!", no video) only
happens if a non-admin actually replies to it. Per AGENTS.md §1 ("v1 code wins
for observable behaviour"), this port implements v1's real behaviour: the
command is ungated, and the rejection fires on the reply attempt. The QA
scenario is copied verbatim (not reworded) into `qa/features/core_welcome.feature`;
its step definitions in `qa/test_core_welcome.py` drive the actual v1 trigger
point (a reply attempt) and assert the *intent* of the scenario (a non-admin
cannot successfully set the welcome message and is told so) rather than the
exact copied wording, which does not exist anywhere in v1 for this command. This
conflict could not be recorded in `docs/FEATURE-MAP.md` (out of this task's file
ownership) so it lives here instead.

## Phase 3 — QA scenario

Copied `../Cookiebot-QA/features/core_welcome.feature` verbatim into
`qa/features/core_welcome.feature` (all three scenarios unchanged), then added:

- A PT/ES alias scenario for the join-time default text and for the `/newwelcome`
  aliases actually registered (`novobemvindo`; `nuevabienvenida` is not testable
  yet, see below).
- A scenario for a placeholder-substitution round trip (custom welcome with
  `<user>` set, joiner has no username -> first name used).
- A scenario for another bot joining (`new_bot_participant`, not the welcome
  text).
- A scenario for the bot itself joining (no welcome sent at all).
- A scenario for multiple simultaneous joiners (only the first is welcomed).

## Implementation (v2)

`packages/cb-gateway/src/cb_gateway/handlers/welcome.py`, `router = Router(name="welcome")`:

- `on_join` — `@router.message(F.new_chat_members)`. Reads `message.new_chat_members[0]`
  only (the v1 quirk, documented above). Returns with no action if that user is
  the bot itself (`newcomer.id == bot.id`, no API call needed — `Bot.id` is
  derived from the token). Sends `new_bot_participant` (a reply) if the joiner
  is a bot. Otherwise looks up `group_welcomes`, renders the text (default or
  custom + substitution), and sends it as a plain non-reply message with a
  parse-error retry, matching v1's real fallback path exactly.
- `newwelcome` — `@router.message(CommandName("newwelcome"))`. No `AdminOnly`
  filter (matches v1's ungated entry point). Replies with the fixed,
  non-localised prompt text.
- `capture_new_welcome` — matches a reply whose `reply_to_message.text` equals
  the prompt text verbatim and whose own text does not start with `/` (a lone
  `"/"` still counts as non-command, matching v1's `startswith("/") and
  len(text) > 1` guard exactly). Admin check is `ctx.is_admin` (already covers
  the anonymous-admin case correctly, per `docs/contracts/admins.md`) — same
  as the sibling `core_rules` port (`handlers/rules.py`), which implements this
  identical v1 shape and also does not compose v1's separate `ownerID`
  bot-owner bypass (`Settings.owner_id` exists but is otherwise unused anywhere
  in this codebase; composing a one-off interpretation of it here, differently
  from the one other feature with the exact same two-step admin flow, seemed
  more likely to introduce drift than to fix one — flagged as a shared open
  question rather than answered unilaterally). On success: upserts
  `group_welcomes`, best-effort 👍 reaction, confirmation reply, then
  best-effort deletion of the bot's own prompt message. On failure: the literal
  v1 string `"You are not a group admin!"`.

## Phase 6 — Parity table

| Row | Verdict | Note |
|---|---|---|
| `/newwelcome` triggers `newwelcome`/`novobemvindo`/`nuevabienvenida` | same | via `COMMAND_ALIASES` (pre-existing, not touched by this port) + `CommandName("newwelcome")`. |
| `/newwelcome@OtherBot` ignored | changed (intentional, fix) | same class of fix as `core_privacy` — v1's bare `startswith` would still fire for a foreign-bot-addressed command; `parse_command` correctly returns `None`. Not observable by any real user, strictly better. |
| `/newwelcome` command itself is ungated | same | matches v1's actual runtime behaviour; QA scenario 2's video+immediate-rejection describes `/configurar`, not this command — see conflict section above. |
| Reply-to-prompt admin check | same, plus a fix | membership test via `ctx.is_admin`, which already fixes v1's anonymous-admin defect (an anonymous admin succeeds here; in v1 the six "accidental-good" call sites, of which this is one, already worked via `sender_chat` presence, so this is not a behaviour change, just a cleaner implementation of the same accidental-good path). The `ownerID` bot-owner bypass (`docs/contracts/admins.md` "not built here" #1) is **not composed** — matching the sibling `core_rules` port, which has the identical v1 shape and also omits it; a real bot owner in v1 could force-set the welcome message even without being a group admin, which this port does not reproduce. |
| Reply-to-prompt admin-check text | same | literal, non-localised `"You are not a group admin!"`. |
| `/`-prefixed reply does not set the welcome | same | `_is_welcome_reply` excludes `text.startswith("/")`, matching v1's `if/elif` ordering. |
| Prompt text | same | byte-identical, non-localised. |
| Save confirmation (react + text + delete prompt) | same | 👍 reaction (best-effort, matches v1's uncaught-but-swallowed exposure), `"Welcome message updated! ✅"` reply, prompt message deleted. |
| Default welcome text (no custom message) | same | `welcome_user`/`welcome` keys, byte-identical, ported verbatim in `cb_core.locales`. |
| Placeholder substitution (10 tags) | same | all ten resolve identically; unknown placeholders left untouched; `\n` unescape runs first. |
| Success send shape | same | plain, non-reply `sendMessage`, default HTML parse mode, no reply_markup — v1's real, always-exercised fallback path (image card never attempted, see Boundary). |
| HTML parse-error retry | same | strip `\`/`>`, resend with no `parse_mode`; a second failure is logged and swallowed, not raised to the user. |
| Empty stored body | changed (intentional, fix) | v1 crashes (dict reaches the Telegram call, group gets silence, owner gets a traceback); v2 treats an empty/`NULL` body defensively the same as "no custom message" (falls back to the default text). Same practical unreachability via `/newwelcome` in both; v2 additionally avoids a crash if the row is ever written some other way. |
| Bot-itself-joining suppressed | same | no welcome, no lookup; a separate bot-onboarding concern not built here. |
| Another bot joining | same | `new_bot_participant`, as a reply, instead of the welcome text. |
| Multiple joiners in one update | same (preserved quirk) | only `new_chat_members[0]` processed; verified against `telepot` 12.7 source, not inferred. |
| `limbotimespan` restriction (mute + `restrict_message`) | **not built here** | `core_mediarestrict`'s responsibility; see Boundary section. Welcome text is still sent (v1 sends both). |
| Pixel-art welcome card image + Rules button | **not built here** | deferred to a future `cb-worker` job (AGENTS.md §4); v1's own fallback path (plain text, no button) is what this port implements, not a regression from it. |
| 5 hardcoded excluded chat IDs | not ported (deliberate) | instance-specific magic numbers from the original deployment, not a generalisable behaviour. |
| Persistence | same | `group_welcomes`, `PRIMARY KEY(group_id)`, every statement filters on `group_id`, via `cb_core.db`. |

## A second verified placeholder defect: `$user`/`$username` collide

Implementing `_substitute_user_tags` byte-for-byte from `GroupShield.py:38-47`
surfaces a real v1 bug that the placeholder table above understates by only
listing the ten tags as if each were independent. They are not: `str.replace`
scans for a *literal substring*, and v1's own tag list checks `$user` before
`$username` (same order in both v1 and this port — `GroupShield.py:40`). `$user`
is a literal prefix of `$username` with no closing delimiter to disambiguate
them (unlike `{user}`/`{username}` or `$(user)`/`$(username)`, where the `}` /
`)` breaks the collision). So a custom welcome text containing the *wider* tag
`$username` is corrupted: `"hi $username!"` -> checking `$user` first finds it
as a substring of `$username` and replaces it, producing `"hi @joename!"` (the
`name` tail of `$username` survives, glued onto the substituted value) instead
of the presumably-intended `"hi @joe!"`. Verified by writing the port straight
from v1's algorithm and having a unit test fail against its own (wrong,
idealised) expectation — not inferred by reading the code alone. `<user>` vs
`<username>` and `$(user)` vs `$(username)` do **not** collide (their closing
delimiters make them non-substrings of each other). This is preserved, not
fixed: it is exactly what a real v1 group offering `$username` in a custom
welcome message has always produced.

## Needed in a file I don't own

Nothing outstanding for `cb_core/textmatch.py` — all three v1 aliases
(`newwelcome`, `novobemvindo`, `nuevabienvenida`) are already registered in
`COMMAND_ALIASES`.
