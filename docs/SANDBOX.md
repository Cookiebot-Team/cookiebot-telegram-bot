# The sandbox — a local Telegram the real bot talks to

A Telegram-shaped client and a fake Bot API, so a person can drive the bot by
hand: switch users, join a group, send `/rules`, press a config button, watch a
sticker flood get deleted. It exists because reading a passing test tells you the
handler did something; this tells you *what a user sees*.

The important property: **the bot is not modified or mocked.** cb-gateway points
its Telegram API base at the sandbox and uses its polling ingest, so every click
runs the production stack — the same routers, filters, middlewares, Postgres and
Valkey. If the sandbox shows the wrong thing, the bot is wrong.

```
web/  (Next.js + Tailwind, :3001)  ──REST + SSE──►  cb-sandbox (:8083)
                                                      ├── /bot<token>/<method>   ← cb-gateway polls this
                                                      └── /api/...               ← the client drives this
cb-gateway (unchanged)  ──CB_TELEGRAM_API_BASE + CB_TELEGRAM_INGEST=polling──►
```

## Running it

```bash
python scripts/cb.py sandbox-up      # prints the exact wiring for three terminals
```

In short: `cb.py up` (Postgres + Valkey), `cb.py sandbox`, the gateway with

```bash
CB_TELEGRAM_API_BASE=http://localhost:8083 \
CB_TELEGRAM_INGEST=polling \
CB_BOT_TOKENS='{"cookiebot": "424242:SANDBOX"}' \
python scripts/cb.py gateway
```

and `cb.py sandbox-web`. Open <http://localhost:3001> and press **Seed default**.

The gateway *must* be started with both variables. Pointed at the real
api.telegram.org it will sit there politely doing nothing, and the UI will look
broken for a reason that has nothing to do with the sandbox.

## What it is for

| Question | How to answer it here |
|---|---|
| Does `/rules` answer in Portuguese for a `pt` group? | switch the group's language in `/config`, send `/regras` |
| Does an **anonymous admin** still get the config menu? | toggle anonymity on an admin, send `/config` — this is the v1 defect the port fixed |
| Does sticker spam actually delete? | send stickers past the limit, watch the API-call log fill with `deleteMessage` |
| Is a new member's media really restricted? | join, immediately send a photo |
| Did the bot answer the callback? | press an inline button, look for `answerCallbackQuery` in the log |

The right-hand **API call log** is the validation surface: it shows what the bot
actually asked Telegram to do, including the calls a chat window cannot show —
`restrictChatMember`, `banChatMember`, `deleteMessage`.

## Shape

```
packages/cb-sandbox/src/cb_sandbox/
  state.py         users, chats, messages, the update queue — the shared world
  telegram_api.py  /bot<token>/<method>, the surface cb-gateway consumes
  control_api.py   /api/..., the surface the web client drives, plus SSE
  app.py           assembly
web/
  app/             the three-pane shell
  components/chat/ chat list, bubbles, composer, inline keyboards
  components/sandbox/ user switcher, membership controls, seeds, API-call log
  lib/             typed API client and the live-update hook
  types.ts         shapes shared by both halves
```

State lives in memory for reads and in **DuckDB** (`CB_SANDBOX_DB`, default
`sandbox.duckdb`) for durability: users, groups, memberships, messages and the
API-call log survive a restart, so a scenario you set up an hour ago is still
there, and a second process can open the same file read-only to inspect a run.
The `getUpdates` queue and the SSE event buffer stay in memory — they are
protocol and notification state, not the world.

A failed write logs and carries on in memory. A workbench that refuses to run
because its notebook is locked is worse than one that forgets.

**Reset** clears both, and is meant to be pressed often.

## Running the BDD suite through it

```bash
CB_SANDBOX_DB=/tmp/qa-sandbox.duckdb CB_QA_SANDBOX=1 python scripts/cb.py test
```

Opt-in. With `CB_QA_SANDBOX` unset the acceptance suite runs against
`qa/mock_telegram.py` exactly as before — same pass count, same speed — because
that suite is the CI gate and must not get slower or flakier to feed a viewer.

With it set, the scenarios drive `cb_sandbox` instead, so the run leaves a
DuckDB file you can open (or point the sandbox server at) and read back what the
scenarios actually did. A `core_rules` + `core_privacy` run leaves, for example:

```
sandbox_messages   49     24 from the test user, 23 bot replies,
sandbox_api_calls  43     23x sendMessage, 18x getChatAdministrators, 2x deleteMessage
```

including one message from GroupAnonymousBot — the anonymous-admin scenario,
visible as the thing it actually is rather than as a green dot.

The two modes answer different questions. The default one asks "is the behaviour
still correct", fast and in CI. This one asks "what does that behaviour look
like", which is the question a passing test cannot answer.

## Bot API compatibility

The sandbox exists to make UAT trustworthy: if it disagrees with real
`api.telegram.org` (or the self-hosted `tdlib/telegram-bot-api` server), a
scenario that passes here proves nothing about production. This section
tracks what has been checked against `core.telegram.org/bots/api`, what was
added, and — just as important — every place this file deliberately does
something other than what the real server does.

### The bug that made resets dangerous

`SandboxStore.reset()` used to restart `_update_ids` and its message-id
counter at their base values. cb-gateway runs a real Valkey-backed dedupe
middleware (`cb_core.dedupe.idempotency_key`, keyed `cb:upd:<bot>:<update_id>`,
set with `NX` and a TTL) exactly as it would against production Telegram, and
Valkey has no idea the sandbox was reset. The first `getUpdates` batch after a
reset would reuse ids Valkey already had recorded (within the TTL) as
delivered, so the middleware silently treated them as redeliveries and
dropped them before `dp.feed_update` ever ran — the bot would look completely
dead, with nothing in the sandbox's own logs to explain why. This was the
single most confusing failure mode the tool could produce, and it is why
"press Reset constantly" (above) used to come with an asterisk.

Fixed by persisting each counter's high-water mark in DuckDB
(`sandbox_counters`, the one table `reset()` does not clear) and resuming
past it on both a reset and a process restart. See `SandboxStore.next_update_id`'s
docstring in `state.py` for the full reasoning, and
`tests/test_bot_api_compat.py::TestUpdateIdSurvivesReset`/`TestUpdateIdSurvivesRestart`
for the regression tests.

### Methods added or changed, and the real semantic each now honours

| Method | Real-API semantic now honoured |
|---|---|
| `getMe` | Returns `can_join_groups`, `can_read_all_group_messages`, `supports_inline_queries` — aiogram's `User` model carries them and code may branch on them. |
| `getUpdates` | `limit` bounds (1–100, else 400); negative `offset` ("forget everything before the last `-offset` updates"); `allowed_updates` actually filters the response and is remembered across calls that omit it; a second concurrent poll gets `409 Conflict: terminated by other getUpdates request; make sure that only one bot instance is running`. |
| `getWebhookInfo` | New. Always reports no webhook configured — correct for a bot pointed here with `CB_TELEGRAM_INGEST=polling`. |
| `sendMessage`/`editMessageText` | `parse_mode` (`HTML`, `MarkdownV2`) is actually parsed into plain `text` + `entities`, the same shape aiogram's `Message.entities` reads; malformed markup raises the same `Bad Request: can't parse entities: ...` class of error the real server would (this is what makes `welcome.py`'s `TelegramBadRequest` retry path exercisable at all). `chat_id` accepts `@username`. `reply_parameters` (with `allow_sending_without_reply`) alongside the legacy `reply_to_message_id`. `link_preview_options` and `message_thread_id` (sets `is_topic_message`) round-trip onto the returned `Message`. |
| `sendPhoto`/`sendVideo`/`sendAnimation`/`sendDocument`/`sendAudio`/`sendVoice` | `caption`/`parse_mode`/`caption_entities` parsed the same way as `sendMessage`'s `text`, into the message's own `caption_entities` (a field distinct from `entities`). `sendDocument`/`sendAudio`/`sendVoice` are new. |
| `sendDice` | New. Per-emoji ranges from the real docs (🎲🎯🎳 1–6, 🏀⚽ 1–5, 🎰 1–64). |
| `editMessageCaption` | New — same parse_mode handling as the send-with-caption methods. |
| `deleteMessage` | Correct error wording for a message that doesn't exist (`Bad Request: message to delete not found`, distinct from `editMessageText`'s `... to edit not found`). Does **not** enforce the real 48-hour delete window (see divergence table). |
| `deleteMessages` | New. Silently skips ids it can't find, matching the real "partial success" behaviour, rather than failing the whole call. |
| `forwardMessage` | New. `from` on the result is the bot (whoever called the Bot API), never the original sender; `forward_origin` carries the original attribution — the split real Telegram makes. |
| `copyMessage` | New. Returns only `{"message_id": ...}` (`MessageId`), not a full `Message`, and carries no forward attribution at all — the two real, deliberate differences from `forwardMessage`. |
| `answerCallbackQuery` | Rejects an id that was never issued or was already answered with the real `Bad Request: query is too old and response timeout expired or query id is invalid`, instead of always succeeding. |
| `getChatMemberCount` | New. Excludes `left`/`kicked` members. |
| `restrictChatMember` | `use_independent_chat_permissions`: without it, `can_send_other_messages`/`can_add_web_page_previews` imply every "send media" permission and `can_send_polls` implies `can_send_messages`, exactly as documented. `until_date` under 30s or over 366 days from now collapses to "forever" (`until_date: 0`), matching the real rule. `getChatMember`'s "restricted" branch now renders the *actual* granted permissions instead of one fixed template. Status only becomes `"member"` once every permission is granted back. |
| `banChatMember` | Same `until_date` forever-collapsing rule as `restrictChatMember`. |
| `unbanChatMember` | `only_if_banned`: lifts a ban if there is one, otherwise leaves the member untouched, instead of always resetting to `"left"`. |
| `setChatPermissions` | New. Same permission normalisation as `restrictChatMember`, applied to the chat's default permissions. |
| `pinChatMessage`/`unpinChatMessage` | New (single most-recent pin only — see divergence table). |
| `setChatTitle`/`setChatDescription`/`exportChatInviteLink`/`leaveChat` | New. |
| `setMessageReaction` | New. Recorded in the API-call log; does not mutate the message's own rendered shape (see divergence table). |
| `setMyCommands`/`getMyCommands`/`deleteMyCommands` | `getMyCommands`/`deleteMyCommands` are new. Commands are actually stored and retrieved per `(scope, language_code)`, with a two-axis fallback approximating Telegram's own scope/language fallback chain — `setlang.py` sets commands per-chat, per-language, and this is what makes that call inspectable. |
| Unknown method / unimplemented real method | `404 Not Found: method not found` — the real server's exact wording (verified against community reports of the tdlib self-hosted server; the string was previously wrong, interpolating the method name into it). |
| Error envelope | `{"ok": false, "error_code", "description"}` plus an optional `parameters` object (`retry_after`/`migrate_to_chat_id`) mirroring `ResponseParameters` — wired at the `TelegramApiError`/dispatch level even though nothing raised today populates it (see divergence table). |
| Chat objects (private) | A private chat's `as_telegram()` now sends `first_name` (matching a real Telegram private `Chat`), not `title` — a private chat never has a `title` on the wire. |
| Private chats (addressing) | A DM's id **is** the user's id, as on real Telegram — that is the only id a handler answering privately (`bot.send_message(ctx.actor.user_id, ...)`) ever has. `POST /api/users/{id}/dm` opens one, standing in for the user pressing Start. Sending to a known user who has no DM yet returns Telegram's own `403 Forbidden: bot can't initiate conversation with a user`, not "chat not found": a bot may answer a conversation, never start one. |
| Service messages (join/leave) | A join or a leave is a *stored* message carrying `new_chat_members`/`left_chat_member` rather than text, exactly as Telegram models it — not merely a queued update. The captcha replies to the join message, and a reply needs a message to point at. |

### Deliberate divergences from real Telegram

| Method / area | Real behaviour | Sandbox behaviour | Why |
|---|---|---|---|
| Any method not in `_METHODS` | A genuinely unknown method 404s; a *real but unimplemented* method (payments, inline mode, stickers management, business accounts — see below) gets whatever method-specific error the real server would give it | Both cases get the same `404 Not Found: method not found` | The sandbox cannot tell "this method doesn't exist" from "this method exists but I haven't built it" without hand-listing every real Bot API method name. A 404 is never a silent 500, which is the property that actually matters for a bot polling this server. |
| Rate limiting / flood control | Real Telegram throttles and returns `429` with `parameters.retry_after` | Never throttles | UAT is a human (or a fast test loop) driving one bot against one sandbox process; simulating flood control would only make manual testing slower for no behavioural signal. The `parameters` field is still wired end-to-end (`tests/test_bot_api_compat.py::test_error_envelope_carries_parameters_when_given`) so nothing would need to change if this ever did get simulated. |
| Group → supergroup migration | `migrate_to_chat_id` in `parameters` when a method is called against a chat that has since migrated | Never happens — every sandbox chat is created once, as whatever type it starts as | Nothing in this tool ever converts a chat's type after creation. |
| `deleteMessage`'s 48-hour window | Deleting a message older than 48h (channels/other chats) fails | No time limit — a message can always be deleted (or a 400 if it doesn't exist/was already deleted) | Explicitly out of scope per this file's own review: enforcing it would make sandbox scenarios time-dependent for a rule that has no UAT payoff (nobody is testing "can the bot delete a week-old message" by hand). |
| `pinChatMessage`/`unpinChatMessage` | A chat can have several simultaneously pinned messages | Tracks only the single most recently pinned message per chat | Modelling more has no UAT payoff here. `getChat`'s response also never renders `pinned_message` at all (real Telegram does, as a full nested `Message`) — doing so would mean that nested `Message` embedding its own `chat`, which embeds `pinned_message` again, recursing forever; not worth the complexity for a field nothing in this codebase reads. |
| `setMessageReaction` | Aggregates every user's reaction into `Message.reactions` (a `ReactionCount` list) | Records the call in the API-call log (this tool's actual validation surface) and publishes an SSE event; does not mutate the message's own rendered `reactions` | Modelling the aggregate has no UAT payoff; "did the bot call `setMessageReaction`" is the question this sandbox answers, not "what does the reaction count look like". |
| `<pre><code class="language-x">...</code></pre>` (HTML parse_mode) | Collapses into one `pre` entity carrying `language` | Produces two entities: an inner `code` and an outer `pre`, neither carrying `language` | Nothing in this codebase's locale strings (`cb_core/locale_data/*/lib.json`) uses this construct — only bare `<b>`, `<i>`, `<blockquote>`, `<span class="tg-spoiler">` appear in real production content. |
| MarkdownV2 parse_mode | Every reserved character (`_*[]()~\`>#+-=\|{}.!`) outside an entity must be backslash-escaped, or the whole message is rejected | Only escapes what it's asked to; does not reject unescaped reserved characters elsewhere | Nothing in this codebase sends `parse_mode=MarkdownV2` today (the bot's `DefaultBotProperties` fixes `HTML`) — enforcing the strict rule would make MarkdownV2 nearly unusable for a human typing a sandbox test message by hand, for a mode with no current production traffic. |
| Legacy `parse_mode=Markdown` | A distinct, simpler dialect (no `__`, `~`, `\|\|`) | Routed through the MarkdownV2 parser | An approximation rather than a third parser for a deprecated mode nothing in this codebase sends. |
| Payments, inline mode, stickers management, business accounts | Full method families (`sendInvoice`, `answerInlineQuery`, `createNewStickerSet`, `setBusinessAccountName`, ...) | Not implemented — every method in these families 404s like any other unimplemented method | The sandbox has no concept of an invoice, an inline query, a sticker set editor, or a Telegram Business account, and cb-gateway/cb-worker do not call any of them. Building the machinery to back them would be pure speculation with nothing to validate against. |
| `getFile`/file downloads | Real file bytes | A fixed placeholder blob (`cb-sandbox-placeholder-file`) for every file | Nothing that reads a downloaded file in this codebase cares about its actual bytes (only content-type sniffing), so a real media pipeline isn't worth building for this tool. |
| Real Telegram API rate/size limits (message length, caption length, entity count, etc.) | Enforced, with specific `Bad Request` errors | Not enforced | Out of scope: these are input-validation edge cases with no UAT payoff for a human or a fast local test loop. |

### Reading this table

"Real semantic now honoured" does not mean "byte-for-byte identical to
`tdlib/telegram-bot-api`'s C++ implementation" — it means the observable
behaviour a real bot's handler code (aiogram parsing, admin checks, media
handlers, `TelegramBadRequest` retries) can actually depend on now matches.
Every payload in the table above is validated against aiogram's real pydantic
models in `tests/test_telegram_api.py` and `tests/test_bot_api_compat.py`,
not just eyeballed against the docs — see either file's module docstring for
why that step is the one that actually catches a regression here.
