# x_image_search — Design

## Module placement

| Piece | Where | Reuses |
|---|---|---|
| Blocklist + term extraction | `cb_core/image_search.py` (new), data in `cb_core/asset_data/search/avoid_search.txt` | `importlib.resources`, the idiom `locales.py` uses |
| Gate, guards, quota, enqueue | `cb_gateway/handlers/image_search.py` (new) | `cache.incr_window`, `context_for`, `enqueue` |
| Google call + send loop | `cb_worker/jobs/image_search.py` (new) | `youtube.py`'s job shape verbatim |
| Settings | `cb_core/settings.py` | the `youtube_api_key`/timeout pattern |
| Job constant | `cb_core/jobs.py` | — |

## R1 — the split

**R1.1** Reply path: the utility gate, the `//` guard, the addressed-at-another-bot
guard, the quota and the blocklist. All local, all v1-first-checked.

**R1.2** Worker: the Custom Search request and the up-to-ten send attempts
(D-IS-1). `youtube.py` is the template, including `set_http_client` and not
importing back into `cb_worker.main`.

## R2 — dispatch, the dangerous part

**R2.1** The catch-all's filter is `F.text.startswith("/")` in a group, and its
router is registered after every command router.

**R2.2** **Both non-matches raise `SkipHandler`, never return.** Position is
not enough: `welcome` (the `/newwelcome` reply prompt), `transcribe` and
`fun_random` own real commands and are registered later, each because it also
has a passive half. A handler that returns has handled the update, and aiogram
stops there. This is the single highest-risk line in the feature and has a
scenario of its own.

**R2.3** `parse_command(text) is not None` -> `SkipHandler`. A known command
must never become a search, whether its handler already ran or is registered
below.

**R2.4** `/anything` and its two aliases are an ordinary `CommandName` handler
in the same router, gated on utility, replying `anything_prompt`.

## R3 — the quota

**R3.1** Two `cache.incr_window` keys, `cb:imgsearch:u:<id>:<YYYYMMDD>` and
`cb:imgsearch:all:<YYYYMMDD>`, one-day window. The date is in the key so the
window rolls over without a stored `date` field to compare (v1's
`Cooldowns.py:40,44`).

**R3.2** Both counters increment before either is compared, reproducing v1's
"a refused call still spends the global budget" (`COOKIEBOT.py:284-285`).

**R3.3** Caps are settings, defaulting to v1's 15 and 180.

**R3.4** A Valkey outage fails **open**. Refusing every search during a cache
blip is worse than briefly exceeding a soft cap, and it is the direction every
other cache path in this codebase already fails.

## R4 — the term and the blocklist

**R4.1** `search_term` reproduces `text.split("@")[0].replace("/", " ")`
exactly, leading space included (v1 sends that string; trimming it would send a
different one).

**R4.2** `is_avoided` checks the first word only, and treats a wordless term as
avoided rather than raising (D-IS-3).

**R4.3** The blocklist is vendored byte-for-byte as package data; it is v1
content, not a v2 decision.

## R5 — safe search

**R5.1** `safe='medium'` when the group is SFW, `'off'` when it is not
(`SocialContent.py:153-156`). Sent unchanged even though Google now documents
`active`/`off`: v1's request is the contract, and an unknown value falls back
to Google's default rather than failing.

## Open decisions — answered

1. **Silence, not `utility_off`, for the catch-all** when utility is off — it
   is the final `elif` of v1's chain, so nothing answers. `/anything` itself
   *does* reply `utility_off`, because it sits earlier in the same chain.
2. **The blocklist runs after the quota**, as in v1: typing `/etc` costs you a
   search.
3. **No cooldown beyond the daily caps.** `Cooldowns.py` has none for this.
