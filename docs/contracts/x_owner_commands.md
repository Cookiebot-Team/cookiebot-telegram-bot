# Contract: x_owner_commands (v1 -> v2)

Phase 2/6 of `/migrate-feature` for the owner's private-chat commands. **No QA
scenario exists** — `../Cookiebot-QA/features/` has no owner file at all, so
`qa/features/x_owner_commands.feature` is authored as part of this port
(AGENTS.md §5). FEATURE-MAP row: `x_owner_commands`. Spec:
`.specs/features/x_owner_commands/spec.md`.

Files owned by this port:
`packages/cb-core/src/cb_core/ops.py` (new),
`packages/cb-core/src/cb_core/jobs.py` (two job names),
`packages/cb-gateway/src/cb_gateway/handlers/owner.py` (new),
`packages/cb-gateway/src/cb_gateway/handlers/__init__.py` (registration),
`packages/cb-worker/src/cb_worker/jobs/broadcast.py` (new),
`packages/cb-worker/src/cb_worker/main.py` (job registration), and the tests
listed at the bottom.

## Phase 1 — where v1 lives

- Dispatch: `../COOKIEBOT-Telegram-Group-Bot/Bot/COOKIEBOT.py:83-105` — seven
  branches of the private-chat `if/elif` chain, each re-testing
  `msg['from']['id'] == ownerID`.
- `list_groups`: `Miscellaneous.py:83-112`.
- `broadcast_message`: `Miscellaneous.py:114-122`.
- `leave_and_blacklist`: `universal_funcs.py:320-329`.
- `blacklist_user` / `unblacklist_user`: `universal_funcs.py:307-313`.
- Storage: the Java backend's Mongo, through `post_request_backend` /
  `delete_request_backend` on `blacklist/{id}`, `registers/{id}`,
  `configs/{id}`, `groups/{id}`.
- Locale strings: the nested `groups` object (`total`, `new`, `remove`) in
  `cb_core/locale_data/{en,pt,es}/lib.json` — already ported byte-identical.
  Every other string in this feature is a hardcoded English f-string in v1 and
  stays one here.

## Phase 2 — v1 behaviour contract

| Aspect | v1 behaviour (file:line) |
|---|---|
| Chat type | The whole block is inside `if chat_type == 'private':` and `return`s at its end (`COOKIEBOT.py:75,109`) — no owner command works in a group |
| Authorisation | `'from' in msg and msg['from']['id'] == ownerID`, repeated per branch (`:83,89,92,95,98,101,104`) |
| `/grupos`, `/groups` | `startswith(("/grupos", "/groups"))` ⇒ `list_groups` (`:83-84`) |
| `list_groups` | `sendChatAction typing`; read every group from `registers`; per group `getChat` + `sleep(0.4)`, skip anything not a group/supergroup; **one `sendMessage` per group** + `sleep(0.1)`; then `groups.total` with `len(groups) - len(removed)`; diff against a local `list_groups.json` and send `groups.new` / `groups.remove` when non-empty (`Miscellaneous.py:83-112`) |
| `/stop` | `msg['text'] == "/stop"` (exact) ⇒ `kill_api_server(); os._exit(0)` — no reply (`:95-97`) |
| `/restart` | `msg['text'] == "/restart"` (exact) ⇒ `kill_api_server(); os.execl(sys.executable, ...)` — no reply (`:98-100`) |
| `/leave` | `startswith("/leave")` ⇒ `leave_and_blacklist(msg['text'].split()[1])`, then `send_message(ownerID, f"Auto-left\n{chat_id}")` (`:101-103`) |
| `leave_and_blacklist` | `str(chat_id).replace('@','')`; POST `blacklist/{id}`; DELETE `registers/{id}`, `configs/{id}`, `groups/{id}`; `leaveChat` in a `try/except` that only `print`s (`universal_funcs.py:320-329`) |
| `/blacklist` | `startswith("/blacklist")` ⇒ `blacklist_user(split()[1])`, reply `f"Blacklisted user with ID {id}"` (`:89-91`) |
| `/unblacklist` | `startswith("/unblacklist")` ⇒ `unblacklist_user(split()[1])`, reply `f"Unblacklisted user with ID {id}"` — **the same line whether or not a row existed** (`:92-94`) |
| Argument parsing | `msg['text'].split()[1]` then `str(...).replace('@','')`, so `@123` and `123` are the same id and a username was never actually supported despite the `@` stripping (`universal_funcs.py:307-308`) |
| Missing argument | `IndexError` into the global handler; nothing is answered |
| `/broadcast` | `startswith("/broadcast")` ⇒ `broadcast_message`: every group, `send_message(int(id), text.replace('/broadcast',''))`, `sleep(0.5)`, `except: pass`; **no reply to the owner** (`Miscellaneous.py:114-122`) |
| Non-owner sending any of these | Falls through the chain to `elif msg['text'].startswith("/")` ⇒ `"Commands must be used in a group chat!"` (`COOKIEBOT.py:106-107`) |
| Known defects | D-OC-1 … D-OC-5 below, plus FEATURE-MAP **D8** and **D11** |

## Defects — verdict per item

| id | Defect | Verdict |
|---|---|---|
| D-OC-1 | **`/leave` confirms the wrong chat.** `send_message(ownerID, f"Auto-left\n{chat_id}")` (`COOKIEBOT.py:103`) — `chat_id` there is the *private chat the command was typed in*, i.e. the owner's own id, never the group that was left. The one number the message exists to report is the one it does not contain. | **fix** — the reply names the chat that was actually left. Same wording (`"Auto-left\n{id}"`), correct id. |
| D-OC-2 | **`leave_and_blacklist` orphans everything it did not name.** It deletes `registers`, `configs` and `groups` and leaves `randomdatabase`, scheduled posts and giveaways pointing at a group the bot is no longer in (`universal_funcs.py:322-324`). | **fix** — one `DELETE FROM groups`, and every tenant-scoped table cascades. |
| D-OC-3 | **`/grupos` is O(groups) Telegram calls and O(groups) messages**, serialised behind `sleep(0.4)` + `sleep(0.1)` on a handler thread (`Miscellaneous.py:93-103`). FEATURE-MAP **D11**. | **fix** — `groups.title` is a column, so no `getChat` at all; one message, paged at `GROUPS_PAGE_SIZE = 100`. |
| D-OC-4 | **`/broadcast` blocks a handler thread for `0.5 × groups` seconds** and swallows every failure (`Miscellaneous.py:114-122`). FEATURE-MAP **D8**. | **fix** — the gateway enqueues `jobs.BROADCAST_TO_GROUPS`; the worker defers one `BROADCAST_DELIVER` per group at v1's own 0.5s spacing (`_defer_by`) and counts sent/failed. |
| D-OC-5 | **The blacklist could not distinguish a banned user from an abandoned chat.** Both `blacklist_user` and `leave_and_blacklist` POST the same `blacklist/{id}` (`universal_funcs.py:307-309,321`). | **fix** — `blacklist.kind` is `'user'` or `'chat'`. `util_doomlist`'s reads already filter `kind = 'user'`, so a chat left by `/leave` can no longer ban a user whose id happens to collide. |
| D-OC-6 | **`/stop` and `/restart` are process control on a single-process deployment.** | **not ported** — see "Deliberately not ported" below. |

## Deliberately not ported

`/stop` (`os._exit(0)`) and `/restart` (`os.execl`) assume v1's one process on
one host. v2 runs N stateless gateway replicas behind an orchestrator:
whichever replica received the DM would die, the orchestrator would replace it
within seconds, and the other N-1 would carry on — at best a confusing no-op,
at worst an operator repeating it into a self-inflicted outage.
`.specs/features/private_dispatch/spec.md` reached the same conclusion.

Both commands **answer** rather than being absent (`PROCESS_CONTROL_REFUSAL`),
because silence from an owner-only command reads as success. The refusal names
the orchestrator's own rollout as the thing that replaces them.

## Preserved deliberately

- **`"Auto-left\n{id}"`, `"Blacklisted user with ID {id}"` and
  `"Unblacklisted user with ID {id}"`** — v1's own English f-strings, not
  catalog keys in v1 and therefore not translated here.
- **`@123` and `123` are the same argument** (`parse_subject` strips a leading
  `@`), including the fact that a real `@username` still does not resolve.
- **`groups.total`** stays a catalog lookup through `locales.get_nested`, since
  `groups` is a nested object — the same distinction `x_giveaways` documents.
- **`leaveChat` failures do not abort the command** (`universal_funcs.py:328-329`
  only prints): the row is deleted and the blacklist written either way, so a
  chat the bot was already removed from still gets forgotten.
- **An owner-only command answers nothing to a non-owner.** v1's generic
  `"Commands must be used in a group chat!"` fallback belongs to the DM
  fallthrough branch, which is a separate, unported unit of work
  (`.specs/features/private_dispatch/`, `/start`'s DM screen) — so a non-owner
  currently gets silence rather than that line. Named difference, not a
  behaviour this feature chose.

## Differences a group could notice

None. Every command here is private-chat, owner-only; no group-visible
behaviour changes except `/broadcast`'s message arriving spaced by the worker
rather than by a blocked thread, and `/leave` now taking the group's other
rows with it.

## Phase 6 — parity

| Behaviour | v1 | v2 | Same? |
|---|---|---|---|
| Private chat only | inside `if chat_type == 'private'` | `F.chat.type == ChatType.PRIVATE` | ✅ |
| Owner gate | `msg['from']['id'] == ownerID` | `settings.owner_id`, unset ⇒ nobody | ✅ |
| `/grupos`, `/groups` | one message per group, `getChat` each | one paged message, title from the column | ⚠️ D-OC-3 |
| `groups.total` footer | `len(groups) - len(removed)` | `count(*)` | ⚠️ v1 subtracted the groups its own sweep had just failed to reach |
| `groups.new` / `groups.remove` | diffed against `list_groups.json` on the host | not ported — a local-file diff has no meaning across N replicas | ⚠️ named |
| `/leave` | blacklist, delete 3 collections, `leaveChat`, confirm the wrong id | blacklist `kind='chat'`, delete `groups` (cascades), `leaveChat`, confirm the right id | ⚠️ D-OC-1/2/5 |
| `/blacklist` | POST, fixed reply | INSERT `kind='user'`, same reply | ✅ |
| `/unblacklist` | DELETE, same reply either way | reports "was not blacklisted" when nothing was removed | ⚠️ spec R8 |
| Bad/missing argument | `IndexError`, no reply | `Usage: …` | ⚠️ deliberate |
| `/broadcast` | inline `sleep(0.5)` loop, no reply | queued fan-out, "Broadcast queued." + a count from the worker | ⚠️ D-OC-4 |
| `/broadcast` body | `text.replace('/broadcast','')` — removes *every* occurrence, keeps the leading space | the command's argument, stripped | ⚠️ a body containing the literal `/broadcast` survives intact in v2 |
| `/stop`, `/restart` | kill / re-exec the process | refusal explaining why | ⚠️ D-OC-6 |
| Trigger matching | `startswith`, so `/blacklistfoo` matched | `CommandName`, exact | ⚠️ stricter |

## Tests

| Layer | File |
|---|---|
| Unit | `packages/cb-gateway/tests/test_owner.py` — every v1 trigger resolving, the owner gate (including unconfigured ⇒ nobody), `parse_subject` against v1's own `split()[1].replace('@','')`, and the one-message group list |
| Unit | `packages/cb-worker/tests/test_broadcast_job.py` — one deferred job per group, v1's 0.5s spacing, the owner's count, and a group that fails costing only itself |
| Acceptance | `qa/features/x_owner_commands.feature` + `qa/test_x_owner_commands.py` — nine scenarios, authored, against a real `groups`/`blacklist` |
