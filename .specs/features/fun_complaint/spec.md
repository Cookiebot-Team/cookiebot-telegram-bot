# fun_complaint — Specify

**Feature id:** `fun_complaint` · **Milestone:** M2 · **Kind:** v1 port
**v1 source:** `Bot/Miscellaneous.py:240-248` (`complaint`) and `:250-259`
(`complaint_answer`), dispatched `Bot/COOKIEBOT.py:215,234-235` and `:300-301`

## Goal

The HR-complaint bit. `/reclamacao` sends a photo of Milton from HR inviting the
user to file a complaint; replying to that photo puts the user on hold — a
random hold-music voice note stamped with a protocol number — and, 10 to 20
seconds later, answers with a random canned line. Two entry points, one stateless
reply chain, no database.

## Scope

In: both entry points, the static assets (2 photos, 8 hold-music files), the
delayed tail, tests at all three layers, contract, status flip.
Out: any new table (v1 stores nothing and neither should v2), localisation
repairs for `es` (see D-CP-1/D-CP-2), topic/thread threading (v1 does not do it
for this feature).

## Behaviour contract (Phase 2)

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
| answer pool | `Bot/Static/locales/{eng,pt,es}/answers.txt`, 22 lines each — already ported to `cb_core/locale_data/`, read with `locales.lines("answers", lang)` |
| `fun_off` | `lib.json` eng:119 / pt:131 / es:114 — already ported |
| photos | `Bot/Static/reclamacao/milton_pt.jpg` (34 KB), `milton_eng.jpg` (69 KB) — no `milton_es.jpg` |
| hold music | `Bot/Static/reclamacao/hold{1,2,3,4,5,6,7,9}.wav`, 8 files, ~3.2 MB total — `hold8.wav` does not exist |
| protocol caption | literal `Protocol: ` + the generated number, never localised (`:255`) |

The English and Portuguese `complaint` captions end in the literal substrings
`Milton from HR.` and `Milton do RH.` respectively — those two strings are what
entry 2 matches on. They are load-bearing: changing the caption breaks the reply
chain.

### Defects — verdict per item

| id | Defect | Verdict |
|---|---|---|
| D-CP-1 | `es/lib.json` has no `complaint` key, so Spanish groups get the **English** caption through the `es → eng` fallback (`loc.py:158-164`). | **preserve.** v2's `cb_core/locales.py` already falls back the same way; the port inherits it for free. Record it, do not invent a Spanish string — HANDOFF already notes v1 locale drift (`pt` missing 4 keys, `es` 8) as a finding, not a bug to fix inside a port. |
| D-CP-2 | No `milton_es.jpg`; the `else` branch serves the English photo to every non-`pt` language. | **preserve** — same reasoning. The language check is `== "pt"`, not a lookup. |
| D-CP-3 | Entry 2 matches a caption **substring**, so any photo whose caption happens to contain the signature re-arms the flow, including one sent by a different bot. | **preserve.** It is how the flow works and it is user-visible (people reply to old Milton photos). Reproduce the `in`-over-both-signatures test exactly. |
| D-CP-4 | `time.sleep(randint(10,20))` blocks a worker thread. | **fix, invisibly.** v2 must not hold the reply path (AGENTS.md §2.4); the observable timing stays 10–20 s. See design R3. |
| D-CP-5 | The hold pool is missing `hold8.wav`; v1 picks from whatever `os.listdir` returns. | **preserve** — copy the 8 files that exist, byte-identical. Do not renumber. |

## QA scenario

`Cookiebot-QA/features/fun_complaint.feature` exists with two scenarios (typed
here as-is, trailing whitespace included):

```gherkin
Feature: sends a fun complaint message and picture to the group when the user types a specific command

    Background:
        Given that the bot is in the group and properly set up

    Scenario: User types the complaint command
        Given that the user is a member of the group
        When the user types the command "/complaint"
        Then the bot should send a fun complaint message to the group
        And the bot should send a fun complaint picture to the group 
        And prompt the user to answer the message with a complaint of their own

    Scenario: User responds to the complaint message
        Given that the user has received the fun complaint message
        When the user responds to the message with their own complaint
        Then the bot should send a voice message with a on-hold music to the group
        And then after some minutes answer with a random phrase.
```

**QA/v1 conflicts to record in `feature-map.mdx`:**

1. QA says "message *and* picture"; v1 sends **one** message — a photo whose
   caption is the message. Implement v1's single send.
2. QA says "after some minutes"; v1 waits 10–20 **seconds**. Implement v1's.
3. QA omits both deletions entirely. They are v1 behaviour and are in scope.

## Success criteria

1. All five command triggers fire entry 1; a reply to a caption containing
   either signature fires entry 2; a reply to anything else does not.
2. Photo selection is `pt` ⇒ `milton_pt.jpg`, everything else ⇒ `milton_eng.jpg`.
3. Protocol string matches `^\d{2}-\d{6}/\d{4}$` and the caption is
   `Protocol: <that>`.
4. The handler returns without waiting out the hold; the deletion and the answer
   still land 10–20 s later.
5. Unit, integration-free, and acceptance tests green; contract written.
