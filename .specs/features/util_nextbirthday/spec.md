# util_nextbirthday — Specify

**Feature id:** `util_nextbirthday` · **Milestone:** M2 · **Kind:** v1 port
**v1 source:** `Bot/Birthdays.py:104-117` (`next_birthdays`), dispatched
`Bot/COOKIEBOT.py:244-245`.

Shares its investigation with `util_birthday` — see
`.specs/features/util_birthday/spec.md` for the full write-up (the
collection-mechanism finding, which applies identically here since both
features read the same `users.birthdate`, and the scope discussion). This
file only records what is specific to `/nextbirthday` itself.

## Behaviour contract (Phase 2)

| Aspect | v1 behaviour (file:line) |
|---|---|
| Triggers | `/proximosaniversarios`, `/nextbirthdays`, `/proximoscumpleanos` (`COOKIEBOT.py:244-245`) |
| Preconditions | `functionsFun` (same `elif` chain as `/birthday`) |
| Cooldowns / quotas | None |
| Success output | `bday.next` header (`"UPCOMING BIRTHDAYS (all groups):\n\n"` — hardcoded, never localised beyond `en`/`pt`/`es` catalog value, and the "(all groups)" wording is stale even for the manual, single-group invocation this port builds — preserved, a cosmetic label, not a behaviour bug) then, for `offset` in `1..4`: `f"{offset} dias:\n"` (also hardcoded Portuguese — `"dias"`, never `"days"`, regardless of group language — another preserved quirk) then one `@username`/`firstName lastName` line per person whose `birth_month`/`birth_day` matches `today + offset`, or `"- \n"` if nobody that day |
| Scope in v1's real function | `next_birthdays` takes a single `chat_id` and sends directly to it — no group iteration of its own; `Birthdays.py:112` sends one message via `send_message(cookiebot, chat_id)`. It is also v1's own 900-second cron follow-up target (`threading.Timer(900, next_birthdays, ...)`, `util_birthday`'s D-BD-2) — this port's manual command and the deferred-follow-up job (once `util_birthday`'s scope is confirmed) both call the same underlying function, matching v1's own reuse |
| Persistence | None — read-only |
| External calls | None beyond the reply itself — no photo, no Bot API call beyond `sendMessage` |

## Verbatim strings, both preserved quirks

- `bday.next`: `"UPCOMING BIRTHDAYS (all groups):\n\n"` — English only, all
  three locales carry the same key (already ported, `cb_core/locale_data/`).
- `f"{offset} dias:\n"` — literally not through `i18n` at all
  (`Birthdays.py:110`), Portuguese `"dias"` hardcoded regardless of language.

## QA

`../Cookiebot-QA/features/util_nextbirthday.feature` — one scenario, matches
v1 exactly, no conflict.

## Status

Same open decision as `util_birthday` — this feature is small enough that
it does not, on its own, need the collage/Pillow/`_defer_by` questions
answered, but it shares the same data source (`users.birthdate`) and is
built in the same slice once that's confirmed.
