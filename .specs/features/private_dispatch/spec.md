# private_dispatch — Specify

**Kind:** shared infrastructure (like `cb_gateway/queue.py` for `util_everyone`)
— no `scripts/spec.py` row of its own, closes `HANDOFF.md` §1 gap 2.
**v1 source:** the private-chat branch of `thread_function`,
`../COOKIEBOT-Telegram-Group-Bot/Bot/COOKIEBOT.py:73-110`.

## Goal

v2 has no general mechanism for a private-chat (DM) message: no context type
that doesn't assume a `group_id`, and two live, half-solved gaps as a result
— `/privacy` mishandles a DM today (see "The live bug" below), and
`/commands` solved its own DM case with a private, un-generalised branch
inside one handler. This slice builds the shared mechanism both should have
been using, retrofits both to prove it generalises, and is the prerequisite
`util_birthday`/`util_nextbirthday` need for DM birthdate collection
(`.specs/features/util_birthday/` — separate slice, next).

## v1's private-chat branch, read in full

`COOKIEBOT.py:73-110`, the entire `if chat_type == 'private':` block inside
`thread_function` — the *only* place v1 ever handles a DM, and it unconditionally
`return`s at the end, so nothing past this block (including group-only
bookkeeping like `check_new_name`, `COOKIEBOT.py:332`) ever runs for a private
chat:

| Trigger | Handler | Note |
|---|---|---|
| no `text` key | inline | `i18n.get("private_chat", lang="en")` — a message type the bot can't read (sticker, photo with no caption, etc.) |
| `/start` | `set_private_commands` | Sets this chat's command menu via `setMyCommands`, language literally `"private"` (`Configurations.py:100-101`) — **out of scope, named follow-up**, see below |
| reply to the `/config` "REPLY THIS MESSAGE..." prompt | `configurar_set` | The `/config` wizard's DM continuation — **out of scope**, owned by `config_menu.py`, not this slice's file ownership |
| `/grupos`, `/groups` (owner only) | `list_groups` | **out of scope**, see "Owner-only ops commands" below |
| `/comandos`, `/commands` | `list_commands(..., 'eng')` | **already ported** (`cb_gateway/handlers/listcommand.py`) — ad hoc, this slice generalises it |
| `/privacy`, `/privacidade`, `/privacidad` | `privacy_statement(..., 'eng')` | **not ported for DM** — this slice's live bug fix, below |
| `/stop`, `/restart` (owner only) | kills/restarts the process | **out of scope**, see below |
| `/leave`, `/blacklist`, `/unblacklist`, `/broadcast` (owner only) | various | **out of scope**, see below |
| any other `/`-prefixed text | inline | `"Commands must be used in a group chat!"` — hardcoded English, never through `i18n` |
| anything else | `pv_default_message` | The bot's DM welcome screen — **out of scope, named follow-up** |

## The live bug: `/privacy` in a DM today

`cb_gateway/handlers/privacy.py`'s only handler is
`@router.message(CommandName("privacy"))` — no chat-type filter at all. A
private-chat `/privacy` matches it and calls `context_for(bot, message)`,
which does `group_id = message.chat.id` (a DM's own chat id — a plain
positive integer, the sender's user id) and then
`await group_config.get_config(group_id)`: a **distributed-table read keyed
on a value that was never a real group**. It happens not to crash — `group_config`
falls back to defaults for an unknown id, and `cb_core.admins.resolve_actor`
degrades to "not an admin" when `bot.get_chat_administrators()` fails against
a private chat id (Telegram's real API rejects that call for a non-group
chat) — but it is doing real distributed-table work, and potentially warming
an L1/L2 cache entry, for a "group" that will never exist. This is exactly
what AGENTS.md's non-negotiable #2 ("every query filters on the distribution
column... a private chat has no `group_id`") warns against, discovered live,
not hypothetically. **Fixed in this slice.**

## The ad hoc precedent: `/commands` in a DM today

`cb_gateway/handlers/listcommand.py`'s single handler already special-cases
`ChatType.PRIVATE` inline (`if message.chat.type == ChatType.PRIVATE: reply
with locales.text(..., "en"); return`), explicitly to *avoid* calling
`context_for` for the reason above (its own docstring says so). This works,
has a QA scenario, and is already `done` — but it is a one-off inside a
single handler, not a mechanism another feature could reuse. This slice
extracts the *pattern* (a chat-type-scoped handler pair, same idea
`ship.py`/`battle.py`/`fun_random.py` already use for `F.chat.type !=
ChatType.PRIVATE`, mirrored for `==`) and gives it one shared, minimal
context object.

## Behaviour contract — the two retrofits (Phase 2)

| Aspect | v1 behaviour (file:line) |
|---|---|
| `/privacy` DM triggers | `/privacy`, `/privacidade`, `/privacidad` — same aliases as the group command, already in `COMMAND_ALIASES` |
| `/privacy` DM preconditions | None — no owner check, no admin check (there is no admin concept in a DM) |
| `/privacy` DM success output | `i18n.get("privacy", lang='eng')` — **hardcoded English**, regardless of the sender's own Telegram `language_code` (`COOKIEBOT.py:87-88`) — sent as a reply, `parse_mode='HTML'` |
| `/commands` DM triggers | `/comandos`, `/commands` — already ported, unchanged by this slice |
| `/commands` DM success output | `Cookiebot_functions.txt`, hardcoded English — already ported, unchanged by this slice (relocated, not rebehaviored) |
| Cooldowns / quotas | None for either |
| Persistence | None for either |

No defect verdicts needed for these two beyond the live bug above (fixed, not
a preserve-worthy quirk — a distributed-table read against a nonexistent
group is exactly the "silent-failure-shaped" case AGENTS.md's Phase 2 rule
requires fixing).

## Owner-only ops commands — recommend dropping, not porting

`/grupos`, `/stop`, `/restart`, `/leave`, `/blacklist`, `/unblacklist`,
`/broadcast` all gate on `msg['from']['id'] == ownerID`, a single hardcoded
Telegram user id, and several (`/stop`, `/restart`) call `os._exit(0)` /
`os.execl(...)` — process control for a single long-running instance. v2 is
stateless and horizontally replicated (`cb_gateway/main.py`'s own docstring:
"capacity is replicas"); killing "the" process has no coherent meaning when
there are N of them behind a load balancer, and a single hardcoded owner id
does not fit the multi-tenant model `cb_core.tenancy` already established.
**Recommendation: do not port these**, not even behind an env var — flagged
here as a policy decision, not decided unilaterally. If ops tooling is wanted
later, it should be a real admin surface (API + auth), not a Telegram DM.

## `/start` and the DM welcome screen — named follow-up

`pv_default_message` (bot-skin-branded intro text, inline "add me to a group"
button, per-sender language via `normalize_lang(from.language_code)` — a
**different** language convention than `/privacy`/`/commands`'s hardcoded
English) and `set_private_commands` (the DM's own `setMyCommands` menu,
`cb_core/setlang.py`'s `set_group_commands` already built the piece this
would reuse, per that module's own docstring) are real v1 behaviour, but a
separate, larger unit of work with its own design questions (which bot skin,
what the button links to for a multi-tenant deployment). Not this slice —
named here so it isn't rediscovered as a surprise.

## QA

Neither `core_privacy.feature` nor `core_listcommand.feature` needs new
scenario wording changed. `core_listcommand.feature` already has a
private-chat scenario (already passing, unchanged). `core_privacy.feature`
gets one net-new scenario for the DM case — `../Cookiebot-QA/features/core_privacy.feature`
has no private-chat scenario at all, so this is v1 behaviour the spec never
covered, same precedent already used throughout this codebase.
