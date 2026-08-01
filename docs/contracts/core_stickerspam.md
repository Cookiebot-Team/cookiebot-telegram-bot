# Contract: core_stickerspam (v1 -> v2)

Phase 2/6 of `/migrate-feature` for the anti sticker-flood feature. QA:
`../Cookiebot-QA/features/core_stickerspam.feature`. FEATURE-MAP row:
`core_stickerspam`, status `⚠ counter in unlocked process-local dict` (this is
FEATURE-MAP D6). Files owned by this port:
`packages/cb-gateway/src/cb_gateway/handlers/stickerspam.py`,
`qa/features/core_stickerspam.feature`, `qa/test_core_stickerspam.py`,
`packages/cb-gateway/tests/test_stickerspam.py`, this file.

## Phase 1 — v1 source

```python
# Bot/Cooldowns.py:8-22
last_used_sticker = {}


def sticker_anti_spam(cookiebot, msg, chat_id, stickerspamlimit, language):
    if chat_id not in last_used_sticker:
        last_used_sticker[chat_id] = 0
    else:
        last_used = int(last_used_sticker[chat_id]) + 1
        if last_used == int(stickerspamlimit):
            text = i18n.get("flood_stickers", lang=language)
            send_message(cookiebot, chat_id, text, msg)
        if int(last_used) > int(stickerspamlimit):
            delete_message(cookiebot, telepot.message_identifier(msg))
        last_used_sticker[chat_id] = last_used


# Bot/Cooldowns.py:49-50
def sticker_cooldown_updates(chat_id):
    last_used_sticker[chat_id] = 0
```

Call site, `Bot/COOKIEBOT.py`:

```python
# :111-113 — config unpacked once per update; stickerspamlimit is index 2
FurBots, sfw, stickerspamlimit, limbotimespan, captchatimespan, funfunctions, \
    utilityfunctions, language, publisherpost, publisherask, threadPosts, \
    maxPosts, publisherMembersOnly = get_config(cookiebot, chat_id, is_alternate_bot=is_alternate_bot)
...
elif content_type == "sticker":               # :179-180
    sticker_anti_spam(cookiebot, msg, chat_id, stickerspamlimit, language)
    if sfw and 'username' in msg['from']:      # :181-182 — different feature (sticker DB), not this one
        add_to_sticker_database(msg)
    if funfunctions and ...:                   # :183-184 — different feature (sticker auto-reply), not this one
        reply_sticker(cookiebot, msg, chat_id)
...
if chat_type != 'private' and content_type != "sticker":   # :317-318
    sticker_cooldown_updates(chat_id)
```

`stickerspamlimit` default: `Configurations.py:111` — `..., stickerspamlimit, ... = 1, 1, 5, ...` → **5**. The Java backend's `Config.stickerSpamLimit` (`Config.java:23`) carries no default of its own; the Python side's `5` is the only real default (`docs/contracts/group-config.md` already documents this pattern for other fields).

The `elif content_type == "sticker"` branch is reached only after the private-chat early return earlier in the same function (`COOKIEBOT.py:106-110`), so v1 never ran this in a private chat either — irrelevant in practice (Telegram doesn't deliver stickers-in-groups differently, but private chats have no `chat_id` shared state to spam).

## Phase 2 — v1 behaviour contract

| Aspect | v1 behaviour (file:line) |
|---|---|
| Trigger | Any message with `content_type == "sticker"` in a non-private chat (`COOKIEBOT.py:179`, reached after the private-chat return at `:106-110`). Not gated on `funfunctions`/`utilityfunctions` — sits outside both feature-area checks. |
| Preconditions | **None.** No admin exemption — `sticker_anti_spam` is called for every sticker sender unconditionally, admin or not. No feature flag. |
| Counter scope | **Per group (`chat_id`), not per user.** `last_used_sticker` is keyed by `chat_id` alone (`Cooldowns.py:8`) — every sticker from every member of the group increments the same counter, so one user's flood can get a *different* user's next sticker deleted. |
| Counter reset | **Nothing time-based.** The only reset is `sticker_cooldown_updates(chat_id)`, called at `COOKIEBOT.py:317-318` whenever the *current* update is a non-private, non-sticker message — i.e. any other kind of chat activity zeroes the counter. In a chat where stickers are the only traffic, or across the five v1 processes (a message may land on a different process than the one holding the accumulated count), the counter never resets and the group is stuck perpetually over the limit — **this is FEATURE-MAP D6**, called out explicitly by this task as a defect, not behaviour to preserve. |
| Counting logic | On the *first* sticker ever seen for a `chat_id` in a given process, the dict is merely initialised to `0` — no warn, no delete on that first call. From the second sticker onward, `last_used = last_used_sticker[chat_id] + 1`; if `last_used == stickerspamlimit` exactly, warn; if `last_used > stickerspamlimit`, delete. The two conditions cannot both fire on the same sticker (warn fires once, delete fires on every sticker after). |
| Success/limit-hit output | **Warn:** reply-style send (`send_message(..., msg)`, i.e. a reply to the triggering sticker) of the catalog string `flood_stickers`. English: `"Be careful with sticker flood"` (`Bot/Static/locales/eng/lib.json:43`; pt: `"Cuidado com o flood de stickers"`; es: `"Cuidado con las inundaciones de stickers"`). Already ported byte-for-byte into `cb_core/locale_data/{en,pt,es}/lib.json` — no locale file changes needed for this port. **Delete:** `delete_message` on the triggering sticker message itself (`telepot.message_identifier(msg)`), silently swallowing any exception (`universal_funcs.py:340-344`, `except Exception: print(e)`). |
| What actually happens at the limit | **Warn only** at the exact limit, **delete only** strictly past it. Never both on the same message, and **never a restriction** (`restrictChatMember`) — this feature does not mute or ban anyone, unlike `core_mediarestrict` or the captcha flow. |
| Persistence | None. Purely in-memory (`last_used_sticker` dict), never written to the v1 backend. |
| Side effects | None beyond the warn/delete above. |
| External calls | None. |
| Known defects | **FEATURE-MAP D6** ("Caches unbounded + unlocked, never expire (5 processes = 5 divergent views)", `Cooldowns.py:8-10` is explicitly listed). This port fixes it: see "Deliberate fix" below. |

### Config

`ctx.config.sticker_spam_limit` (`cb_core/group_config.py:53`, default `5`, matching v1) and `ctx.config.sticker_spam_window_s` (`:54`, default `60`) already exist in `GroupConfig` — no changes needed to that file for this port. `sticker_spam_window_s` is a **v2 addition with no v1 equivalent**: v1 had no concept of a time window at all (see "Counter reset" above).

### Deliberate fix: the window (not preserved from v1)

v1's reset mechanism ("any non-sticker message in the chat, in the *same process* that holds the count") is not a real anti-flood window — it is an accident of how the dict happened to get touched, and it does not survive v2's multi-replica gateway at all: porting the dict literally would mean five gateway replicas each keeping their own count for the same group, exactly reproducing D6 in a new shape. `cb_core.cache.incr_window(key, window_seconds)` replaces it with one atomic, Valkey-backed, cross-replica counter scoped to `sticker_spam_window_s`. The **warn-at-`==`-limit, delete-at-`>`-limit** thresholds are preserved exactly; only *how the count is kept and when it resets* changes, deliberately, per this task's explicit instruction.

`cb_core.cooldowns.SlidingWindow` (compiled, per-process) was considered and rejected for this counter specifically: it is in-memory and does not share state across gateway replicas, so a sticker flood split across replicas would under-count on each one — the same failure mode D6 describes, just moved from "unlocked dict" to "unshared in-process window". `SlidingWindow` remains the right tool for anything genuinely per-process and hot (its own docstring mentions this feature, but that predates the cross-replica requirement this task states explicitly); `incr_window` is the one that is actually shared.

### Cache outage: fail open, not closed

If Valkey is down, `cache.incr_window` raises. The handler (`_bump` in `stickerspam.py`) catches this, logs a warning, and returns `None`; the caller treats `None` as "cannot tell" and takes **no action at all** — no warning, no deletion. Justification: the alternative (fail closed, i.e. treat an unknown count as "over the limit") would turn a Valkey blip into every sticker, from every user, in every group, being silently deleted — a correctness outage far worse and far more visible than a temporary loss of anti-spam enforcement. This is pinned by `test_bump_fails_open_when_the_cache_is_unreachable` and `test_no_action_at_all_when_the_cache_is_down` (unit) and `test_cache_outage_fails_open_not_closed` (acceptance, against the real un-faked `cb_core.cache`, since this suite genuinely runs with no Valkey).

### QA-spec mismatch found while writing this contract

The upstream scenario "The feature is set up to allow sticker spam" has no v1 equivalent switch — `stickerSpamLimit` (`Configurations.py:111`, `Config.java:23`) is only ever a number, never a boolean toggle. The only real lever an admin has is a limit high enough that no realistic flood trips it, so this port's step for that Given seeds `sticker_spam_limit = 1_000_000` rather than any "disabled" flag. Recorded here per AGENTS.md ("record the conflict... rather than silently picking"); `docs/site/content/docs/feature-map.mdx`'s `core_stickerspam` row could use a note pointing at this, but that file is out of this port's ownership.

## Phase 3 — QA scenario

Copied `../Cookiebot-QA/features/core_stickerspam.feature` verbatim into
`qa/features/core_stickerspam.feature` (both original scenarios, wording
unchanged), then added, for v1 behaviour the original spec never exercises:

1. **"The bot keeps deleting stickers sent after the warning"** — the spec only
   asserts the warning; v1 also deletes every sticker past the limit, ported
   the same way (see "Counting logic" above).
2. **"Sticker spam is counted per group, not per user"** — the spec's wording
   never distinguishes per-user from per-group counting; v1's `chat_id`-only
   key means it is per-group, and the surprising cross-user collateral effect
   is real, observable v1 behaviour worth pinning down explicitly.
3. **"An admin is not exempt from the sticker spam limit"** — v1's call site
   has no admin check; without this scenario a future change could add an
   accidental admin exemption and nothing would catch it.

The cache-outage / fail-open behaviour is **not** added as a Gherkin scenario:
it has no v1 equivalent (v1 never had a shared cache to lose), so per Phase 3's
instruction to add scenarios "for v1 behaviour the spec missed" rather than
new v2-only concerns, it is instead covered by a plain (non-BDD) pytest test in
`qa/test_core_stickerspam.py` and thoroughly by unit tests.

## Implementation (v2)

`packages/cb-gateway/src/cb_gateway/handlers/stickerspam.py`:

- `router.message(F.sticker, F.chat.type.in_({"group", "supergroup"}))` — no
  `AdminOnly`, no `FeatureGate`, matching v1's unconditional dispatch outside
  private chats.
- `_key(group_id)` — `"cb:stickerspam:{group_id}"`, deliberately carrying no
  user id (see "Counter scope" above).
- `_bump(group_id, window_seconds)` — the one seam that talks to
  `cb_core.cache.incr_window`; returns `None` on any cache failure (fail
  open), never raises.
- `sticker_anti_spam(message)` — `count == limit` -> reply with
  `t(ctx, "flood_stickers")`; `count > limit` -> `bot.delete_message(...)`
  wrapped in `contextlib.suppress(Exception)`, matching v1's swallow-and-print.

## Phase 6 — Parity table

| Row | Verdict | Note |
|---|---|---|
| Trigger: sticker message, non-private chat | same | `F.sticker` + `F.chat.type.in_({"group","supergroup"})`. |
| No admin exemption | same | preserved deliberately — real v1 quirk, not a bug (`test_admins_are_not_exempt`, `core_stickerspam.feature`'s new admin scenario). |
| No feature-flag gate | same | sits outside `functionsFun`/`functionsUtility`, like v1. |
| Counter scope: per group, not per user | same | `_key` carries only `group_id` (`test_key_is_scoped_to_the_group_only`, "per group" QA scenario). |
| Warn at exactly the limit | same | `count == limit`. |
| Delete strictly past the limit | same | `count > limit`, every subsequent sticker, not just the first over. |
| Warn text | same | `flood_stickers` catalog key, already ported byte-for-byte; no locale changes needed. |
| Delete swallows its own failure | same | `contextlib.suppress(Exception)`, mirroring `universal_funcs.py:340-344`. |
| Never restricts/mutes/bans | same | no `restrictChatMember` call anywhere in this handler, matching v1 exactly. |
| Counter storage: unlocked per-process dict, no window | **changed (intentional, fix — FEATURE-MAP D6)** | replaced with `cb_core.cache.incr_window`: atomic, Valkey-shared across gateway replicas, bounded by `sticker_spam_window_s`. This is the one deliberate behaviour change; see "Deliberate fix" above. |
| Counter reset trigger | **changed (intentional, fix)** | v1 reset on an unrelated non-sticker message in the same process; v2 resets on a time window elapsing, in every replica, uniformly. Never "stuck spam forever" (D6). |
| `sticker_spam_window_s` config field | **new (v2 addition)** | v1 had no window concept at all; default `60`. |
| Cache-down behaviour | **new (v2 addition, fail open)** | v1 had no shared cache to lose. Explicit decision: silence, never "everything is spam." |
| First-ever sticker in a fresh dict does nothing (v1 quirk) | **not ported (behaviourally equivalent)** | `incr_window`'s first call for a fresh key returns `1`, matching v1's *second* sticker (its first call only zero-initialises and checks nothing). The skipped v1 "zeroth" check was a dict-initialisation artifact, not intended anti-spam behaviour, and produces the same first three observable counts either way. |
| `sfw` / sticker-DB auto-reply, `funfunctions` sticker auto-reply | **not this feature** | `COOKIEBOT.py:181-184`'s other two branches in the same `elif` are separate features (sticker database, sticker auto-reply) owned by other ports; not touched here. |

## Known gaps for whoever owns the listed files

- `packages/cb-gateway/src/cb_gateway/handlers/__init__.py` does not import or
  register `stickerspam.router` — needs `root.include_router(stickerspam.router)`
  (plus the import) for `qa/test_core_stickerspam.py` to pass end to end. Out
  of this port's file ownership.
- `docs/site/content/docs/feature-map.mdx`'s `core_stickerspam` row could use a note that the
  QA-spec "allow sticker spam" scenario has no real v1 toggle (see the
  mismatch above); this agent could not edit that file.
