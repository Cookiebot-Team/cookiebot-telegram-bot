# fun_battle — Specify

**Feature id:** `fun_battle` · **Milestone:** M2 · **Kind:** v1 port
**v1 source:** `Bot/SocialContent.py:294-379` (`battle`), dispatched
`Bot/COOKIEBOT.py:216,218-219,224-225`.

## Status: decisions made, in progress

`/battle` was scoped as "pure Telegram, no new infrastructure, no assets."
Having read the full handler and every helper it touches, that undersold
the scope — two of its three invocation shapes need the exact same class of
blocker `fun_death` just hit (a private GCS bucket, never checked into the
v1 repo), and the third shape v1 built on a genuinely fragile mechanism —
unauthenticated HTML scraping of `telegram.me` profile pages. Both open
questions this raised are now decided (see "Decisions" below); `design.md`
and `tasks.md` implement them.

## Decisions

1. **The redesign is accepted.** Resolve a target through
   `cb_core.members.roster(group_id)` to a real `user_id`, fetch their
   photo through `bot.get_user_profile_photos` (the same Bot API call v1's
   own no-tag path already used), hand the returned `file_id` straight to
   Telegram. No download, no OpenCV, no temp files — D-BT-1 (the temp-file
   race) and D-BT-2 (the scrape) both disappear with the code that caused
   them.

   **Accepted behavioural drift, recorded here and in the contract**: a
   tagged user who has never spoken in this group cannot be resolved this
   way, where v1's scrape sometimes could reach such a user via their
   public `telegram.me` page. This is not a new failure mode — an
   unresolvable tag falls into v1's **existing** `battle_extract` message,
   the same string a scrape failure already produced, naming the same
   tagged token. A user only notices *that* the battle failed, never a
   difference in *how* it failed.

2. **Path A (two people, explicit tags or `"random"`) ships now.** It needs
   no GCS bucket at all. Paths B (one tag) and C (no tag) both need a
   "fighter" opponent image from the same `Fight/` GCS prefix `fun_death`'s
   `Death/` prefix is blocked on — reachable from `cookiebot-bucket`, never
   checked into the v1 repo, no credential anywhere in this environment.
   Rather than inventing a new "not implemented yet" string, B/C reuse
   `battle_no_picture` verbatim — an already-ported, literally true
   description ("you need to have a profile picture ... or it's private")
   repurposed for "there is no fighter image to use." This is a **temporary
   route into an existing v1 failure branch**, not the final behaviour for
   B/C — `design.md` and the contract both say so explicitly, and it is
   replaced once the `Fight/` export lands, joining `fun_death` under the
   same bucket-export gap in `HANDOFF.md` rather than getting one of its
   own.

## Goal

`/battle` (aliased `/batalha`, `/batalla`) posts two side-by-side photos —
either two people, or one person against a randomly-picked "fighter"
character — with a caption naming a random fight type/rule/equipment
combination, then a native Telegram poll so the group can vote on the
winner.

## The three invocation shapes v1 actually has

`get_members_tagged(msg)` (`SocialContent.py:104-111`) collects every
`@token` after an `@` in the message text, minus anything ending `bot`.
That count, plus the literal substring `"random"` anywhere in the message,
selects one of three completely different code paths:

### A — two people (two `@tags`, or the word "random")

`len(members_tagged) > 1` (two or more explicit tags — only the first two
are ever used) **or** `'random' in msg['text'].lower()`. Once this
combined condition is true, v1 checks `'random' in text` **again**
(`:298-299`) and lets it win: a message with both two `@tags` and the word
`"random"` anywhere takes the random-pick path, not the explicit tags.
Only when `"random"` is absent do the two tags get used. The random pick
draws from `get_members_chat`, v1's registry read, retried up to 100 times
for two rows that both carry a `'user'` key. For each of the two
resulting usernames, v1 does an **unauthenticated HTTP GET of
`https://telegram.me/{username}`**, parses the returned HTML with
BeautifulSoup, and takes the `src` of the first `<img>` tag it finds —
Telegram's public web-preview page for that username, present only when the
account has a public username *and* has not disabled "who can see my
profile photo." If either page returns zero `<img>` tags, v1 replies
`battle_extract` (`"I couldn't extract the photo of %(user)s..."`) naming
that specific user and returns — no poll, no partial result.

Both fetched images are re-encoded through OpenCV
(`cv2.imdecode` → `cv2.imwrite`) to two **hardcoded, non-namespaced local
filenames**, `user1.jpg`/`user2.jpg` (`:325-326`) — see the concurrency
defect below — then reopened and sent as an `InputMediaGroup`, captioned
`"{a} VS {b}"` plus the flavour-text suffix (`:328-333`), followed by
`sendPoll(poll_title, [a, b], is_anonymous=False, allows_multiple_answers=False)`
(`:342-343`). No GCS bucket involved in this path at all.

### B — exactly one tagged user

`len(members_tagged) == 1`. Same `telegram.me` scrape for that one user
(`:346-357`) — empty result ⇒ `battle_private` (a *different* string than
path A's `battle_extract`, same meaning) and return. The fetched photo is
cv2-round-tripped to a third hardcoded filename, `user.jpg` (`:356-357`),
then falls through into the shared tail below.

### C — no tag, no "random"

Falls back to the caller's own Telegram profile photo via the real Bot API
— `cookiebot.getUserProfilePhotos(msg['from']['id'], limit=1)['photos'][0][-1]['file_id']`
(`:360-361`, the *only* branch that uses the actual Telegram API rather than
scraping) — `IndexError` (no photos, or a private profile) ⇒
`battle_no_picture` and return.

### Shared tail for B and C — the GCS blocker

Both fall-through paths need a "fighter" opponent: a random image from
`bloblist_fighters_eng`/`bloblist_fighters_pt`
(`SocialContent.py:24-25` — `storage_bucket.list_blobs(prefix="Fight/English")` /
`prefix="Fight/Portuguese")`) — the exact same private `cookiebot-bucket`
GCS bucket `fun_death`'s `bloblist_death` reads from, just a different
prefix (`Fight/` instead of `Death/`), read the same way (15-minute signed
URL, `:372`). PT groups pick from *either* pool (`random.choice(random.choice([eng, pt]))`,
`:367`); every other language uses the English pool only (`:370`). The
fighter's display name is derived from its blob filename
(`fighter.name.split('/')[-1]` with extensions stripped and underscores
turned to spaces, title-cased, `:373`) — there is no locale string for
fighter names, they come from however the file was named in the bucket.

A coin flip (`:375-376`) decides display order (user-first or
fighter-first) in both the media-group caption and the poll's choice list.
Same `sendMediaGroup` + `sendPoll` tail as path A (`:377-379`).

I checked whether `fun_death`'s finding (bucket contents never checked into
`../COOKIEBOT-Telegram-Group-Bot`) also holds here: it does.
`find ../COOKIEBOT-Telegram-Group-Bot -iname '*fight*' -not -path '*/locales/*'`
returns nothing, and there is no `battle`/`fighter`-named asset anywhere in
the checkout. **Paths B and C are blocked on the same prerequisite as
`fun_death`** — someone exporting `Fight/English` and `Fight/Portuguese`
from `cookiebot-bucket`. Path A needs no bucket at all.

## Behaviour contract (Phase 2)

| Aspect | v1 behaviour (file:line) |
|---|---|
| Triggers | `/battle`, `/batalha`, `/batalla` — shared fun `elif` chain (`COOKIEBOT.py:216,224-225`) |
| Preconditions | `functionsFun` gate, same block `fun_death`/`fun_ship`/`fun_complaint` share (`COOKIEBOT.py:218-219`) — off ⇒ `fun_off`, sent as a reply |
| Cooldowns / quotas | None — grepped `Cooldowns.py` in full, no entry for `battle` |
| Target resolution | Three shapes — see above. `get_members_tagged` takes every `@token`, not just the first two; a third+ tag is silently ignored (only `members_tagged[0]`/`[1]` are ever read) |
| Success output | `InputMediaGroup` (two photos, first captioned) as a **reply** to the trigger, immediately followed by a `sendPoll` **also as a reply** to the trigger — two separate API calls, two separate messages, no explicit link between them beyond both replying to the same message and sharing the same two names |
| Poll shape | `is_anonymous=False` (v1's own choice — the group can see who voted for what), `allows_multiple_answers=False`, exactly two options, native Telegram poll (`sendPoll`, `:343`/`:379`) — **not** an inline keyboard, **not** a custom vote-count anywhere in v1's own code |
| Failure output | `battle_no` (fewer than 2 known members for "random"), `battle_extract` (path A, one side's scrape came back empty — names *which* side), `battle_private` (path B scrape empty), `battle_no_picture` (path C, caller has no/private profile photo) — four distinct strings for four distinct failure points, none of them reused |
| Persistence | None — no table, no row, ever. The poll's vote tallies live entirely inside Telegram's own servers (this is a genuine native poll object, not a message the bot has to interpret button presses on) |
| Side effects | Up to two outbound HTTP GETs to `telegram.me` (paths A/B only) plus a GCS signed-URL read (paths B/C only) |
| External calls | Telegram: `sendChatAction`, `sendMediaGroup`, `sendPoll`, and (path C only) `getUserProfilePhotos`. Non-Telegram: `telegram.me` HTML scrape (paths A/B), GCS blob read (paths B/C) |
| Known defects | D-BT-1, D-BT-2, D-BT-3 below |
| What closes the poll | **Nothing.** No `stopPoll` call anywhere in v1, no timer, no admin command that targets it. A `/battle` poll stays open forever unless a human closes it by hand in their Telegram client (the poll creator or a group admin can always do that natively — nothing this bot does or needs to do). Confirms there is no vote-state defect of the kind `core_stickerspam`'s in-process counter had: the state was never in v1's process to begin with. |

## Defects — verdict per item

| id | Defect | Verdict |
|---|---|---|
| D-BT-1 | **Race condition**: paths A/B/C all write to hardcoded, non-namespaced local filenames (`user1.jpg`, `user2.jpg`, `user.jpg`, `SocialContent.py:325-326,356`) in the process's working directory. Two `/battle` invocations — different groups, same process, or even the same group in quick succession — overwrite each other's temp files mid-flight; a slow scrape on one request can lose a race to a faster one on another and send the wrong photo, or a half-written file. This is exactly the "state that should be per-request living in shared mutable storage" shape AGENTS.md's Phase 2 rule requires fixing, not preserving — it is a silent-failure/race bug, not a user-visible quirk. | **fix** |
| D-BT-2 | **Fragile, undocumented dependency**: fetching a *tagged* or *randomly chosen* member's photo via unauthenticated scraping of `telegram.me`'s HTML (paths A/B) depends on a page structure Telegram does not version or document, requires the target to have a public username and a publicly visible profile photo (many users have neither), and — unlike every other external call in this codebase — is not the Bot API at all. `util_embedder`'s contract already established the precedent for this exact class of problem: v1's synchronous `requests.get`-based link validation was a defect in the *mechanism*, not the user-visible *outcome*, and was replaced with a reliable mechanism that produces the same kind of result. | **fix the mechanism, preserve the outcome** (accepted, see "Decisions" above) — a two-photo battle for two known people is still the feature; how their photos get found is not something a user can observe today, so replacing it is not a behavioural change in the sense AGENTS.md guards against |
| D-BT-3 | **Crash on `battle_extract`'s substitution when reached via `"random"`**: v1 names the failing side with `members_tagged[0]`/`[1]` unconditionally (`:316,320`), regardless of whether the live branch is the two-tags case or the `"random"` case. A message like `/battle random` has zero `@` tags, so `members_tagged` is empty, and `members_tagged[0]` raises an uncaught `IndexError` the instant either randomly-picked member's photo extraction fails — propagating to the dispatcher's bare `except`, silently dropping the update. Found while reading the code for this spec, not previously documented anywhere. | **fix** — a silent-failure/crash bug, not a user-visible quirk; the redesign (R2.3 in `design.md`) names whichever side actually failed by construction, so this class of bug has no equivalent to reproduce |

## QA

`../Cookiebot-QA/features/fun_battle.feature` — one scenario:

```gherkin
Scenario: User creates a poll and users vote on it
    Given that the user is a member of the group
    When the user types the command /battle
    And tags another user in the group
    Then the bot should display a message "Who would win in a battle?" with options "Option A" and "Option B"
    And makes a poll in which the users can vote on who would win in a battle
```

Two things worth recording as conflicts, same pattern already established
for `fun_ship`/`util_everyone`:

1. **QA's "tags another user" (singular) is v1's path B, not path A.** One
   `@tag` means "this user vs. a random fighter," not "this user vs. the
   caller" or any other two-human interpretation. QA's paraphrase ("Option
   A"/"Option B") does not name what the two options actually are; v1's
   real poll options are the two display names (a username or a fighter
   name), never the literal strings "Option A"/"Option B." Nothing to
   reconcile — v1 code wins per AGENTS.md, the feature file keeps the QA
   wording and the step definitions drive the real v1 behaviour underneath,
   same pattern `util_everyone`'s "/ping everyone" mismatch already uses.
2. **QA never exercises paths A or C.** Both are net-new scenarios this
   port should add once it is unblocked, same precedent
   `util_everyone`/`fun_ship` already set for untested v1 branches.

## Implementation notes — how the accepted redesign answers "what does this member look like"

v2 already has the pieces to answer that reliably, without `telegram.me`,
without BeautifulSoup, without OpenCV, and without a single byte touching
local disk:

- `cb_core.members.roster(group_id)` returns `MemberRef(user_id, username)`
  for every member the bot has ever seen speak in the group — already
  built, already used by `fun_ship`/`util_everyone`. A tagged `@username`
  or a `random`-selected pair is resolved to a **real Telegram user id** by
  scanning the roster for a matching username — one in-memory filter over a
  query already being made, no new backend call shape.
- Once a `user_id` is known, `bot.get_user_profile_photos(user_id, limit=1)`
  is the *exact same Bot API call* v1's own no-tag path already uses for
  the caller — nothing new, just applied to a resolved id instead of only
  `msg['from']['id']`. An empty result is v1's existing failure
  (`battle_extract`), reached a more reliable way.
- The `file_id` `get_user_profile_photos` returns goes straight into a new
  `InputMediaGroup` (`InputMediaPhoto(media=file_id)`) with no download and
  no re-encode — this is what removes OpenCV, the temp files, and D-BT-1's
  race entirely: there is no local file to race over because there is no
  local file.

See `design.md` R1-R2 for the exact functions and their unit-test surface.
Path A ships with zero new dependencies — the handler is fast enough for
the reply path, same as every other fun command; there is no HTTP scrape
or image decode left to push into `cb-worker`.
