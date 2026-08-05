# util_postforwarder — Specify

**Feature id:** `util_postforwarder` · **Milestone:** M3 · **Kind:** v1 port
**v1 source:** `Bot/Publisher.py` — `ask_publisher_command` (`:57-75`),
`ask_approval` (`:77-92`), `prepare_post` (`:182-221`), `deny_post` (`:223-228`),
`schedule_post` (`:230-286`), `schedule_autopost` (`:288-314`),
`scheduler_pull` (`:329-357`), `check_notify_post_reply` (`:359-369`), and the
job store `create_job`/`list_jobs`/`delete_job`/`edit_job_data` (`:94-127`).
Dispatched at `COOKIEBOT.py:205` (`/divulgar`), `:208` (`/repost`), `:303`
(reply relay), `:370-375` (the three callback branches), `:448-455`
(`scheduler_check`, a 300s `threading.Timer` chain).

## Goal

The outbound half of v1's publisher: a channel ad is submitted, approved by the
bot owner, rendered once into the "Mural" postmail channel in Portuguese and
English, and then forwarded into every consenting group on a randomised daily
schedule for N days. Plus `/repost`, the same scheduler used to re-post one
message inside a single group, and the reply relay that carries a group
member's answer back to the post's author.

`util_postgetter` is the receiving half and `util_deletereposts` the cancel;
all three share the schedule table and the cron introduced here. This feature
owns both.

## Scope

**In:** everything listed under *v1 source* above, the `scheduled_posts` table
that replaces `Publisher.db`, and the arq cron that replaces
`scheduler_check`.

**Out:** the `publisher_ask` prompt on an auto-forwarded channel post
(`ask_publisher`, `:46-55`) — that is `util_postgetter`'s trigger, specced
there. `cancel_posts` (`:316-327`) — `util_deletereposts`.

## Phase 2 — v1 behaviour contract

### Triggers

| Trigger | v1 (file:line) | Gate |
|---|---|---|
| `/divulgar`, `/publish`, `/publicar` | `COOKIEBOT.py:205` → `ask_publisher_command` | **none** — no admin check, no `functionsUtility`, no `functionsFun`. Anyone in any group. |
| `/repost`, `/repostar`, `/reenviar` | `COOKIEBOT.py:207-208` → `schedule_autopost` | admin, `ownerID`, or `sender_chat` present (`:290`) |
| Callback `SendToApprovalPub …` | `COOKIEBOT.py:370-371` → `ask_approval` | none |
| Callback `yPub …` | `COOKIEBOT.py:372-373` → `schedule_post` | **none** — see D-PF-1 |
| Callback `nPub …` | `COOKIEBOT.py:374-375` → `deny_post` | none |
| A text reply to a bot message that carries a `reply_markup` | `COOKIEBOT.py:302-303` → `check_notify_post_reply` | none; sits in the `elif` chain *after* the captcha-reply and complaint-reply branches and *before* the conversational-AI branch |
| 300-second timer | `COOKIEBOT.py:448-455` → `scheduler_pull` | primary bot process only |

All six command spellings already resolve in `cb_core.textmatch.COMMAND_ALIASES`
(`textmatch.py:63-64`) — `publish` and `repost` canonical.

### `/divulgar` — `ask_publisher_command` (`:57-75`)

| Aspect | v1 behaviour |
|---|---|
| Precondition 1 | `'reply_to_message' not in msg` ⇒ reply `publish_need_reply` (strings below), return (`:59-62`) |
| Precondition 2 | replied message lacks `forward_from_chat` **or** `forward_from_message_id` ⇒ reply `publish_not_channel`, return (`:63-66`) |
| Precondition 3 | replied message lacks `caption` ⇒ reply `publish_needs_media`, return (`:67-70`) |
| Success | cache the replied post, call `ask_approval(f"SendToApprovalPub {replied.forward_from_chat.id} {chat_id} {replied.forward_from_message_id} {replied.message_id}", msg.from.id)`, reply `publish_sent_for_approval` (`:71-75`) |
| Side effect | `send_chat_action(typing)` first, every branch (`:58`) |

Note `add_post_to_cache` (`:26-44`) picks the media by first match of
`photo`(last size)/`video`/`animation`/`document`, and **stores a `document`
under the key `animation`** (`:36-38`) — so a document ad is later re-sent with
`sendAnimation`. Preserved (D-PF-4).

### Approval — `ask_approval` (`:77-92`)

Forwards the *requester's own message* (`second_chatid`/`second_messageid`) into
`APPROVAL_CHAT_ID` (`-1001659344607`, hardcoded `:22`), then sends
`'Approve post?'` — English only, never localised — with five buttons:

| Button text | callback_data |
|---|---|
| `✔️ 7 days (NSFW)` | `yPub {origin_chatid} {second_chatid} {origin_messageid} {origin_userid} 7 {second_messageid} 1` |
| `✔️ 7 days` | `… 7 {second_messageid} 0` |
| `✔️ 3 days` | `… 3 {second_messageid} 0` |
| `✔️ 1 day` | `… 1 {second_messageid} 0` |
| `❌` | `nPub {origin_messageid}` |

Before any callback branch runs, `COOKIEBOT.py:367-369` **deletes the message
carrying the button**, best-effort.

### Render — `prepare_post` (`:182-221`)

Runs on approval, against the cached post. Builds one inline keyboard, in this
order:

1. `[origin_chat['title'] → https://t.me/{origin_chat['username']}]` (`:185`)
2. For each **unique** URL matched by `URL_REGEX` in the caption (`:186-191`):
   `name = url.rstrip('/').split('/')[-1].replace('www.','')`, and
   `url_no_emojis_on_ends = remove_emojis_from_ends(url)`. Emitted only when
   `len(name)` is truthy **and** `len(url_no_emojis_on_ends) > 3` **and** it is
   not the origin channel's own link. The caption then has `url` replaced by
   the de-emojified form.
3. For each `caption_entities` item carrying a `url`, while `len(entity['url']) > 3`
   **and the keyboard is still shorter than 5 rows** (`:193-196`):
   `name = url.rstrip('/').replace('www.','').replace('http://','').replace('https://','')`
4. If `origin_user` is not `None` **and** `'Mekhy' not in origin_user['first_name']`:
   `[first_name → https://t.me/{username}]` (`:197-198`)
5. Always: `[Mural 📬 → POSTMAIL_CHAT_LINK]` (`:199`, `https://t.me/CookiebotPostmail`)

Caption pipeline (`:192,200-209`): `emojis_to_numbers` (keycap emoji → ASCII
digit) → `translate(.., 'pt')` and `translate(.., 'en')` → `convert_prices_in_text`
against `BRL`/`USD` respectively → `<`→`⩽`, `>`→`⩾`, `&`→`＆` → truncate to
1020 chars → if the string contains `'Error 500 (Server Error)'`, discard the
whole translation and fall back to the untranslated caption.

Then **two** sends to `POSTMAIL_CHAT_ID` (`-1001869523792`, hardcoded `:21`) —
the pt caption then the en caption, same media, same keyboard (`:210-218`).
`sendPhoto` passes `parse_mode='HTML'`; `sendVideo` and `sendAnimation`
**do not** (D-PF-5). Returns both message ids. The cache entry is popped
(`:219-220`).

`convert_prices_in_text` (`:129-173`): short-circuits when the target is `BRL`
and the text already mentions `R$`/`BRL`/`Reais`/`reais`. Otherwise, per
paragraph, takes the **largest** parsed amount and the **last** parsed currency
(`price_parser.Price.fromstring(word, currency_hint='usd')`), maps the symbol to
an ISO code, and appends ` ({code_target} ≈{converted})` using
`https://v6.exchangerate-api.com/v6/{key}/latest/{code_from}`, `timeout=10`.
Any exception ⇒ the paragraph is emitted unchanged. `code_from == code_target`
⇒ the **whole original text** is returned immediately, discarding paragraphs
already converted (D-PF-6).

### Fan-out — `schedule_post` (`:230-286`)

| Step | v1 behaviour |
|---|---|
| Parse | 8 space-separated fields off the callback data (`:231`) |
| Lookups | `getChat(origin_chatid)`; `getChatMember(origin_chatid, origin_userid)['user']`, `None` on any exception (`:232-236`) |
| Render | `prepare_post` (`:237`) |
| Dedupe | every existing job whose name before `-->` equals this origin chat's title is deleted first (`:238-242`) — one live campaign per source channel |
| Header | `answer = f"Post set for the following times ({days} days):\nNOW - Cookiebot Mural 📬\n"` (`:243`) |
| Per group | iterate the backend's `registers` list — **every group the bot knows**, not just the requester's (`:244`) |
| Skip: no config | `except TypeError: continue` (`:246-248`) |
| Skip: opted out | `not publisherpost` (`:249`) |
| Skip: NSFW into an SFW group | `has_nsfw == '1' and sfw` (`:249`) |
| Skip: members-only | `publisher_members_only` and the author's username is not in that group's member register; any exception also skips (`:251-257`) |
| Cap | counts existing jobs whose name contains `--> {target_title}`; while the count exceeds `max_posts`, deletes jobs as it walks (`:261-267`) — see D-PF-7 |
| Schedule | `hour = randint(0,23)`, `minute = randint(0,59)`; `postmail_message_id = sent_pt if language == 'pt' else sent_en` (`:268-270`) |
| Row | `create_job` writes `next_time` = **tomorrow** at `hour:minute` (`:96` adds `timedelta(days=1)` unconditionally) |
| Line | `answer += f"{hour}:{minute} - {target_chattitle}\n"` (`:274`) |
| Any per-group error | `except Exception: pass` — group silently absent from the schedule (`:275-276`) |
| Footer | `answer += "OBS: private chats are not listed!"` (`:278`) |
| Report | DM `ownerID`, DM `origin_userid`, then reply in the requester's chat: `"Post added to the publication queue!"` (`:279-282`) |
| Report failed | DM the traceback to `ownerID`, and reply instead: `"Post added to the publication queue, but I was unable to send you the times.\n<blockquote> Send /start in my DM so I can send you messages. </blockquote>"` (`:283-286`) |

### `/repost` — `schedule_autopost` (`:288-314`)

| Aspect | v1 behaviour |
|---|---|
| Admin gate | `str(from.id) not in listaadmins_id and int(from.id) != ownerID and 'sender_chat' not in msg` ⇒ `not_group_admin`, return (`:290-293`). Note `listaadmins_id` is fetched with `ignorecache=True` (`COOKIEBOT.py:207`) |
| No reply | ⇒ `repost_need_reply`, return (`:294-297`) |
| Bad arg | a second word that is not `.isnumeric()` ⇒ `repost_bad_days`, return (`:298-302`) |
| Days | the numeric arg, else `9999` (`:303,306`) |
| Schedule | `hour = randint(10,17)`, `minute = randint(0,59)` — a **daytime** window, unlike `schedule_post`'s all-day one (`:310-311`) |
| Row | source chat, target chat and requester chat are all this group; source message and requester message are both the replied message (`:312`) |
| Output | react `👍`, then reply `repost_scheduled_days` / `repost_scheduled_nolimit`, `parse_mode='HTML'` (`:313-314`) |

### Scheduler — `scheduler_pull` (`:329-357`)

Every 300 s, over every row:

1. `next_time` in the future ⇒ skip (`:333-334`).
2. `days <= 1` ⇒ delete the row; else decrement `days` and set
   `next_time = now + 1 day` (`:335-339`). **The decrement happens before the
   send and regardless of whether it succeeds.**
3. Target group's config missing, or `publisher_post` off ⇒ delete the row,
   skip (`:342-345`). A group that opts out drains its backlog permanently.
4. `getChat(target)`; when `is_forum` is set, forward with
   `message_thread_id = int(config[10])` (`thread_posts`), else a plain forward
   (`:347-351`).
5. `BotWasKickedError` ⇒ delete the row (`:352-354`).
6. Any other exception ⇒ DM the traceback to `ownerID` **and delete the row**
   (`:355-357`) — one transient Telegram error kills the campaign for that
   group (D-PF-8).

### Reply relay — `check_notify_post_reply` (`:359-369`)

Fires when someone replies to a bot message that has a `reply_markup`. Walks
every job and takes the **first** whose `name` starts with the text of
`reply_to_message.reply_markup.inline_keyboard[0][0]` — i.e. the origin
channel's title, which is button row 1 from `prepare_post`. Then:

- DMs `second_chatid`, replying to `second_messageid`:
  `f"@{username}"` (or `f"{first_name} {last_name}"`) `+ f" replied:\n'{text}'\n\nIn chat {chat_title}"`
- Replies in the group: `notify_post_reply_sent`
- Returns after the first match.

### Strings — verbatim, all three languages

None of these live in `Bot/Static/locales/`; every one is an inline ternary, so
they are new keys in v2's `cb.json` overlay (never `lib.json`, which stays a
byte-identical copy of v1's).

| key | en | pt | es |
|---|---|---|---|
| `publish_need_reply` | `You need to reply to a message with the command for me to be able to share it!` | `Você precisa responder a uma mensagem com o comando para eu poder divulgar ela!` | `¡Debes responder un mensaje con el comando para que pueda compartirlo!` |
| `publish_not_channel` | `This message is not from a channel!` | `Essa mensagem não é de um canal!` | `¡Este mensaje no es de un canal!` |
| `publish_needs_media` | `This ad needs to have a photo, video or GIF` | `O anúncio precisa ter uma foto, vídeo ou GIF` | `¡El anuncio necesita tener una foto, vídeo o GIF!` |
| `publish_sent_for_approval` | `Post sent for approval, please wait` | `Post enviado para aprovação, aguarde` | `Publicación enviada para aprobación, por favor espere` |
| `not_group_admin` | `You are not a group admin!` | `Você não é um administrador do grupo!` | `¡No eres un administrador del grupo!` |
| `repost_need_reply` | `You need to reply to a message with the command for me to be able to repost it in this group!` | `Você precisa responder a uma mensagem com o comando para eu poder repostar ela nesse grupo!` | `¡Debes responder un mensaje con el comando para que pueda compartirlo en este grupo!` |
| `repost_bad_days` | `You need to put a valid number of days!` | `Você precisa colocar um número de dias válido!` | `¡Debes poner un número de días válido!` |
| `repost_scheduled_days` | `Repost scheduled for the group for %(days)s days!` | `Repostagem programada para o grupo por %(days)s dias!` | `¡Reposteo programado para el grupo por %(days)s días!` |
| `repost_scheduled_nolimit` | `Repost scheduled for the group! (no limit of days)` | `Repostagem programada para o grupo! (sem limite de dias)` | `¡Reposteo programado para el grupo! (sin límite de días)` |
| `notify_post_reply_sent` | `Reply sent to the owner of the post!` | `Resposta enviada ao dono do post!` | `¡Respuesta enviada al dueño del post!` |
| `publish_queued` | `Post added to the publication queue!` | *(same — v1 never localises this one, `:281`)* | *(same)* |
| `publish_queued_no_dm` | `Post added to the publication queue, but I was unable to send you the times.\n<blockquote> Send /start in my DM so I can send you messages. </blockquote>` | *(same, `:285`)* | *(same)* |

`'Approve post?'` (`:84`) stays a module constant, not a locale key: the
approval chat is one operator chat with one language, and v1 hardcodes it.

### Persistence

v1: a **local SQLite file** `Publisher.db`, one table `publisher`, opened once
at import with `check_same_thread=False` and no lock (`:15-17`). Columns:
`name TEXT, days INT, next_time TEXT, target_chat_id INT, postmail_chat_id INT,
second_chatid INT, postmail_message_id INT, second_messageid INT,
origin_userid INT`. There is no primary key; `name` is the de-facto key and is
also *parsed* three different ways (`split('-->')[0]`, `f"--> {title}" in name`,
`name.startswith(button_text)`).

Also `cache_posts`, a module-global dict keyed by `forward_from_message_id`
(`:19`), holding the pending post between the prompt and the approval.

### External calls

| Call | Failure behaviour in v1 |
|---|---|
| Google Cloud Translate v2 (`translate`, `universal_funcs.py:139-161`) | uncaught — but the `'Error 500 (Server Error)'` check at `:206-209` shows the client returns an error *page* rather than raising, and v1 falls back to the untranslated caption |
| exchangerate-api v6, `timeout=10` (`:167-168`) | caught per paragraph; paragraph emitted unchanged (`:171-172`) |
| Telegram `getChat` / `getChatMember` / `forwardMessage` / `sendPhoto` … | per-group `except Exception: pass` in the fan-out; row-deleting in the scheduler |
| The Java backend `registers` and `configs` endpoints | `except TypeError: continue` |

### Known defects

| id | Defect | v1 | Verdict |
|---|---|---|---|
| D-PF-1 (=FEATURE-MAP D5) | `Publisher.db` is one shared SQLite connection, `check_same_thread=False`, no lock, across every worker thread | `:15-16` | **fix** — a distributed Postgres table |
| D-PF-2 | **Anyone can press `yPub`.** The approve buttons only ever appear in the private approval chat, so v1 relies on chat membership as the authorisation — but the callback branch itself checks nothing, and a callback id can be replayed by anyone who learns the payload shape | `COOKIEBOT.py:372-373` | **fix** — the press must come from `APPROVAL_CHAT_ID`, and be rejected otherwise |
| D-PF-3 | `cache_posts` is a process-global dict — the pending post is lost on restart, and is invisible to every other replica | `:19,44,183` | **fix** — the cache moves to Valkey with a TTL (v2 is horizontally replicated; a module dict is v1's D6 all over again) |
| D-PF-4 | A `document` ad is cached under the key `animation` and re-sent with `sendAnimation` | `:36-38` | **preserve** — user-visible, and `sendAnimation` does accept a document file id for a GIF, which is what these ads are |
| D-PF-5 | `parse_mode='HTML'` on `sendPhoto` only; video and animation captions are sent unparsed | `:211-218` | **preserve** — changing it would start rendering raw `<`/`>` differently in half the posts |
| D-PF-6 | `convert_prices_in_text` returns the *whole original text* when a paragraph's currency already equals the target, discarding conversions already appended to earlier paragraphs | `:164-165` | **preserve** — pure output quirk, no correctness or safety impact, and "fixing" it changes the caption of every mixed-currency ad |
| D-PF-7 | The `max_posts` trim deletes jobs while iterating the same list it is counting, and compares `>` against a count it has already decremented — the number of surviving jobs per group is order-dependent | `:261-267` | **fix** — see design R4.3; the *intent* (cap the campaigns targeting one group) is preserved, the arithmetic is made deterministic |
| D-PF-8 | Any non-kick exception during a scheduled forward deletes the row, so one transient 5xx ends the campaign for that group | `:355-357` | **fix** — transient failures retry; only a kick, a missing chat, or an exhausted retry budget deletes |
| D-PF-9 | `days` is decremented and `next_time` advanced *before* the forward is attempted, so a failed send still burns a day | `:335-339` | **preserve** — the alternative is unbounded retries of a permanently broken target, and D-PF-8's fix already covers the transient case |
| D-PF-10 | `'Mekhy' not in origin_user['first_name']` — a hardcoded personal name suppresses the author button | `:197` | **fix** — becomes `settings.publisher_hidden_author_names`, defaulting to `("Mekhy",)` so behaviour is identical out of the box |
| D-PF-11 | `scheduler_pull` runs only in the primary bot process, from a recursive `threading.Timer`; a crash between ticks silently stops every scheduled post forever | `COOKIEBOT.py:448-455` | **fix** — arq cron |

## QA vs v1 conflicts

1. **`util_postforwarder.feature` describes the outcome, not the mechanism.**
   Both scenarios say "a post is forwarded from group a to the bot" and assert
   group b does or does not receive it. v1's actual flow requires an owner
   approval step between the two, and delivery is on a randomised daily
   schedule rather than immediate. The scenarios are ported as written, with
   the approval press and a scheduler tick made explicit `When` steps — intent
   preserved, mechanism made honest. Recorded in `feature-map.mdx`.
2. **The publisher has no Gherkin for the approval workflow at all** — it is
   prose only, in `Cookiebot-QA/features/publicador(PTBR).md`, which
   `feature-map.mdx:60` already flags. Scenarios for `/divulgar`, the approval
   press, `/repost` and the reply relay are **authored**, not ported.

## Infrastructure that must become configuration

v1 hardcodes four identifiers of one specific deployment. v2 is multi-tenant, so
each becomes a setting, and the feature is inert when unset:

| v1 constant | v1 value | v2 setting |
|---|---|---|
| `POSTMAIL_CHAT_ID` (`:21`) | `-1001869523792` | `CB_POSTMAIL_CHAT_ID` |
| `POSTMAIL_CHAT_LINK` (`:20`) | `https://t.me/CookiebotPostmail` | `CB_POSTMAIL_CHAT_LINK` |
| `APPROVAL_CHAT_ID` (`:22`) | `-1001659344607` | `CB_APPROVAL_CHAT_ID` |
| `ownerID` | env `ownerID` | `settings.owner_id` — **already exists** (`settings.py:138`) |
