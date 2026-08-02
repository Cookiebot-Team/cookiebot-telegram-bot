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
| Success output | `bday.next` header, via `i18n.get` — each language has its own translated value (`en` "UPCOMING BIRTHDAYS (all groups):\n\n", `pt`/`es` their own), but the "(all groups)" wording is stale in every language for this port's single-group manual scope — preserved, a cosmetic label, not a behaviour bug — then, for `offset` in `1..4`: `f"{offset} dias:\n"` (literally not through `i18n` at all, `Birthdays.py:110` — Portuguese `"dias"` hardcoded regardless of group language, unlike the header above — a genuinely different, second quirk, both preserved) then one `@username`/`firstName lastName` line per person whose `birth_month`/`birth_day` matches `today + offset`, or `"- \n"` if nobody that day |
| Scope in v1's real function | `next_birthdays` takes a single `chat_id` and sends directly to it — no group iteration of its own; `Birthdays.py:112` sends one message via `send_message(cookiebot, chat_id)`. It is also v1's own 900-second cron follow-up target (`threading.Timer(900, next_birthdays, ...)`, `util_birthday`'s D-BD-2) — this port's manual command and the deferred-follow-up job (once `util_birthday`'s scope is confirmed) both call the same underlying function, matching v1's own reuse |
| Persistence | None — read-only |
| External calls | None beyond the reply itself — no photo, no Bot API call beyond `sendMessage` |

## Verbatim strings, two distinct preserved quirks

- `bday.next` (localised, `cb_core.birthdays.bday_next_header`): each
  language's own translated value, all three already ported byte-identical
  (`cb_core/locale_data/`) — but "(all groups)"/"(todos os grupos)" is stale
  wording for this port's single-group scope, in every language.
- `f"{offset} dias:\n"` — literally not through `i18n` at all
  (`Birthdays.py:110`), Portuguese `"dias"` hardcoded regardless of the
  group's actual language. A different, second quirk from the header above,
  not the same one restated.

## QA

`../Cookiebot-QA/features/util_nextbirthday.feature` — one scenario, matches
v1 exactly, no conflict.

## Status

Scope approved alongside `util_birthday` (manual-only, no cron). Built in
the same slice — see `.specs/features/util_birthday/design.md` (R2, R4) for
the shared query and where `/nextbirthday`'s own logic lives.
