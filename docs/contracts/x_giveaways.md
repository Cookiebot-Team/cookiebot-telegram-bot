# Contract: x_giveaways (v1 -> v2)

Phase 2/6 of `/migrate-feature` for `/giveaway`. **No QA scenario exists** —
`../Cookiebot-QA/features/` has no giveaway file at all, so
`qa/features/x_giveaways.feature` is authored as part of this port
(AGENTS.md §5). FEATURE-MAP row: `x_giveaways`. Spec:
`.specs/features/x_giveaways/spec.md`.

Files owned by this port:
`packages/cb-api/migrations/versions/0006_giveaways.py` (new),
`packages/cb-core/src/cb_core/giveaways.py` (new),
`packages/cb-core/src/cb_core/pending_giveaways.py` (new),
`packages/cb-core/src/cb_core/textmatch.py` (one alias),
`packages/cb-gateway/src/cb_gateway/handlers/giveaway.py` (new),
`packages/cb-gateway/src/cb_gateway/handlers/__init__.py` (registration),
`qa/conftest.py` (`clean_giveaways`), and the tests listed at the bottom.

## Phase 1 — where v1 lives

- Feature: `../COOKIEBOT-Telegram-Group-Bot/Bot/Giveaways.py:25-173` —
  `giveaways_ask`, `giveaways_create`, `giveaways_enter`, `giveaways_end`,
  `giveaways_delete`.
- Dispatch: `COOKIEBOT.py:249,262-263` (the `functionsUtility` `elif` chain,
  the same one `/youtube` and `/dado` sit in) and `COOKIEBOT.py:415-428` for
  the four callbacks.
- Storage: `Giveaways.db`, a second SQLite file next to `Publisher.db`, opened
  per thread via `threading.local()` and serialised by a module `RLock`
  (`:14-23`).
- Locale strings: the nested `giveaway` object in
  `cb_core/locale_data/{en,pt,es}/lib.json` — already ported byte-identical.
  **`es` is missing ten of its sixteen entries** (v1's own drift, reported by
  `locales.missing_keys()`); see "Catalog" below.

## Phase 2 — v1 behaviour contract

| Aspect | v1 behaviour (file:line) |
|---|---|
| Trigger | `/giveaway` — one spelling, no PT/ES alias anywhere (`COOKIEBOT.py:249`) |
| Preconditions | `functionsUtility`; then an admin/owner check (`:27`) |
| Not an admin | `giveaway.permission` (`:28-30`) |
| No prize | `len(msg['text'].split()) == 1` ⇒ `giveaway.raffled` (`:31-34`) |
| Prompt | `giveaway.create` plus five one-button rows, `callback_data = f'GIVEAWAY {n} {prize}'` where `prize = json.dumps(prize_text)[:20]` (`:35-46`) |
| Count press | admin gate (`COOKIEBOT.py:416-418`), delete the prompt, `giveaways_create` (`:420-421`) |
| Create | reject `n` outside 1..5 silently (`:49-50`); announce `giveaway.time` with prize/count/`datetime.now().strftime(giveaway.strftime)`; two buttons from `giveaway.buttons`; INSERT; `pinChatMessage` in a bare `try/except` (`:48-72`) |
| Enter | participant label is `"@" + username` or `first_name` (`:77`); membership tested by substring against a comma-joined string; `giveaway.in` / `giveaway.enter` / `giveaway.not_found`; whole body wrapped in `except` ⇒ `giveaway.error` (`:74-99`) |
| End (authorisation) | admin **or** the row's `creator_id` **or** `ownerID`, else `giveaway.end_adm` (`:114-117`) |
| End (no entrants) | `giveaway.no_one` to the group, DELETE the row, answer `giveaway.end`, delete the message (`:118-126`) |
| End (draw) | `random.sample(participants, min(n_winners, len(participants)))`; per winner `giveaway.winnner.one` when `n_winners == 1` else `.more`, with `idx`/`winner`/`prize`; profile photo when the scrape produced one, plain message otherwise (`:127-148`) |
| End (after the draw) | `giveaway.draw_more` with ✅ = `GIVEAWAY end` and ❌ = `GIVEAWAY delete`; `UPDATE giveaways SET message_id` to the new message; answer `giveaway.selected`; delete the old message (`:149-160`). **Entrants are not cleared**, so a re-draw can pick a previous winner. |
| Delete | DELETE the row, answer `giveaway.end`, delete the message; no check of its own (`:165-173`) |
| Persistence | `Giveaways.db`, one host, `participants TEXT` |
| External calls | `get_profile_image` — an HTML scrape of `https://telegram.me/{username}` (`SocialContent.py:279-292`) |
| Known defects | D-GA-1 … D-GA-5 below |

## Defects — verdict per item

| id | Defect | Verdict |
|---|---|---|
| D-GA-1 | **`/giveaway` never completed.** The prize went out as `json.dumps(text)[:20]` inside the callback data, the dispatcher stripped every `"` back out of it (`COOKIEBOT.py:421`), and `giveaways_create` then called `json.loads` on the result (`:54`) — which raises for anything that is not a bare JSON literal, i.e. every real prize. The exception reached v1's top-level `except` and the user saw nothing. | **fix** — the prize is held in `cb_core.pending_giveaways` (Valkey, `giveaway:pending:<uuid7>`) and the callback carries only the token. No JSON round trip, no 20-character truncation, no 64-byte `callback_data` ceiling on the prize. Porting the defect faithfully would mean porting a feature that cannot run. |
| D-GA-2 | **The "Put me in!" button was admin-only.** The dispatcher's `GIVEAWAY` gate (`COOKIEBOT.py:416-418`) covers *every* action including `enter`, so no ordinary member could join. | **fix** — `enter` is open to everyone; the gate stays on `create` and `delete`. v1's own code shows the intent: `giveaways_end` re-checks admin/creator/owner itself (`:114-117`) and answers `giveaway.end_adm`, which would be unreachable if the outer gate were meant to cover it. |
| D-GA-3 | **Lost update on entry.** `giveaways_enter` reads the joined participant string, edits it in Python and writes the whole column back (`:81-94`). Two presses in the same instant lose one. | **fix** — one `INSERT ... ON CONFLICT DO NOTHING` into `giveaway_participants`; `qa/integration/test_giveaways.py` fires ten concurrent entries and asserts all ten land. |
| D-GA-4 | **Identity is the display name.** Two members whose first name is "Alex" and who have no username are one entrant, and a rename changes who is in the raffle. | **fix** — the primary key is `(group_id, giveaway_id, user_id)`; the display name is stored alongside because it is what the announcement prints. |
| D-GA-5 | **The lookup had no chat predicate.** `WHERE message_id = ?` (`:81,107,169`) is global across every group in the file, so two groups whose messages shared an id answered each other's presses. | **fix** — `group_id` is the distribution column and every statement carries it; the unique index is `(group_id, message_id)`. |
| (shared) | `get_profile_image`'s `telegram.me` HTML scrape into a fixed `temp.jpg` (`:144-146`) — the same cross-request race `fun_battle`'s port already rejected (D-BT-1/D-BT-2). | **fix** — this handler holds the entrant's real `user_id`, so it calls `get_user_profile_photos` and forwards the `file_id`. No download, no re-encode, no temp file. v1's "no photo ⇒ plain message" fallback is preserved. |

## Preserved deliberately

- **The callback vocabulary**: `GIVEAWAY <n>`, `GIVEAWAY enter`,
  `GIVEAWAY end`, `GIVEAWAY delete` — v1's own wire format, with the trailing
  field now a token rather than a mangled prize.
- **`giveaway.winnner`** — v1's typo, in all three locale files. The catalog
  is a byte-for-byte port, so the typo is the key.
- **Singular keyed off the configured count**, not the drawn one (`:131`): a
  3-winner raffle with a single entrant still reads "our 1st winner is…".
- **Entrants survive a draw**, so ✅ re-draws from the same pool and can pick
  a previous winner (`:149-157`).
- **`pinChatMessage` failures are swallowed** (`:69-72`): a bot without pin
  rights still runs a giveaway.
- **`NOT_ADMIN_TEXT`** is v1's hardcoded English `"Only admins can do this"`
  (`COOKIEBOT.py:417`) — not a catalog key in v1, so not translated here.
- **The unknown-action branch** answers v1's literal
  `"ERROR! please contact @MekhyW"` (`COOKIEBOT.py:428`).

## Catalog

`giveaway` is a nested object, so `locales.get` (flat keys only) does not
apply. `handlers/giveaway.py:gtext` does the fallback by hand — **per
sub-key**, unlike `groupguardian.py`'s `_captcha_strings`, which falls back
per object. That difference is load-bearing: `es`'s `giveaway` object exists
but is missing `not_found`, `in`, `enter`, `error`, `no_one`, `end_adm`,
`end`, `selected`, `end_error` and `buttons`, so an object-level fallback
would answer a Spanish group with a key name. Per-key fallback answers in
English, which is what `locales.get` already does for every flat key.

## Phase 6 — parity

| Behaviour | v1 | v2 | Same? |
|---|---|---|---|
| Trigger and gate | `/giveaway`, `functionsUtility` | same | ✅ |
| Non-admin caller | `giveaway.permission` | same | ✅ |
| Empty prize | `giveaway.raffled` | same | ✅ |
| Count keyboard | five rows, 1..5 | same | ✅ |
| Announcement | `giveaway.time`, prize truncated to 20 chars — and never actually reached | full prize, announcement reached | ⚠️ D-GA-1 |
| Pin | best-effort | same | ✅ |
| Enter | admin-only, name-keyed, racy | open, id-keyed, atomic | ⚠️ D-GA-2/3/4 |
| Already entered | `giveaway.in` | same | ✅ |
| End authorisation | admin / creator / owner | same | ✅ |
| No entrants | `giveaway.no_one`, row deleted, message deleted | same | ✅ |
| Winner announcement | photo from a web scrape, else plain text | photo from the Bot API, else plain text | ⚠️ mechanism, same output |
| Draw more | `giveaway.draw_more`, row re-pointed, entrants kept | same | ✅ |
| Delete | row and message removed | same | ✅ |
| Storage | local SQLite, one host | Citus, distributed on `group_id` | ⚠️ by design |

## Tests

| Layer | File |
|---|---|
| Unit | `packages/cb-gateway/tests/test_giveaway.py` — trigger, callback grammar, keyboards, the draw, and the `es` per-key fallback |
| Integration | `qa/integration/test_giveaways.py` — real Citus: chat-scoped lookup, duplicate entry, same-name entrants, ten concurrent entries, cascade, re-point, and `Task Count: 1` on both reads |
| Acceptance | `qa/features/x_giveaways.feature` + `qa/test_x_giveaways.py` — ten scenarios, authored |
