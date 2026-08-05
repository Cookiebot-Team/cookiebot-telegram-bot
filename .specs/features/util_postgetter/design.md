# util_postgetter — Design

Consumes `util_postforwarder`'s shared pieces; see
`.specs/features/util_postforwarder/design.md` R1 (the `scheduled_posts`
table), R2 (the pending-post cache) and R7 (the delivery cron). This document
covers only what is this feature's own.

## R1 — the prompt handler

**R1.1** `packages/cb-gateway/src/cb_gateway/handlers/postgetter.py`, one
handler. The filter reproduces `COOKIEBOT.py:165`'s six-way conjunction as an
aiogram magic filter plus one predicate:

```python
F.chat.type.in_({"group", "supergroup"})
& (F.photo | F.video | F.animation | F.document)
& F.sender_chat
& F.forward_from_chat        # aiogram 3 keeps the legacy field populated
& F.from_user
& F.caption
```

plus `message.from_user.first_name == "Telegram"`. That last one is v1's real
discriminator for "Telegram auto-forwarded this from the linked channel" and it
is a literal string comparison in v1 — kept as one, not replaced with a check
on user id `777000`, which is a different (and in v1 untested) predicate.

**R1.2** `ctx.config.publisher_ask` gates it. Default `True`
(`group_config.py:64`, matching v1's `Configurations.py:111`). When off, the
handler raises `SkipHandler` so the message continues down the chain.

**R1.3 Text.** `t(ctx, "publisher_ask_prompt")` — but the catalog carries only
`en` and `pt` entries for this key, and `es` deliberately resolves to the
English text (D-PG-3). That is expressed by *not* adding an `es` entry and
letting `locales.get`'s existing fallback do it, rather than by copying the
English string into the `es` file, so the omission stays visible to anyone
diffing the catalogs.

**R1.4 Buttons.** Built by `cb_gateway.handlers.publisher.build_approval_request`
(imported, not duplicated — `util_postforwarder` owns the callback wire):
`✔️ → "SendToApprovalPub {forward_from_chat.id} {chat_id} {forward_from_message_id} {message_id}"`,
`❌ → "nPub"` with **no** message id, exactly as v1 (`:52`). The bare `nPub`
is a deliberate no-op beyond deleting the prompt — `deny_post` returns early on
a one-field payload (`:224-225`) — and the port keeps that rather than
"improving" it into a cache eviction v1 never performed.

**R1.5 The cache write.** `pending_posts.put(...)` keyed on
`forward_from_message_id`, media resolved in v1's order with a document stored
as `animation` (D-PF-4). The resolver is
`cb_core.publisher.resolve_pending_media(message)`, shared with `/divulgar`,
which runs the same `add_post_to_cache` against a *replied* message.

**R1.6 Registration order.** Immediately before `fun_random.router` in
`build_router`. v1's `elif` chain puts this branch ahead of the
`add_to_random_database` branches (`COOKIEBOT.py:165-172`), so an
auto-forwarded channel ad must never also be pooled into the group's random
library. The handler *replies*, so it completes without `SkipHandler` and
aiogram stops — which is what reproduces the `elif`. Registering it after
`fun_random` would silently pool every ad.

## R2 — delivery-side behaviour this feature owns

**R2.1** The `publisher_post` re-check and the row-deleting semantics of
opting out (D-PG-4) live in `util_postforwarder`'s cron (its R7.2), because
that is the only code that can perform them. This feature's contribution is
the assertion that they behave as specced, and the QA scenario that proves a
`publisher_post = false` group receives nothing.

**R2.2 The `thread_posts` fix (D-PG-1).** `group_config.thread_posts` is
already `str | None` with the `"9999"` sentinel normalised to `NULL`
(`group_config.py:66-69`). The cron passes `message_thread_id` only when the
value is not `None` *and* the target chat reports `is_forum`. No code in this
feature needs to know about `"9999"`; the guard is that nothing reintroduces
it.

## R3 — telemetry

**R3.1** `cb_gateway_publisher_ask_total{outcome}`, outcome in
`prompted|disabled`. No group label (AGENTS.md §7).

## Open decisions — answered

1. **`first_name == "Telegram"`, not user id `777000`.** R1.1 — port the
   predicate v1 actually evaluates.
2. **`es` falls back to English by omission, not by duplication.** R1.3.
3. **Registered ahead of `fun_random`.** R1.6 — this is the `elif` order, and
   getting it wrong is silent.
