# fun_death — Specify

**Feature id:** `fun_death` · **Milestone:** M2 · **Kind:** v1 port
**v1 source:** `Bot/Miscellaneous.py:335-357` (`death`), dispatched
`Bot/COOKIEBOT.py:216,218-219,238-239`.

## Status: BLOCKED — see "The blocker" below before reading further

This spec documents v1's full behaviour so the port is mechanical once the
prerequisite lands. Nothing in `design.md` should be executed yet — there is
no `tasks.md` for the same reason.

## Goal

`/death` (aliased `/morte`, `/muerte`) posts a random "cause of death" for
the caller, a tagged name, or whoever they replied to: a random image or gif
from a themed pool, captioned with a randomised template line and a random
"reason" pulled from a 81-line pool. QA: `../Cookiebot-QA/features/fun_death.feature`
-> `qa/features/fun_death.feature` (not yet created).

## The blocker

v1's image pool is not a static file, checked into the bot's repo — it is a
live listing of a private GCS bucket, fetched at import time:

```python
# Bot/Miscellaneous.py:17
bloblist_death = list(storage_bucket.list_blobs(prefix="Death"))
```

`storage_bucket` (`universal_funcs.py:27`) is `storage_client.get_bucket("cookiebot-bucket")`
— a private bucket (v1 reads it via 15-minute signed URLs,
`fileblob.generate_signed_url(...)`, which is only necessary because the
bucket itself is not publicly readable). I checked all three places this
port could plausibly find the images:

1. **`../COOKIEBOT-Telegram-Group-Bot`'s static tree** — `Bot/Static/` has
   `locales/`, `Meme/` (for `fun_meme`) and `reclamacao/` (for
   `fun_complaint`, already vendored per its contract). No `Death/` directory
   and no death-prefixed image anywhere in the checkout
   (`find ../COOKIEBOT-Telegram-Group-Bot -iname '*death*' -not -path '*/locales/*'`
   returns nothing).
2. **This repo** — nothing under `packages/cb-core/src/cb_core/asset_data/`
   references death, and no importer, migration or config wires a GCS
   credential for `cookiebot-bucket` specifically (`.env.example`'s GCS entry
   is the generic `GOOGLE_APPLICATION_CREDENTIALS` var for `cb_core.storage`,
   v2's own media bucket — a different bucket for a different purpose, not a
   read path into v1's).
3. **This environment** — no GCP credentials are configured here, and even if
   they were, `cookiebot-bucket` belongs to v1's infrastructure, which is out
   of scope for anything this session can authenticate to.

HANDOFF §4 already flagged this ("needs media assets v1 kept in a GCS
bucket... nobody has copied them out of v1"); this investigation confirms it
is still true, not stale.

**v1 has no graceful empty-pool path to fall back to, either.** If
`bloblist_death` were empty, `random.randint(0, len(bloblist_death)-1)`
becomes `random.randint(0, -1)`, which raises `ValueError` (`randrange` with
`start > stop`) — the handler would simply crash. There is no "text-only"
behaviour anywhere in v1's code to port; inventing one would not be a port,
it would be new product behaviour dressed up as one, which is exactly what
AGENTS.md's "v2 must be backwards compatible with v1" opening line rules
out. QA's own scenario also only describes "reply with a meme and a random
skull gif" — the image is the feature, not decoration on top of a caption.

**Recommendation:** treat this as infrastructure-blocked, `Status.BLOCKED` in
`scripts/spec.py`, not `Status.PLANNED`.

**Update — the prerequisite has landed, and it is not the one this section
originally described.** The export exists: `cb.py gcs-auth provision` mints a
read-only, bucket-scoped credential from the operator's own Google account and
`cb.py cutover --only bucket` copies every prefix — `Death/` among them — into
`cb_core.storage`. So the blocker is gone, but the *shape* of the answer
changed with it, and this paragraph's original recommendation is superseded:

- **Not vendored into the repository.** This section proposed copying the
  bytes into `packages/cb-core/src/cb_core/asset_data/death/`, following
  `fun_complaint`'s 3.4 MB precedent. `Death/` is 34 objects and 21.5 MB, and
  the bucket as a whole is 1.34 GB — past what belongs in a wheel, and
  `fun_meme` already established the alternative for exactly this size
  problem: a small catalog ships as package data while the bytes live in
  `cb_core.storage`.
- **The pool comes from `cb_core.legacy_assets`.** The export writes
  content-addressed keys, so the v1 prefix a blob came from survives only in
  the export manifest; `cb.py legacy-catalog` turns that manifest into
  per-prefix catalogs under `asset_data/legacy/`, and
  `legacy_assets.choose("Death", rng)` is what this feature calls. Bytes are
  read through the entry's `storage_key`, never a key derived at the call
  site.

`design.md`'s asset-pool section (R2) and `tasks.md`'s `T0` still describe the
vendoring approach and should be read with this correction in mind.

## Behaviour contract (Phase 2)

| Aspect | v1 behaviour (file:line) |
|---|---|
| Triggers | `/death`, `/morte`, `/muerte` — `startswith` prefix match on the shared fun `elif` chain (`COOKIEBOT.py:216,238-239`). Aliases already live in v2: `cb_core/textmatch.py:43` maps `death`/`morte`/`muerte` -> `death` (out of this port's file ownership, already done). |
| Preconditions | Sits inside the `functionsFun` gate shared by every command in that `elif` chain — `if not funfunctions: notify_fun_off(...)` (`COOKIEBOT.py:218-219`). Off ⇒ `notify_fun_off`, `fun_off` locale key, sent as a reply. No admin check, no membership check. |
| Cooldowns / quotas | None. `Cooldowns.py` has no entry for `/death` or `death`. |
| Target resolution | ① more than one whitespace-separated token in the message ⇒ the **raw second token**, un-resolved, no membership lookup (`msg['text'].split()[1]`, `:341-342`) — same "use the literal token" behaviour `fun_ship`'s two-argument case already established as v1's house style. ② else, if the message is a reply ⇒ the replied-to sender's **first_name** (not username) (`:343-344`). ③ else ⇒ the caller's own `@username`, or bare `first_name` if they have none (`:345-346`) — **see the caption-prefix defect below**, this branch drops the skull emoji prefix when there is no username. |
| Success output | ① react `👻` (`:336`) ② `sendChatAction upload_photo` regardless of whether the chosen file turns out to be a gif (`:337`) ③ pick one random blob from the `Death` prefix (`:338`) ④ build the caption: `'💀💀💀 ' + <target>` (see the operator-precedence defect below) + `i18n["death.template"]` with a random `variant` substituted (`:348-350`) + `i18n["death.Reason"]` with a random line from `death.txt` substituted (`:351-353`) ⑤ send the blob as an **animation** if its filename ends `.gif`, else as a **photo**, in both cases as a **reply** to the trigger, `caption=<the built string>` (`:354-357`). No explicit `parse_mode` — v1's `send_message`/`send_photo` wrapper defaults to `HTML` (matches every other feature's caption in this codebase). |
| Failure output | None — no length check, no "no images" fallback. An empty blob list crashes (see "The blocker" above). |
| Persistence | None. No table, no backend call. |
| Side effects | One signed-URL round trip to GCS per call (v1 only — the whole reason v2's own asset accessor exists is to not need this). |
| External calls | v1: GCS `generate_signed_url` (no network call, computed locally from the service-account key) + Telegram `sendChatAction`/`sendAnimation`/`sendPhoto`. v2 (once unblocked): no GCS at all — local package asset, `FSInputFile`, same as `fun_complaint`. |
| Known defects | D-DE-1, D-DE-2 below. |

## Defects — verdict per item

| id | Defect | Verdict |
|---|---|---|
| D-DE-1 | Caption-prefix operator-precedence bug: `'💀💀💀 ' + '@'+msg['from']['username'] if 'username' in msg['from'] else msg['from']['first_name']` parses as `('💀💀💀 @' + username) if has_username else first_name` — a caller with no username gets a caption with **no skull-emoji prefix at all**, just their bare first name (`:345-346`). | **preserve** — user-visible quirk (the caption looks different depending on whether the target has a Telegram username), not a silent-failure or race bug; same category as `fun_ship`'s `@@alice + @@bob` double-sigil, which AGENTS.md's Phase 2 rule keeps as-is |
| D-DE-2 | `sendChatAction upload_photo` is sent unconditionally even when the chosen blob is a `.gif` and will be sent via `sendAnimation` (`:337`) | **preserve** — cosmetic (Telegram's "sending photo…" indicator briefly shows the wrong verb), not observable in any output a test can usefully assert beyond "some chat action was sent"; not worth a divergent code path for zero user-facing difference once it resolves |
| D-DE-3 | No empty-pool fallback; a `ValueError` from `random.randint(0, -1)` would propagate to the dispatcher's bare `except` (never actually reachable in v1, since the bucket has never been empty in production) | **fix, trivially** — v2's asset pool is a static package directory; if it were ever accidentally empty, degrading to "nothing to send" beats a raw traceback. Not a behavioural divergence a user could ever observe, since the pool is fixed at build time once vendored. |

## QA

`../Cookiebot-QA/features/fun_death.feature` — two scenarios, both already
matching v1 behaviour with no conflict:

```gherkin
Scenario: user uses the command /death
    Given that the user is in the group
    When the user sends the command /death
    Then the bot should reply with a meme and a random skull gif
    And random cause of death for the user

Scenario: user uses the command /death with another user tagged
    Given that the user is in the group
    When the user sends the command /death and tags another user
    Then the bot should reply with a meme and a random skull gif
    And random cause of death for the tagged user
```

No QA/v1 conflict to record — the two scenarios exercise exactly v1's target
resolution branches ① (own username) and ② (raw tagged token), and neither
requires a reply-based target, so that third branch is net-new test coverage
this port would add, same as `util_everyone`'s precedent for untested v1
paths.
