# x_giveaways — Specify

**Feature id:** `x_giveaways` · **Area:** util · **Milestone:** M3 · **Kind:**
v1 port with no QA scenario (`docs/site/content/docs/feature-map.mdx` §4).

## Goal

`/giveaway <prize>` runs a raffle in the group: an admin names a prize, picks
how many people win, the bot announces and pins it, members press a button to
enter, and an admin ends it — at which point the bot draws the winners, posts
each one with their profile photo, and offers to draw again or close.

## Source of truth

`../COOKIEBOT-Telegram-Group-Bot/Bot/Giveaways.py:25-173`, dispatched at
`COOKIEBOT.py:249,262-263` (command) and `:415-428` (callbacks). The full
behaviour table, with file:line for every branch, is
`docs/contracts/x_giveaways.md` §Phase 2 — not repeated here.

## The thing to know before starting: v1's `/giveaway` does not work

`giveaways_ask` encodes the prize as `json.dumps(text)[:20]` into the callback
data (`:36`), the dispatcher strips every `"` out of it again
(`COOKIEBOT.py:421`), and `giveaways_create` then calls `json.loads` on what
comes back (`:54`). That raises for every prize that is not a bare JSON
literal. The exception is caught by v1's top-level handler, so the raffle is
never announced and the user is told nothing.

A second defect compounds it: the dispatcher's admin gate covers *every*
`GIVEAWAY` callback including `enter` (`COOKIEBOT.py:416-418`), so the
"Put me in!" button never worked for the members it invites.

**Consequence for this port**: "reproduce observable v1 behaviour" would mean
shipping a command that answers nothing. Both defects are fixed, and both are
recorded as D-GA-1/D-GA-2 in the contract with the evidence — including v1's
own redundant admin check inside `giveaways_end`, which is what shows the
outer gate was never meant to cover `enter`.

## Decisions

| # | Decision | Why |
|---|---|---|
| R1 | Two distributed tables (`giveaways`, `giveaway_participants`), both on `group_id`, colocated with `groups` | v1's `participants TEXT` column is a read-modify-write race and makes the display name the identity. A row per entrant makes "already in" a primary-key conflict. |
| R2 | The prize lives in Valkey behind a UUIDv7 token; the callback carries the token | Telegram caps `callback_data` at 64 bytes, which is why v1 truncated. Same shape `cb_core.pending_posts` already uses for the publisher's pending submission. |
| R3 | v1's callback vocabulary is preserved verbatim | `GIVEAWAY <n>` / `enter` / `end` / `delete` — nothing about a press changes. |
| R4 | Profile photos come from `get_user_profile_photos`, not a `telegram.me` scrape | `fun_battle`'s port already made this call and rejected the scrape (D-BT-1/2); this handler holds the real `user_id`, so it does not even need a roster lookup. |
| R5 | Per-sub-key locale fallback, not per-object | `es`'s `giveaway` object is missing ten entries (v1's own drift). An object-level fallback would answer a Spanish group with key names. |

## Success criteria

1. Every branch of v1's five functions has an equivalent, with the five
   defects above fixed and recorded.
2. `qa/features/x_giveaways.feature` — authored, since no QA file exists —
   passes against the real dispatcher, a real Postgres and a real Valkey.
3. Integration tests prove chat-scoping, atomic entry under concurrency, the
   cascade, and `Task Count: 1` for both hot reads.
4. `ruff`, `mypy`, `cb.py migrate-check` and `cb.py check` all clean.
