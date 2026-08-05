# x_reverse_search — Specify

**Feature id:** `x_reverse_search` · **Milestone:** M3 · **Kind:** v1 port
**v1 source:** `Bot/SocialContent.py:113-142` (`reverse_search`), with
`fetch_temp_jpg` `:86-102`, dispatched `Bot/COOKIEBOT.py:212-213`.

## Goal

`/buscarfonte` (aliased `/searchsource`, `/buscarfuente`) replies to an image
with its source, found by reverse image search through SauceNAO.

No blocker: no bucket asset, no dead code. Its five locale strings are already
ported byte-identical in `lib.json` (`reverse_image`, `reverse_other`,
`reverse_limit`, `reverse_best`, `reverse_no_found`).

## Phase 2 — v1 behaviour contract

| Aspect | v1 behaviour (file:line) |
|---|---|
| Triggers | `/buscarfonte`, `/searchsource`, `/buscarfuente` (`COOKIEBOT.py:212`). **None are in `COMMAND_ALIASES` yet** — this port adds all three. |
| Preconditions | `utilityfunctions` only (`COOKIEBOT.py:212`, the `elif` is gated on it). No admin check, no cooldown (`Cooldowns.py` has no entry). |
| No reply | `'reply_to_message' not in msg` ⇒ reply `reverse_image` ("Reply an image with the command to search for the source (reverse search)\n<blockquote> For direct search, use /anything </blockquote>"), return (`:115-118`) |
| Image source | `fetch_temp_jpg(..., only_return_url=True)` — the largest `photo` size, falling back on `KeyError` to `document` (`:86-98`). Returns a Telegram file URL. |
| Search | `SauceNao(saucenao_key).from_url(url)` (`SocialContent.py:15,21`, `:120`) |
| Short limit | `errors.ShortLimitReachedError` ⇒ `reverse_other` ("I'm still processing other results, please wait and try again"), return (`:121-124`) |
| Long limit | `errors.LongLimitReachedError` ⇒ `reverse_limit` ("Daily search limit reached, please wait and try again"), return (`:125-128`) |
| Hit | `results and results[0].urls and results[0].similarity > 80` ⇒ react `🫡` (`is_big=False`), then reply `reverse_best` + `f'"{title}"'` + `f" - {author}"` when there is an author + `f"\n{urls[0]}\n\n"` (`:129-138`) |
| Miss | react `🤷` (`is_big=False`), then reply `reverse_no_found` ("The search found no matches, it seems to be an original image!") (`:139-142`) |
| Persistence | none |
| Side effects | `send_chat_action(typing)` (`:114`); one reaction per outcome |
| External calls | SauceNAO. **No timeout anywhere** — neither `saucenao_api` nor the call site sets one. |
| Known defects | D-RS-1 … D-RS-5 below |

Note the **similarity threshold is strictly greater than 80**, and it is only
consulted for `results[0]` — v1 never looks past the first result even when a
later one clears the bar. Both ported as-is.

## Defects — verdict per item

| id | Defect | Verdict |
|---|---|---|
| **D-RS-1** | **The bot token is handed to a third party.** `fetch_temp_jpg(only_return_url=True)` builds `https://api.telegram.org/file/bot{cookiebotTOKEN}/{path}` (`:89,95`) and `reverse_search` passes that URL straight to SauceNAO (`:119-120`). SauceNAO fetches it, and the full bot token is in the URL it receives, its access logs, and any referer it forwards. Anyone holding that token controls the bot. | **fix — this one is not negotiable.** v2 downloads the file itself and uploads the *bytes* to SauceNAO. AGENTS.md §2.5 ("no secrets… no credentials in code") is about the same class of leak. |
| D-RS-2 | No timeout on the SauceNAO call, on the reply path (v1's threaded workers absorb it; v2's single event loop per replica would not) | **fix** — moved to `cb-worker` per AGENTS.md §2.4, with `settings.saucenao_timeout_seconds` |
| D-RS-3 | Every failure that is not one of the two rate-limit types propagates uncaught into the global traceback handler — a SauceNAO outage, a malformed response or a network error is silence in the group | **fix** — degrades to `reverse_no_found`, the same "nearest existing honest string" policy `util_youtube`'s D-YT-1 and `util_calladms`'s `admin_usernames` already set |
| D-RS-4 | The answer is **machine-translated a second time**. `answer` is already localised by `i18n.get("reverse_best", lang=language)`, and then `send_message(..., language)` re-runs it through Google Translate for `eng`/`es` groups (`universal_funcs.py:195-198`) — which also translates the artwork's *title and author name*. | **not ported.** No v2 feature machine-translates an outgoing message; every ported handler resolves a string with `t(ctx, key)` and stops. Reproducing it would mean a live translation call on a path that already has the right string, and would garble proper nouns. Recorded here and in the contract. |
| D-RS-5 | `fetch_temp_jpg` distinguishes photo from document by catching `KeyError` on `msg['photo']`, so a reply carrying *neither* raises `KeyError` on `msg['document']` and the update dies silently | **fix** — an explicit check; a reply with no image answers `reverse_image`, the same string the no-reply branch uses (v1 has no separate string and this port does not invent one) |

## QA

**No scenario exists** — `../Cookiebot-QA/features/` has nothing for this
feature, and `feature-map.mdx` §4 already lists reverse search among the 20+
v1 features the spec never covered. `qa/features/x_reverse_search.feature` is
**authored**, not ported, and a `feature-map.mdx` row is added as part of this
work (the map has none today).
