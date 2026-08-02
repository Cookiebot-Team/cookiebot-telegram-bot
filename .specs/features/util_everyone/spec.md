# util_everyone — Specify

**Feature id:** `util_everyone` · **Milestone:** M2 · **Kind:** v1 port
**v1 source:** `Bot/UserRegisters.py:97-146`, dispatched `Bot/COOKIEBOT.py:272-273`

## Goal

An admin types `/everyone` and every member the bot has ever seen in that group
is pinged: one or more chunked `@mention` messages in the chat, then a private
message to each member with a deep link back to the call. HANDOFF §4 puts this
first in the next batch — the member registry it needed landed with `fun_ship`.

It is also the port that is expected to build **gateway → worker enqueue
wiring** (HANDOFF §1 gap 5, §4 row 1): "batch it, and put the fan-out in
cb-worker, never on the reply path."

## Scope

In: the reply-path handler (admin gate, roster read, chunked ping), a batched
roster query, gateway→worker enqueue, a worker that holds a bot, the DM fan-out
job with its registry hygiene, tests at all three layers, the contract.
Out: `util_calladms`' DM half and the captcha's 30 s unban — both want the same
wiring and both become follow-ups once it exists, not part of this slice.

## Behaviour contract (Phase 2)

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
| Known defects | D-EV-1 … D-EV-6 below |

### Backend surface being replaced

| v1 call | Java side | Mongo |
|---|---|---|
| `GET registers/{id}` | `RegisterResource.findById:38-42` → `RegisterService.findById:33-35` | `registers`, `{id, users:[{user, date, accountId}]}` |
| `GET users?username=` | `UserResource.findAll:33-38` → `UserService.findAll:26-38` → `UserRepository.findByUsername:13` (derived, **unindexed**) | `users`, `{id, username, firstName, lastName, languageCode, birthdate}` |
| `DELETE registers/{id}/users` | `RegisterResource.deleteUser:72-76` → `RegisterService.deleteUser:82-99` (`$pull`) | `registers` |

No batch endpoint exists — no `findByUsernameIn`, no array parameter, no
`$lookup`. The N+1 is structural in v1 and disappears in v2 because
`group_members` already carries the user id.

### Verbatim strings — `Bot/Static/locales/{lang}/lib.json`

| Key | eng |
|---|---|
| `everyone_no` | `You don't have permission to call all members of the group!\n<blockquote>If you're speaking as a channel, join and use the command as a user</blockquote>` |
| `everyone_len` | `I haven't seen any members in the chat to call yet!\nOver time, the bot will recognize members and allow calling everyone.` |
| `everyone_call` | `You were called in the chat <b> %(title)s </b>` |

pt and es exist for all three and are already in `cb_core/locale_data/` — read
them from there, never retype. The `Number of known users: ` prefix has no
locale key and is always English (`UserRegisters.py:112`).

### Defects — verdict per item

| id | Defect | Verdict |
|---|---|---|
| D-EV-1 | **N+1 backend calls** — one `GET users?username=` per member (`:129`); flagged in `feature-map.mdx`. | **fix.** v2 reads the whole roster in one query on `group_members` filtered by `group_id`. This is the point of the port. |
| D-EV-2 | Admin gate is skipped when the admin list is empty, so a failed `getChatAdministrators` turns `/everyone` into a free-for-all (`:99-102`). | **fix.** A silent-failure bug, not a user-visible quirk (`/migrate-feature` Phase 2 rule). v2 denies with `everyone_no` when it cannot establish that the caller is an admin. Record the change. |
| D-EV-3 | A caller with **no username** passes the gate regardless of admin status (`:100`, the gate only runs `if 'username' in msg['from']`). | **fix**, same reasoning — v2 gates on the resolved actor, not on the presence of a username. |
| D-EV-4 | The `Number of known users:` header is hardcoded English and appears only on the first chunk. | **preserve.** User-visible, harmless, and localising it changes output for every existing group. |
| D-EV-5 | Every 10th DM forwards the triggering group message to the bot owner (`:137-138`). | **drop.** Undisclosed exfiltration of group content to a hardcoded account; there is no configuration for it and no user-facing trace. Do not port. Record the removal prominently in the contract. |
| D-EV-6 | Dead `try/except TypeError: pass` around the chunk length check (`:113-120`). | **drop** — it guards nothing. |

## QA scenario

`Cookiebot-QA/features/util_everyone.feature`:

```gherkin
Feature: allows the admins of a group to ping everyone in the chat

    Background:
        Given that the bot is in the group and properly set up

    Scenario: Admins can use the command to ping everyone in the chat
        Given that the user is an admin of the group
        When an admin sends the command to /ping everyone
        Then all members of the group should receive a notification

    Scenario: Non-admins cannot use the command to ping everyone in the chat
        Given that the user is not an admin of the group
        When a non-admin sends the command to /ping everyone
        Then the bot should respond with a message indicating that they do not have permission to use this command
```

**QA/v1 conflict, already known (HANDOFF §5):** QA writes the trigger as
`/ping everyone`; v1 ships `/everyone` and `@everyone`. **Both spellings must
resolve** — the alias table is the mechanism, and the mismatch is already
recorded in `feature-map.mdx`.

## Success criteria

1. `/everyone`, `@everyone` and the QA spelling all reach the handler; a
   non-admin gets exactly `everyone_no` and no ping.
2. The roster comes from **one** query filtered on `group_id`
   (`EXPLAIN` shows `Task Count: 1`, AGENTS.md §4.6).
3. The ping message text is byte-identical to v1's for a given roster, including
   the English header on the first chunk and the 4096-char chunk boundary.
4. The reply path enqueues the DM fan-out and returns; no DM is sent from the
   gateway.
5. The fan-out marks members it finds `left`/`kicked` as left, and never
   forwards anything to anyone (D-EV-5).
6. Unit, integration and acceptance tests green; contract written.
