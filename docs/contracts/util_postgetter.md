# Contract: util_postgetter (v1 -> v2)

Phase 2/6 of `/migrate-feature` for the publisher's inbound half. QA:
`../Cookiebot-QA/features/util_postgetter.feature`. FEATURE-MAP row:
`util_postgetter`. Spec/design:
`.specs/features/util_postgetter/{spec,design,tasks}.md`.

Shipped alongside `util_postforwarder`, which owns the `scheduled_posts` table,
the pending-post cache and the delivery cron this feature's behaviour is
expressed through. Files owned here:
`packages/cb-gateway/src/cb_gateway/handlers/postgetter.py` and its one
registration line, plus the tests below.

## Phase 1 — where v1 lives

- `ask_publisher`, `../COOKIEBOT-Telegram-Group-Bot/Bot/Publisher.py:46-55`,
  with `add_post_to_cache` `:26-44`.
- Dispatch: `COOKIEBOT.py:165-166`, a six-way conjunction in the content-type
  chain.
- Delivery side: `scheduler_pull`'s per-group gate and topic routing,
  `Publisher.py:340-351`.

## Phase 2 — v1 behaviour contract

| Aspect | v1 behaviour (file:line) |
|---|---|
| Trigger | `content_type in [photo, video, document, animation]` **and** `sender_chat` **and** `forward_from_chat` **and** `from` **and** `caption` **and** `msg['from']['first_name'] == 'Telegram'` **and** `publisherask` (`COOKIEBOT.py:165`) |
| Preconditions | `publisher_ask`, default **1** (`Configurations.py:111`). No admin check, no fun/utility gate. |
| Cooldowns | None |
| Success output | a **reply** to the forwarded message: `"Divulgar postagem?"` when the group's language is `pt`, else `"Share post?"` — **there is no Spanish arm** (`:48`) |
| Buttons | `[✔️ → SendToApprovalPub {forward_from_chat.id} {chat_id} {forward_from_message_id} {message_id}]`, `[❌ → nPub]`. The ❌ payload carries **no** id, unlike the approval chat's `nPub {id}`, and `deny_post` returns early on a one-field payload (`:224-225`) — so it deletes the prompt and nothing else. |
| Failure output | none — no branch of `ask_publisher` can decline |
| Persistence | `cache_posts[str(forward_from_message_id)] = {media_type: file_id, 'caption', 'caption_entities'}`; a `document` is stored under the key `animation` (`:36-38`) |
| Side effects | `send_chat_action(typing)` (`:47`) |
| External calls | none |
| Delivery gate | config missing **or** `not publisher_post` (default **0**) ⇒ the queued row is **deleted**, not paused (`:342-345`) |
| Delivery | `forwardMessage` from the Mural — a forward, so Telegram renders the "Forwarded from" attribution the QA scenario calls "the original source" (`:347-351`) |
| Forum topic | when the target reports `is_forum`, `message_thread_id = int(config[10])` (`thread_posts`), sentinel `"9999"` included (`:348-349`) |
| Cap | `max_posts` (default 9999), enforced at schedule time, not delivery (`:261-267`) |

## Defects — verdict per item

| id | Defect | Verdict |
|---|---|---|
| D-PG-1 | `thread_posts`'s `"9999"` sentinel is passed to `message_thread_id` verbatim, so a forum group that never set a topic gets a forward into topic 9999 — which fails, which `scheduler_pull`'s catch-all then punishes by deleting the row | **fix** — v2 normalises the sentinel to NULL at the storage layer (`group_config.py:66-69`), and NULL means no argument at all |
| D-PG-2 | The post cache is a module-global dict | **fix** — shared with `util_postforwarder`'s D-PF-3; Valkey |
| D-PG-3 | An `es` group is prompted in English | **preserve** — user-visible, and inventing Spanish copy v1 never had is not this port's call |
| D-PG-4 | A group turning `publisher_post` off destroys its queued rows rather than pausing them | **preserve** — v1's semantics are that consent is checked at delivery and withdrawal is final |

D-PG-3 is implemented by **omitting** `publisher_ask_prompt` from the `es`
catalog and letting `locales.get`'s existing en fallback do it, rather than
copying the English string in — so the omission stays visible to anyone
diffing the catalogs. Asserted in
`packages/cb-gateway/tests/test_postgetter.py`.

## Registration order is the behaviour

v1's branch is an `elif` sitting **ahead** of the `photo`/`video` branches that
pool media into the group's random library (`COOKIEBOT.py:165-172`), so an
auto-forwarded ad is never also collected by `fun_random`. aiogram reproduces
that only through registration order, and only because this handler *replies* —
completing without `SkipHandler` is what stops propagation. Registered after
`fun_random`, every ad silently joins the random pool and nothing errors.
`postgetter.router` therefore sits immediately before `fun_random.router`, and a
unit test asserts the index.

When `publisher_ask` is off the handler raises `SkipHandler`, because in v1 the
branch was never entered either and the message continued down the chain.

`first_name == "Telegram"` is ported as the literal comparison v1 evaluates, not
replaced with a check on user id `777000` — that is a different, and in v1
untested, predicate.

## Phase 6 — parity table

| Aspect | Verdict |
|---|---|
| All six filter conditions, and the `first_name` discriminator | **same** |
| `publisher_ask` gate and its default | **same** |
| Prompt text for en and pt; `es` answered in English | **same** — D-PG-3 |
| Reply-vs-send, and which message it replies to | **same** |
| Button labels, the ✔️ payload, and the ❌ payload carrying no id | **same** |
| Media resolution, document filed as `animation` | **same** — shared with D-PF-4 |
| Cache keyspace (`forward_from_message_id`, so two groups collide and the later overwrites) | **same** |
| Cache storage | **changed (intentional, fix)** — D-PG-2 |
| Forum topic when none is configured | **changed (intentional, fix)** — D-PG-1 |
| Opting out destroys the backlog | **same** — D-PG-4, preserved |
| Delivery is a forward, not a re-send | **same** |
| `send_chat_action(typing)` | **changed (intentional)** — no ported command sends one; a no-op for a reply issued in the same round trip, consistent with the eleven already shipped |

## Tests

| Layer | File |
|---|---|
| Unit — the discriminator, the prompt in all three languages, the buttons, the registration index | `packages/cb-gateway/tests/test_postgetter.py` |
| Acceptance — the QA scenario plus three authored (the prompt, the prompt suppressed, and a group that opted out receiving nothing) | `qa/features/util_postgetter.feature`, `qa/test_util_postgetter.py` |

## QA vs v1 conflicts recorded

QA's one scenario asserts "the original source of the post and any relevant
information about it". Ported as: delivery is `forwardMessage`, so Telegram's
own attribution is the source, and the inline keyboard `prepare_post` attached
survives the forward. The spec has **nothing** for `publisher_ask` — the prompt
this feature is mapped to in `feature-map.mdx:57` — so that scenario is
authored, not ported.
