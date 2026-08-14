# x_image_search — Specify

**Feature id:** `x_image_search` · **Milestone:** M3 · **Kind:** v1 port
**v1 source:** `Bot/SocialContent.py:144-146` (`prompt_qualquer_coisa`) and
`:147-170` (`qualquer_coisa`), dispatched `Bot/COOKIEBOT.py:258-259` and
`:283-289`. Quotas: `Bot/Cooldowns.py:6-7,38-47`. Blocklist:
`Bot/Static/avoid_search.txt`, read at `SocialContent.py:31-33`.

## Goal

`/qualquercoisa`, `/anything`, `/cualquiercosa` print a usage line. **Every
other unrecognised `/command` is a Google image search for its own text** —
`/french fries` posts a picture of french fries, captioned with the page it
came from.

That second sentence is the feature. It is why this row was left `PLANNED`
long after the rest of M3: `scripts/spec.py` described it as "image search
(qualquer coisa)", which undersells it — it is v1's catch-all, it interacts
with every command that does not exist, and it is metered.

## Phase 2 — v1 behaviour contract

| Aspect | v1 behaviour (file:line) |
|---|---|
| Triggers (prompt) | `/qualquercoisa`, `/anything`, `/cualquiercosa` -> `anything_prompt`, no search (`COOKIEBOT.py:258-259`) |
| Trigger (search) | **any** `/word` that reached the end of the command chain (`COOKIEBOT.py:283`) |
| Preconditions | `utilityfunctions`, **and** `"//" not in text`, **and** the command is not addressed at another bot (`len(text.split('@')) < 2 or text.split('@')[1] in [five persona usernames]`). Being the final `elif`, a group with utility off gets **silence**, not `utility_off` |
| Quotas | 15/user/day and 180/bot/day (`Cooldowns.py:6-7`), decremented **before** the check, so the crossing call is the refused one; over the limit replies `image_limit` (`COOKIEBOT.py:284-289`) |
| Blocklist | the search term's **first word** in `avoid_search` (49 entries) -> silent return, *after* the quota is already spent (`SocialContent.py:149-150`) |
| Search term | `text.split("@")[0].replace("/", ' ')` — every slash becomes a space, the query is truncated at the first `@`, and the leading space stays (`:148`) |
| Search | Google Custom Search, `num=10`, `filetype='jpg|gif|png'`, `safe='off'` when the group is not SFW and `'medium'` when it is (`:153-156`) |
| Success output | the ten results shuffled, then the first one Telegram accepts: `sendAnimation` when `'gif' in url` else `sendPhoto`, captioned with the result's **referrer** URL, as a reply (`:158-166`) |
| Failure output | react 🤷 then `anything_no_find` — reached both when Google returns nothing and when every result fails to send (`:167-170`) |
| Persistence | none |
| External calls | one Custom Search request; up to ten Telegram fetches of third-party URLs |

## Defects — verdict per item

| id | Defect | Verdict |
|---|---|---|
| D-IS-1 | The whole feature runs inline on the reply path — an external search plus up to ten remote-URL fetches, for *every unrecognised command in every group*. | **fix** — the search and the send loop move to `cb-worker` (AGENTS.md §2.4), like `util_youtube`'s already did. The gate, the guards, the quota and the blocklist stay, because none of them touches the network and v1 checks them first too |
| D-IS-2 | Quotas live in a per-process dict (`Cooldowns.py:9,38-47`), so "180 a day" was really 180 per process across five processes, reset by any restart, and raced by v1's own 50-thread pool. | **fix** — `cache.incr_window`, the primitive `core_stickerspam` already uses. The cap now means what it says |
| D-IS-3 | `searchterm.split()[0]` raises `IndexError` for a term with no words (`/@x` reaches it with `" "`), swallowed by the dispatcher's bare `except`. | **fix** — `is_avoided` treats an empty term as avoided, reaching the same silence without the traceback |
| D-IS-4 | The bot-address check compares against five hardcoded usernames, so a sixth brand's `/cat@SixthBot` would have been searched by every other brand. | **fix** — compare against the username of the bot the update arrived on |
| D-IS-5 | `'gif' in image.url` is a substring test against the whole URL, so a PNG under a `/gifts/` path is sent as an animation. | **preserve** — Telegram delivers it either way; nothing observable changes and "fixing" it would change which API call a result goes through for no gain |

## QA

No upstream `Cookiebot-QA` scenario exists — confirmed against the full
listing. `qa/features/x_image_search.feature` is authored locally, and
deliberately includes one scenario that is not about searching at all: **a
real command is never turned into a search**. That is the way this feature
breaks every other one, and it is not hypothetical — the first implementation
returned instead of raising `SkipHandler` and silently disabled `/random`,
`/transcribe` and `/newwelcome`, all three of which are registered after the
catch-all for reasons of their own.
