# Contract: fun_complaint (v1 -> v2)

Phase 2/6 of `/migrate-feature` for `/milton`, `/reclamacao`, `/reclamação`,
`/complaint` and `/queja`, plus the reply-triggered hold. QA:
`../Cookiebot-QA/features/fun_complaint.feature`. FEATURE-MAP row:
`fun_complaint`. Files owned by this port:
`packages/cb-core/src/cb_core/asset_data/complaint/*`,
`packages/cb-core/src/cb_core/assets.py`,
`packages/cb-gateway/src/cb_gateway/handlers/complaint.py`,
`packages/cb-gateway/src/cb_gateway/handlers/__init__.py` (one router line),
`packages/cb-gateway/tests/test_complaint.py`,
`packages/cb-core/tests/test_assets.py`,
`qa/features/fun_complaint.feature`, `qa/test_fun_complaint.py`, this file.

## Phase 2 — v1 behaviour contract

v1 handlers: `complaint`, `../COOKIEBOT-Telegram-Group-Bot/Bot/Miscellaneous.py:240-248`
and `complaint_answer`, `:250-259`. Dispatch: `COOKIEBOT.py:215,234-235` (entry
1) and `:300-301` (entry 2).

| Aspect | v1 behaviour (file:line) |
|---|---|
| Triggers — entry 1 | `/milton`, `/reclamacao`, `/reclamação`, `/complaint`, `/queja` — `startswith` prefix match (`COOKIEBOT.py:215,234-235`) |
| Triggers — entry 2 | any non-command message whose `reply_to_message` carries a `caption` **containing** `Milton do RH.` or `Milton from HR.` — substring `in`, not equality (`COOKIEBOT.py:300-301`) |
| Preconditions | both entry points sit under the `functionsFun` gate; off ⇒ `notify_fun_off` replies with locale key `fun_off` (`COOKIEBOT.py:218-219,300`, `Miscellaneous.py:129-131`). No admin check, no membership check, no dedupe: anyone may reply to anyone's Milton photo, any number of times, at any later date. |
| Cooldowns / quotas | none |
| Success output — entry 1 | `sendChatAction upload_photo`; then `milton_pt.jpg` if the group language is exactly `pt`, else `milton_eng.jpg`, sent **as a reply** to the trigger, caption `i18n.get("complaint", lang, user=<sender first_name>)` (`Miscellaneous.py:241-248`) |
| Success output — entry 2 | ① delete the message being replied to — the Milton photo itself (`:251`) ② `sendChatAction upload_audio` ③ `protocol = f"{randint(10,99)}-{randint(100000,999999)}/{now().year}"` (`:253`) ④ send a random `.wav` from `Static/reclamacao/` as a **voice** note, caption `Protocol: {protocol}`, replying to the user's message (`:254-255`) ⑤ `sleep(randint(10, 20))` (`:256`) ⑥ delete the voice note (`:257`) ⑦ send `i18n.get_random_line("answers.txt", lang)` as a reply to the user's message (`:258-259`) |
| Failure output | none. No try/except in either function; `delete_message` swallows its own errors (`universal_funcs.py:340-344`), everything else propagates to the dispatcher's bare `except`. |
| Persistence | **none.** No row, no dict, no cache. The protocol number is generated, shown, and never stored. |
| Side effects | two bot messages deleted per full cycle (the photo prompt, then the voice note); one thread-pool worker blocked for up to 20 s (`:256`) |
| External calls | Telegram Bot API only — `sendChatAction`, `sendPhoto`, `sendVoice`, `deleteMessage`, `sendMessage`. No backend call, no Mongo. |
| Known defects | D-CP-1 … D-CP-5 below |

### Verbatim strings and assets

| Thing | Where |
|---|---|
| `complaint` caption, `%(user)s` interpolated | `Bot/Static/locales/eng/lib.json:134`, `pt/lib.json:145` — **absent from `es`** |
| answer pool | `Bot/Static/locales/{eng,pt,es}/answers.txt`, 22 lines each — ported to `cb_core/locale_data/`, read with `locales.lines("answers", lang)` |
| `fun_off` | `lib.json` eng:119 / pt:131 / es:114 — already ported |
| photos | `Bot/Static/reclamacao/milton_pt.jpg` (34 KB), `milton_eng.jpg` (69 KB) — no `milton_es.jpg` |
| hold music | `Bot/Static/reclamacao/hold{1,2,3,4,5,6,7,9}.wav`, 8 files, ~3.2 MB total — `hold8.wav` does not exist |
| protocol caption | literal `Protocol: ` + the generated number, never localised (`:255`) |

The English and Portuguese `complaint` captions (`cb_core/locale_data/{en,pt}/lib.json`,
key `complaint`) end in the literal substrings `Milton from HR.` and `Milton do
RH.` respectively — those two strings are what entry 2 matches on
(`MILTON_SIGNATURES` in `complaint.py`). They are load-bearing: editing either
locale value breaks the reply chain — a photo sent under the old caption would
no longer re-arm entry 2, and a group's already-sent Milton photos would stop
working retroactively.

### Defects — verdict per item

| id | Defect | Verdict |
|---|---|---|
| D-CP-1 | `es/lib.json` has no `complaint` key, so Spanish groups get the **English** caption through the `es → eng` fallback (`loc.py:158-164`). | **preserve.** `cb_core/locales.py` already falls back the same way; the port inherits it for free. Recorded here, not fixed — HANDOFF already notes v1 locale drift (`pt` missing 4 keys, `es` 8) as a finding, not a bug to fix inside a port. |
| D-CP-2 | No `milton_es.jpg`; the `else` branch serves the English photo to every non-`pt` language. | **preserve** — same reasoning. The language check is `== "pt"`, not a lookup (`_photo_filename` in `complaint.py`). |
| D-CP-3 | Entry 2 matches a caption **substring**, so any photo whose caption happens to contain the signature re-arms the flow, including one sent by a different bot. | **preserve.** It is how the flow works and it is user-visible (people reply to old Milton photos). The `in`-over-both-signatures test is reproduced exactly in `test_complaint.py`. |
| D-CP-4 | `time.sleep(randint(10,20))` blocks a worker thread. | **fix, invisibly.** v2 must not hold the reply path (AGENTS.md §2.4); the observable timing stays 10-20 s. See R3 below. |
| D-CP-5 | The hold pool is missing `hold8.wav`; v1 picks from whatever `os.listdir` returns. | **preserve** — the 8 files that exist were copied byte-identical, not renumbered. |

## Phase 6 — parity table

| Aspect | Verdict |
|---|---|
| Triggers (`/milton`, `/reclamacao`, `/reclamação`, `/complaint`, `/queja`, bare/with argument/with `@botname`) | **identical** — all five resolve to the canonical `complaint` name via `cb_core/textmatch.py:COMMAND_ALIASES`, asserted by a parametrised unit test |
| Entry 2 predicate (`_is_milton_reply`) | **identical** — substring containment over both `MILTON_SIGNATURES` against `reply_to_message.caption` (not `.text`), rejects a caption-less reply, rejects a reply whose own text is itself a command, mirroring `COOKIEBOT.py:186,300-301`'s "reply-capture is a sibling `elif` of the command-dispatch `if`" structure |
| Fun gate (`functionsFun`) and its reply | **identical** on both entry points — `ctx.enabled("fun")`, one `fun_off` reply, nothing else sent; `mark_outcome("refused")` recorded |
| Photo choice | **identical** — `"pt" if ctx.lang == "pt" else "eng"`, an equality check, not a lookup (D-CP-2) |
| Caption interpolation | **identical** — `t(ctx, "complaint", user=<sender first_name>)`, sender's `first_name` used unescaped exactly as Telegram gives it |
| Reply vs send — entry 1 | **identical** — the Milton photo is sent as a reply to the trigger message (`message.reply_photo`) |
| Deleting the replied-to photo | **identical in outcome, changed error handling** — v1's `delete_message` swallows its own errors internally; v2 wraps the same `reply.delete()` call in `contextlib.suppress(Exception)` explicitly, same net effect |
| Protocol format | **identical** — `f"{rng.randint(10, 99)}-{rng.randint(100000, 999999)}/{year}"`, `year` from the current UTC year, caption `f"Protocol: {protocol}"`, never localised |
| Hold voice note | **identical** — sent with `send_voice` (a voice note, not an audio file), replying to the user's message, hold file drawn via `rng.choice(assets.pool("complaint", suffix=".wav"))` over the 8 copied files |
| Hold delay (D-CP-4) | **identical observable timing (10-20 s), changed mechanism** — `rng.randint(10, 20)` fed to `asyncio.create_task`'s tail instead of blocking `time.sleep`; see R3 below |
| Deleting the voice note | **identical in outcome, changed error handling** — same `contextlib.suppress(Exception)` treatment as the photo deletion |
| Answer line | **identical** — `rng.choice(locales.lines("answers", ctx.lang))`, sent as a reply to the user's message, same idiom as `ship.py:130` |
| Failure mid-sequence | **identical** — no try/except around the chat-action/photo/voice sends; an aiogram exception propagates and stops the sequence partway, matching v1's swallowed-exception behaviour at the dispatcher |
| Persistence | **identical** — none. No row is written anywhere in this feature. |
| Random source | **identical distribution** — a plain `random.Random` instance (`_rng`), matching `ship.py`'s/`dice.py`'s existing idiom rather than a seeded default |

## R1.3 — why the assets are package data, not `cb_core.storage`

`cb_core.storage.media()`/`store()` exist for **user-supplied** content: they
dedupe by content hash and record a reference row scoped to a group
(AGENTS.md §5). Milton's two photos and the eight hold-music files are the
opposite — bot-owned fixtures, identical for every group, versioned with the
code rather than uploaded by anyone. Routing them through `storage` would mean
a network round trip (object storage) or a database row (dedupe bookkeeping)
to fetch a file that never changes and ships in the wheel already. Instead
they live in `packages/cb-core/src/cb_core/asset_data/complaint/`, shipped as
package data (the same `pyproject.toml` declaration `locale_data` already
uses, extended rather than duplicated — R1.4), and reached at send time
through `cb_core/assets.py`'s `path()`/`pool()` — the one accessor `fun_death`
and `fun_meme` are expected to reuse rather than growing a second mechanism.
Next porter: if what you're shipping is bot-owned and static, this is the
module; if it's something a user sent, it's `cb_core.storage`.

## R3.3 — the in-process-tail restart caveat

Entry 2's tail (delete the voice note, send the answer line, 10-20 s later) is
scheduled with `asyncio.create_task` and held in a module-level
`_pending_tails` set so it isn't garbage-collected mid-sleep — the exact idiom
`groupguardian.py:501-517` uses for the captcha's 30 s unban, copied rather
than reinvented (design R3.2).

This is still in-process state: **if the gateway restarts while a hold is in
flight, the scheduled deletion and answer are lost.** There is no persistence
of "a complaint reply is pending" anywhere (spec: Persistence — none), so
there is nothing to resume from on the next boot. This is the same gap
`groupguardian`'s captcha unban has, tracked as `HANDOFF.md` §1 gap 5
("No gateway -> worker enqueue wiring"). `util_everyone` is expected to build
that wiring; when it lands, this tail becomes a candidate to move off
in-process `asyncio.create_task` and onto a real enqueued job — but that move
is explicitly **not** part of this port (design open decision 3).

## Design R5.2/R5.3 — asset parity test, no integration test

`packages/cb-core/tests/test_assets.py` asserts the copied
`asset_data/complaint/` directory is byte-identical to
`../COOKIEBOT-Telegram-Group-Bot/Bot/Static/reclamacao/` when that checkout is
present, and skips cleanly otherwise (the reference repos are not available in
CI) — the same skip idiom the `locale_data` diff test already uses, reused
rather than duplicated.

The feature writes no row (spec: Persistence — none), so there is no
`qa/integration/test_*` for it: an empty integration test would only exercise
a DB lookup already covered elsewhere (the `fun` gate read through
`context_for`), not anything specific to this feature. Coverage instead comes
from the unit tests (pure `_is_milton_reply`/`_build_protocol`/
`_photo_filename` logic, alias resolution, `assets.pool` shape) and the
acceptance tests (the full two-entry sequence, the fun-off gate, and the
non-Milton-caption near miss, against the mock Telegram API).

## Tests

| Layer | File |
|---|---|
| Unit — alias resolution, `_is_milton_reply`, `_build_protocol`, `_photo_filename`, `assets.pool` shape | `packages/cb-gateway/tests/test_complaint.py`, `packages/cb-core/tests/test_assets.py` |
| Integration — none; see R5.2/R5.3 above | n/a |
| Acceptance — the two copied QA scenarios plus the fun-off gate and the non-Milton-caption near miss | `qa/features/fun_complaint.feature`, `qa/test_fun_complaint.py` |
