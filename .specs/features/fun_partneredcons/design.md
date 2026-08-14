# fun_partneredcons — Design

`spec.md` deliberately stopped before this file: every trigger sends a picture,
and there was no picture to send. The `Countdown/*` prefixes are exported and
catalogued now (`cb_worker.bucket_export`, `cb.py legacy-catalog`), including
the `Countdown/Trex` folder no v1 code path ever listed, so the whole feature is
ordinary handler work.

## Module placement

| Piece | Where | Reuses |
|---|---|---|
| Handler | `packages/cb-gateway/src/cb_gateway/handlers/partneredcons.py` (new) | `cb_core.legacy_assets`, `cb_core.storage`, `cb_gateway.context.context_for`, `CommandName` |
| `number_to_emojis` | `packages/cb-core/src/cb_core/publisher.py` | the `_KEYCAP_DIGITS` table its inverse already uses |
| Router registration | `handlers/__init__.py` | one line, six disjoint triggers |

No database, no migration, no worker job, no new dependency. One storage read
per invocation, the same shape `fun_death` and `fun_meme` already make on the
reply path.

## R1 — the event table

**R1.1** One frozen `Event` struct per convention in a module-level tuple, in
v1's own `elif` order: canonical command name, `legacy_assets` prefix, `cta`
key, hardcoded `(day, month, year)`, the caption template, and `days_span`
(v1's `day + N`, which differs per event: 3 for patas, 2 for bff/fursmeet/
pawstral, 4 for furcamp).

**R1.2** The caption is a `str.format` template rather than an f-string so the
table is data. Every literal — venue, ticket link, Telegram handle, emoji
run — is copied byte-for-byte from `Miscellaneous.py:274,285,296,307,318`,
including the events whose hardcoded date has already passed.

**R1.3** `/trex` is the same struct with an empty date, caption and `cta` key.
Nothing branches on "is this trex" anywhere; `caption_for` returning `None` is
what makes it caption-less.

## R2 — the countdown (pure)

**R2.1** `days_remaining(date, now)` — `(datetime(*date) - now).days + 1`, then
`while remaining < -5: remaining += 365`. Both v1's, both preserved.

**R2.2** `is_happening_now(remaining)` — `-5 <= remaining <= 0`, the window
where `caption_for` returns the bare YouTube link instead.

**R2.3** `render_caption(event, remaining, cta)` — `number_to_emojis(remaining)`
for the count, plus the *hardcoded* day/month/year, never a wrapped-forward
one.

**R2.4** `caption_for(event, now, lang)` composes the three and is the only
function the handler calls. `None` for an event with no caption (R1.3).

## R3 — the `cta` lookup

**R3.1** `locales.nested_value("event", <name>, lang)` returns the per-event
object; the `cta` list is read off it at the call site, the same shape
`groupguardian.py`'s `_captcha_strings` uses. v1's `i18n.get` takes a dotted
path, `nested_value` resolves one level, and one caller does not justify a
second lookup API.

**R3.2** Only the `en` catalog carries the `event` object (the `pt`/`es` files
have `event.error` alone — v1's own state, ported verbatim), so every language
gets the same Portuguese CTA lines through `nested_value`'s fallback. That is
the behaviour, not a gap.

## R4 — dispatch and gating

**R4.1** Six stacked `@router.message(CommandName(...))` decorators on one
handler — `owner.py`'s `/stop`+`/restart` idiom — because v1 has one function
whose body is an `elif` chain over the command word.

**R4.2** **No gate at all.** Not `ctx.enabled("fun")`, not `"utility"`. v1's
dispatch `elif` precedes the utility check and sits outside the fun block
(`COOKIEBOT.py:248-253`), so both switches miss it.

**R4.3** Reaction `🔥` then `send_chat_action("upload_photo")` before the pool
is read, so an empty pool looks to a user exactly like the moment before v1
would have crashed — `fun_death`'s R-ordering, same reasoning.

## R5 — the empty pool

**R5.1** `legacy_assets.choose` returns `None` where `legacy-catalog` has never
run; the handler logs `partneredcons.pool_empty` with the command and prefix
and returns. v1 raised `ValueError` inside `random.randint(0, -1)`.

## Open decisions — answered

1. **`/trex` gets a picture and no caption.** No date for the event exists in
   any reference repo; inventing one would be fabricating content about a real
   convention. QA asks for a picture, which is what it gets.
2. **`/trex` is ungated**, like the five it ships beside.
3. **`number_to_emojis` lives in `cb_core.publisher`**, next to the keycap
   table its inverse already uses, rather than in a new shared module written
   for one caller.
