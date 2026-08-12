# fun_partneredcons — Specify

**Feature id:** `fun_partneredcons` · **Milestone:** M2 · **Kind:** mixed — port
(5 triggers) + net-new (1 trigger)
**v1 source:** `Bot/Miscellaneous.py:261-323` (`event_countdown`), dispatched
`Bot/COOKIEBOT.py:248-251`.

## Status: blocked on assets, evidence below — not building around it yet

Every trigger in this feature sends a picture. I checked all three reference
repos (`../COOKIEBOT-Telegram-Group-Bot`, `../Cookiebot-QA`,
`../COOKIEBOT-backend`) for a local copy of any of them: there is none.
**All five ported triggers** need the same private GCS bucket `fun_death`
and `fun_battle` are already blocked on (`Countdown/` prefixes this time,
detailed below) — per instruction, that joins the existing export
prerequisite rather than becoming a new one. **`/trex`, the net-new
trigger, has no *code* behind it anywhere** — confirmed: `/trex` does not
exist in the v1 source, not even as dead code, nor in QA's repo or the
backend repo.

**Corrected: it does have an image source.** This section originally
concluded "no image source at all", reasoning from the v1 source alone —
`Miscellaneous.py:18-22` lists five countdown folders and `Trex` is not one
of them. Listing the real bucket disproved it: `gs://cookiebot-bucket/
Countdown/Trex` holds **67 images**, sitting there unread by any v1 code
path. They were found by diffing a full bucket listing against
`bucket_export.PREFIXES`, and the prefix has since been added to that tuple
and exported. So `/trex` is not "invent a pool or drop the trigger" any
more; it is an ordinary port with assets, and the only thing genuinely
net-new about it is that v1 never wired the command up.

## Goal

`/bff`, `/patas`, `/fursmeet`, `/furcamp`, `/pawstral` each post a themed
picture with a countdown caption for one specific real-world furry
convention v1's operators have a promotional partnership with. QA also
specifies `/trex`, which has no v1 *behaviour* behind it — but does have 67
images in the bucket that no v1 code path ever read (see the correction
above, and "Triggers: port vs. net-new" below).

## Triggers: port vs. net-new

| Trigger | v1? | Evidence |
|---|---|---|
| `/patas`, `/bff`, `/fursmeet`, `/furcamp`, `/pawstral` | **port** | `event_countdown`, `Miscellaneous.py:264-318`, dispatched `COOKIEBOT.py:250` |
| `/trex` | **net-new** | Zero matches for `trex` (case-insensitive) anywhere in `../COOKIEBOT-Telegram-Group-Bot`. QA is the only source of truth for it — `../Cookiebot-QA/features/fun_partneredcons.feature`'s `/trex` scenario is what this trigger must satisfy, per `/implement-feature`, not `/migrate-feature`. |

One plausible (unverifiable) explanation for why QA specifies a trigger v1's
source never had: v1 has a **second**, fully dynamic photo-command
mechanism — `custom_command` (`Miscellaneous.py:145-158`) — that serves
*any* command name matching a live `Custom/<name>` bucket folder,
dispatched whenever `msg['text']`'s command word is found in
`custom_commands` (`Miscellaneous.py:23`, itself built by listing the
bucket's `Custom/` prefix at import time — `COOKIEBOT.py:281`). A `/trex`
folder could have existed live in the bucket at some point without ever
appearing in source control. This does not change anything about how to
port it (there is still no image to use, and QA's scenario, not v1's
runtime bucket state, is the source of truth for a net-new trigger either
way), but it is worth recording as the likely origin, and as evidence that
this whole feature area has always been operated partly outside source
control.

## Phase 2 — v1 behaviour contract (the five ported triggers)

| Aspect | v1 behaviour (file:line) |
|---|---|
| Triggers | `/patas`, `/bff`, `/fursmeet`, `/furcamp`, `/pawstral` — `startswith`, case-insensitive on the event check itself (`msg['text'].lower().startswith(...)`, `:264` etc.) though the outer dispatch match is case-sensitive (`COOKIEBOT.py:248,250`) |
| Preconditions | **Ungated.** This `elif` (`COOKIEBOT.py:248-251`) checks `msg['text'].startswith((...))` for these five names *before* the `elif not utilityfunctions: notify_utility_off(...)` branch two lines down (`:253`) — the five event triggers are dispatched to `event_countdown` unconditionally, never reaching the `utilityfunctions` check that gates their siblings in the same `elif` chain (`/dado`, `/ideiadesenho`, `/youtube`, `/giveaway`). Not gated on `functionsFun` either — that gate belongs to a different, earlier `elif` block entirely. Confirmed by reading the dispatch code directly, not inferred. |
| Cooldowns / quotas | None — grepped `Cooldowns.py` in full, no entry for any of the five names or `event_countdown`. |
| Success output | ① react `🔥` (`:262`) ② `sendChatAction upload_photo` (`:263`) ③ one signed-URL photo from the event's own `Countdown/*` bucket prefix, random pick (`:267` etc.) ④ a caption **hardcoded per event in Python**, not templated through the locale catalog beyond one `cta` line (see "The caption is not what the locale file implies" below) ⑤ sent as a **reply** to the trigger (`:323`, `msg_to_reply=msg`) |
| Countdown math | `daysremaining = (target_date - now).days + 1` against a **hardcoded `(day, month, year)` per event** (`:265,276,287,298,309`). `-5 <= daysremaining <= 0` ⇒ the caption becomes a **literal YouTube link** (`:270` etc.) — a placeholder for "the event is happening right now," the same link for every event. Otherwise, `while daysremaining < -5: daysremaining += 365` (`:272` etc.) — see "The `+365` wraparound" below. |
| Failure output | `event.error` ("Event not found!") if none of the five prefixes match (`:319-321`) — **dead code**: `event_countdown` is only ever called from the one dispatch site that already matched one of the five names, so this branch is unreachable in practice. |
| Persistence | None |
| External calls | GCS signed-URL read (`Countdown/{Patas,BFF,FurSMeet,Furcamp,Pawstral}` prefixes) |

### The `+365` wraparound — a real, preserved quirk

Each event's `(day, month, year)` is a single hardcoded date — the *next*
occurrence as of whenever that line was last edited, not a recurring rule.
Once that date passes, `while daysremaining < -5: daysremaining += 365`
approximates "next year, same calendar day" by repeatedly adding 365 (never
366 — leap years are never accounted for) until the count is no longer more
than 5 days in the past. The **caption text itself still shows the
original hardcoded day/month** (e.g. patas's caption always reads
`"{day} a {day+3}/{month}"` using the literal `11`/`12` from the hardcoded
tuple), so this only works as a countdown to "the same calendar date, some
number of 365-day hops from now" — it does not know or show a real updated
year, and drifts a day every four years. As of today (2026-08-02), three of
the five hardcoded dates have already passed (`bff`: 17/7/2026, `fursmeet`:
21/11/2025, `pawstral`: 29/8/2025) and are already running on the
wraparound approximation; `patas` (11/12/2026) and `furcamp` (5/2/2027)
have not yet. **Preserve verbatim** — this is a
content-maintenance quirk in v1's hardcoded dates, not a code defect;
"fixing" it would mean guessing real updated convention dates, which is
not this port's call to make.

### The caption is not what the locale file implies

`cb_core/locale_data/{en,pt,es}/lib.json`'s `event` key (already ported
byte-identical) carries `name`, `cta` (list) and `caption` (a template with
`%(headline)s`/`%(cta_line)s`/`%(when_line)s`/`%(where_line)s`/`%(links_line)s`
placeholders) per event, plus a flat `error` string. **`event_countdown`
only ever reads `event.<name>.cta`** (`i18n.get("event.patas.cta", ...)`,
`:266` etc.) and `event.error` (`:320`) — `name` and `caption` are inert,
never read by any code path found in the v1 checkout. The actual caption is
built with a hardcoded Python f-string per event (`:274,285,296,307,318`),
embedding a venue name, dates, ticket links and Discord/Telegram group
handles directly in source, in **Portuguese regardless of the group's
language** for `patas`/`bff`/`fursmeet`/`furcamp`, and in **English
regardless of the group's language** for `pawstral` — none of them consult
`language` for anything beyond picking which `cta` list to draw a random
line from. This is a real, user-visible quirk (a Spanish-speaking group
still gets a Portuguese countdown caption for four of the five events), and
it is what v1 **actually does**, not what the unused `caption`/`when_line`/
`where_line` template fields suggest a finished version might have done.
Per AGENTS.md's tie-break rule, v1's executing code wins over inert data —
**preserve this exactly**, including the language mismatch, when this port
is eventually built.

## QA (`/trex`, net-new — `/implement-feature` territory)

```gherkin
Scenario: User types /trex in in any group
    Given that the user is a member of the group
    When the user types the command "/trex"
    Then the bot should send a picture of the "Trex Furplayer" event to the group
```

No v1 behaviour exists to be compatible with — this scenario **is** the
spec. What it leaves open, to be answered in `design.md` under "Open
decisions — answered" once this feature is unblocked, not guessed here:

- What "the Trex Furplayer event" picture actually is — there is no asset
  anywhere in any of the three reference repos.
- Whether `/trex` gets a countdown-to-a-date treatment like the five ported
  triggers (implying someone supplies a real event date) or a simpler
  "always send this picture" treatment (implying it is not a dated
  countdown at all, just a themed poster).
- Whether it shares `event_countdown`'s ungated-dispatch placement or is
  gated on `functionsFun`/`functionsUtility` like a normal fun/utility
  command — v1 has no precedent to inherit for a command it never had.

QA's feature file also has a duplicated `fursmeet` scenario (appears twice,
byte-identical) — a QA-authoring slip, not a behavioural conflict; noted
here for completeness, not acted on.

## What's still blocked — the five ported triggers

`bloblist_patas`, `bloblist_bff`, `bloblist_fursmeet`, `bloblist_furcamp`,
`bloblist_pawstral` (`Miscellaneous.py:18-22`) are
`storage_bucket.list_blobs(prefix="Countdown/{Patas,BFF,FurSMeet,Furcamp,Pawstral}")`
— the same private `cookiebot-bucket` `fun_death`'s `bloblist_death`
(`Death/` prefix) and `fun_battle`'s `bloblist_fighters_*`
(`Fight/English`/`Fight/Portuguese` prefixes) already read from, this time
the `Countdown/` prefix family. Checked all three reference repos
(`../COOKIEBOT-Telegram-Group-Bot/Bot/Static/`, `../Cookiebot-QA/`,
`../COOKIEBOT-backend/`) for any of the five event names, `countdown`, or a
loose image file that could plausibly be one of these posters: nothing.
Same conclusion as `fun_death`'s and `fun_battle`'s contracts: no local
copy, no credential to the live bucket anywhere in this environment.

**Per instruction, this joins the existing bucket-export prerequisite
(`Death/`, `Fight/English`, `Fight/Portuguese`) rather than becoming a
fourth separate gap** — one export, five more prefixes:
`Countdown/{Patas,BFF,FurSMeet,Furcamp,Pawstral}`.

## What's still blocked — `/trex`

Not a bucket problem — a "there is no image at all, anywhere, for this
specific net-new request" problem. Even once the `Countdown/` prefixes are
exported, there is nothing to export for `/trex`, because it was never
sourced from that bucket (or any bucket this session can find evidence of)
in the first place. Building this trigger needs either a real "Trex
Furplayer" event asset to be supplied, or an explicit decision that it
should point at something else — not something this port should invent a
placeholder for, per the same standard `fun_death`'s spec already applied
("do not invent placeholder media").

## No `design.md`/`tasks.md` yet

Per instruction — reporting the asset situation before building around it.
Everything else needed to execute once assets exist is already captured
above: the exact hardcoded dates/captions/CTAs to preserve, the ungated
dispatch, the wraparound quirk, the language-mismatch quirk, and the three
open `/trex` design questions.
