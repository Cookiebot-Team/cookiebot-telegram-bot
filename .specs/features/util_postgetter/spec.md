# util_postgetter — Specify

**Feature id:** `util_postgetter` · **Milestone:** M3 · **Kind:** v1 port
**v1 source:** `Bot/Publisher.py:26-55` (`add_post_to_cache`, `ask_publisher`),
dispatched at `COOKIEBOT.py:165-166`; the receiving side is
`scheduler_pull`'s per-group gate and topic routing (`Publisher.py:342-351`).

## Goal

The *inbound* half of the publisher: what a group experiences as a consumer of
the network. Two distinct things, both gated by config the group owns:

1. **`publisher_ask`** — when Telegram auto-forwards a linked channel's post
   into the group, the bot offers to push it into the publisher network
   ("Share post?" with ✔️/❌).
2. **`publisher_post`** — whether scheduled posts from *other* groups are
   delivered here at all, into which forum topic (`thread_posts`) and how many
   campaigns may target this group at once (`max_posts`).

## Scope

**In:** the `ask_publisher` prompt and its cache write; the delivery-time
`publisher_post` re-check, `is_forum` / `thread_posts` topic routing, and the
`max_posts` cap as they are observed *by the receiving group*.

**Out:** everything downstream of the ✔️ press — `ask_approval` onward is
`util_postforwarder`, which also owns the `scheduled_posts` table and the cron
that drives delivery. This feature contributes the gate and the routing that
cron consults.

## Phase 2 — v1 behaviour contract

### Trigger — `ask_publisher` (`:46-55`)

Dispatched from the media branch of the content-type chain
(`COOKIEBOT.py:165-166`), which fires only when **all** of these hold:

| Condition | v1 (file:line) |
|---|---|
| `content_type in ["photo", "video", "document", "animation"]` | `COOKIEBOT.py:165` |
| `'sender_chat' in msg` | ibid |
| `'forward_from_chat' in msg` | ibid |
| `'from' in msg` | ibid |
| `'caption' in msg` | ibid |
| `msg['from']['first_name'] == 'Telegram'` | ibid — the sender identity Telegram itself uses when auto-forwarding a linked channel post |
| `publisherask` is on (default **1**, `Configurations.py:111`) | ibid |

This branch sits **before** the plain `photo`/`video` branches, so an
auto-forwarded channel post is never also pooled into `fun_random`'s library
(`COOKIEBOT.py:167-172`) — the `elif` chain guarantees exactly one.

### Behaviour

| Aspect | v1 behaviour |
|---|---|
| Side effect | `send_chat_action(typing)` (`:47`) |
| Text | `"Divulgar postagem?"` when `language == "pt"`, else `"Share post?"` — **there is no Spanish variant**; an `es` group is answered in English (`:48`) |
| Shape | a **reply** to the forwarded message (`msg_to_reply=msg`) (`:49`) |
| Buttons | `[✔️ → SendToApprovalPub {forward_from_chat.id} {chat_id} {forward_from_message_id} {message_id}]`, then `[❌ → nPub]` (`:50-54`). Note the ❌ payload here carries **no message id**, unlike the approval chat's `nPub {origin_messageid}` — and `deny_post` (`:224-225`) returns early on a payload shorter than two fields, so this ❌ does nothing but delete the prompt |
| Persistence | `add_post_to_cache(msg)` (`:55`) — a module-global dict keyed by `str(forward_from_message_id)` holding `{media_type: file_id, 'caption': str, 'caption_entities': list}` |
| Cooldowns | none |
| Failure output | none — no branch of `ask_publisher` can decline |

`add_post_to_cache` (`:26-44`) resolves the media by first match:
`photo` → `msg['photo'][-1]['file_id']` (largest size), `video`, `animation`,
`document`. A `document` is stored under the key **`animation`** (`:36-38`).
If the message carries none of the four, `media_type` and `media_id` are
unbound and the function raises `UnboundLocalError` — unreachable from this
trigger, which already requires one of the four content types.

### Delivery — the receiving group's view of `scheduler_pull` (`:340-351`)

| Aspect | v1 behaviour |
|---|---|
| Gate | `config` missing **or** `not config[8]` (`publisher_post`, default **0**) ⇒ the row is **deleted**, not skipped (`:342-345`). A group that switches the setting off drains its whole backlog permanently rather than pausing it |
| Delivery | `forwardMessage` from `POSTMAIL_CHAT_ID` — a *forward*, so Telegram renders the "Forwarded from" attribution that the QA scenario calls "the original source" (`:347-351`) |
| Forum topic | when the target chat reports `is_forum`, `message_thread_id = int(config[10])` (`thread_posts`); v1 stores the string `"9999"` as its "no topic" sentinel and passes it through unchanged, so a forum group that never configured a topic gets `message_thread_id=9999` and the forward fails (`:348-349`) — D-PG-1 |
| Cap | `max_posts` (default `9999`) bounds how many campaigns may target this group; enforced at *schedule* time, not delivery time (`Publisher.py:261-267`) |

### Known defects

| id | Defect | v1 | Verdict |
|---|---|---|---|
| D-PG-1 | `thread_posts`'s `"9999"` sentinel is passed to `message_thread_id` verbatim, so a forum group with no configured topic gets a failing forward — which `scheduler_pull`'s catch-all then punishes by deleting the row | `:348-349` | **fix** — v2 already normalises the sentinel to `NULL` at the storage layer (`group_config.py:66-69`, and the M4 ETL does the same), so "no topic" means no `message_thread_id` argument at all |
| D-PG-2 | The post cache is a module-global dict, lost on restart and invisible to other replicas | `:19` | **fix** — shared with `util_postforwarder`'s D-PF-3; the cache moves to Valkey |
| D-PG-3 | An `es` group is prompted in English | `:48` | **preserve** — user-visible, and inventing a Spanish string here would be v2 authoring copy v1 never had. Recorded in the contract |
| D-PG-4 | A group opting out of `publisher_post` destroys its queued rows rather than pausing them | `:342-345` | **preserve** — v1's semantics are "consent is checked at delivery, and withdrawal is final"; keeping rows for a group that has opted out is a worse default than dropping them |

## QA vs v1 conflicts

`Cookiebot-QA/features/util_postgetter.feature` has one scenario: *"Getter
feature is set on the group and user views a post forwarded … they should see
the original source of the post and any relevant information about it."*

- "the original source" ⇒ Telegram's own forward attribution, which follows
  from using `forwardMessage` rather than a re-send. Asserted as such.
- "any relevant information" ⇒ the inline keyboard `prepare_post` attaches
  (origin channel, ad links, author, Mural). That keyboard is built by
  `util_postforwarder`; this scenario asserts it survives the forward.
- The scenario says nothing about `publisher_ask`, the prompt that
  `feature-map.mdx:57` maps this feature to. A scenario for the prompt is
  **authored**, not ported.
