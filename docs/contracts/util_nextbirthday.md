# Contract: util_nextbirthday (v1 -> v2)

Phase 2/6 of `/migrate-feature` for `/nextbirthday`. QA:
`../Cookiebot-QA/features/util_nextbirthday.feature`. FEATURE-MAP row:
`util_nextbirthday`. Spec: `.specs/features/util_nextbirthday/spec.md`;
design shared with `util_birthday` (`.specs/features/util_birthday/design.md`
R2/R4). Files owned: `packages/cb-gateway/src/cb_gateway/handlers/nextbirthday.py`
(new); `cb_core.birthdays`'s `all_users_with_birthday`/`next_birthdays_text`
are shared with `util_birthday`'s deferred follow-up, not owned solely here.

## Phase 1 — where v1 lives

- Handler: `next_birthdays`, `Bot/Birthdays.py:104-117`.
- Dispatch: `COOKIEBOT.py:244-245` — `/proximosaniversarios`,
  `/nextbirthdays`, `/proximoscumpleanos`, gated on `functionsFun`. Also
  v1's own 900-second cron-follow-up target from `birthday()`
  (`threading.Timer(900, next_birthdays, ...)`, `Birthdays.py:56-57` —
  `util_birthday`'s D-BD-2).

**Alias gap found and fixed**: `cb_core/textmatch.py:COMMAND_ALIASES` had
`nextbirthday`/`nextbirthdays`/`proximosaniversarios` mapped, but was
missing `proximoscumpleanos` — one of v1's three real triggers
(`COOKIEBOT.py:244-245`). Added as part of this port (AGENTS.md §1: no v1
trigger stops working).

## Phase 2 — v1 behaviour contract

| Aspect | v1 behaviour (file:line) |
|---|---|
| Triggers | `/proximosaniversarios`, `/nextbirthdays`, `/proximoscumpleanos` — `functionsFun` gated, same chain as `/birthday` |
| Preconditions | `functionsFun` only |
| Cooldowns / quotas | None |
| Success output | `bday.next` header (localised — `en`/`pt`/`es` each have their own translated value, all still saying "(all groups)"), then for `offset` in `1..4`: a **literal** `f"{offset} dias:\n"` line (not through `i18n` at all — hardcoded Portuguese `"dias"` regardless of the group's language, `Birthdays.py:110` — a genuinely different, second quirk from the header, both preserved), then one `@username`/`firstName lastName` line per person whose birthday falls on that day, or `"- \n"` if nobody does |
| Reply shape | `send_message(cookiebot, chat_id, text)` — **no** `msg_to_reply` (`:112`) — a new message, not a reply. Different from `/birthday`'s own reply-to-trigger shape. |
| Persistence | None — read-only |
| External calls | None beyond the reply — no photo, no external API |

## Scope — not group-scoped, matching v1 exactly (read this before "fixing" it)

`next_birthdays` reads the **raw, unfiltered** backend response (`GET
users?birthdate=`, `:109`) — every user in the whole system with an
upcoming birthday, not just members of the group that asked. This is
genuinely different from `/birthday`'s own collage, which explicitly
filters the same kind of list down to "and is in this group"
(`Birthdays.py:36-39`). `bday.next`'s own header text, "(all groups)", is
honestly describing this — it is not stale copy left over from a refactor,
it is what the command actually does.

**This port preserves that scope exactly** (`cb_core.birthdays.all_users_with_birthday`,
no `group_id` parameter at all) — per AGENTS.md's tie-break, v1 code wins
for observable behaviour. This has a real privacy dimension worth naming
plainly: any group can currently learn the upcoming birthday of a member of
a completely unrelated group via this command, in both v1 and this port.
Not silently narrowed to "this group only" on the assumption the wider
scope was unintentional — if narrowing it is wanted, that is a deliberate
product decision for someone to make explicitly, not an implementation
detail to slip in during a port.

## QA

`../Cookiebot-QA/features/util_nextbirthday.feature` — one scenario,
matches v1 exactly, no conflict. A net-new scenario proves the real query
against a seeded upcoming birthday, not just the list's shape.

## Phase 6 — parity table

| Aspect | Verdict |
|---|---|
| Triggers (including the fixed `proximoscumpleanos` alias gap) | **same** |
| `functionsFun` gate | **same** |
| Header, per-day line, "- " placeholder | **same, byte-identical, both quirks preserved** |
| Scope — not group-scoped | **same, deliberately preserved, privacy dimension flagged** |
| Reply shape (new message, not a reply-to) | **same** |
| Shared logic with `/birthday`'s deferred follow-up | **same as v1** — one function, two call sites, matching v1's own `next_birthdays` reuse from both its dispatch and its timer |

## Tests

| Layer | File |
|---|---|
| Unit — trigger surface | `packages/cb-gateway/tests/test_nextbirthday.py` |
| Unit — shared text builder | `packages/cb-core/tests/test_birthdays.py` |
| Acceptance — the QA scenario plus a seeded-birthday net-new scenario, against a real database (this feature's behaviour *is* the query) | `qa/features/util_nextbirthday.feature`, `qa/test_util_nextbirthday.py` |
