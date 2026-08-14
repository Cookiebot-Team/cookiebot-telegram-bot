# Contract: x_image_search (v1 -> v2)

Phase 2/6 of `/migrate-feature` for `/qualquercoisa` **and for every command
the bot does not recognise**. No upstream QA scenario exists. FEATURE-MAP row:
`x_image_search`. Spec/design: `.specs/features/x_image_search/`. Files owned
by this port: `packages/cb-core/src/cb_core/image_search.py` (new),
`packages/cb-core/src/cb_core/asset_data/search/` (new, v1's blocklist
verbatim), `packages/cb-core/src/cb_core/settings.py` (two credentials, a
timeout, two caps), `packages/cb-core/src/cb_core/jobs.py` (one constant),
`packages/cb-core/src/cb_core/textmatch.py` (three aliases),
`packages/cb-gateway/src/cb_gateway/handlers/image_search.py` (new),
`packages/cb-gateway/src/cb_gateway/handlers/__init__.py` (one router line),
`packages/cb-worker/src/cb_worker/jobs/image_search.py` (new),
`packages/cb-worker/src/cb_worker/main.py` (one registration), the tests, and
this file.

## Phase 1 — where v1 lives

- Prompt: `prompt_qualquer_coisa`, `SocialContent.py:144-146`, dispatched
  `COOKIEBOT.py:258-259`.
- Search: `qualquer_coisa`, `SocialContent.py:147-170`, dispatched
  `COOKIEBOT.py:283-289` — the **last `elif`** of the command chain.
- Quotas: `Cooldowns.py:6-7,38-47`.
- Blocklist: `Static/avoid_search.txt`, read `SocialContent.py:31-33`.
- Locale strings: `anything_prompt`, `anything_no_find`, `image_limit`, all
  already ported byte-identical.

## Phase 2 — v1 behaviour contract

| Aspect | v1 behaviour (file:line) |
|---|---|
| Triggers (prompt) | `/qualquercoisa`, `/anything`, `/cualquiercosa` — prints the usage example, searches nothing |
| Trigger (search) | any `/word` no earlier branch claimed (`COOKIEBOT.py:283`) |
| Preconditions | `utilityfunctions`; `"//" not in text`; not addressed at another bot. Silence — not `utility_off` — when utility is off, because this is the chain's final `elif` |
| Quotas | 15/user/day, 180/bot/day, decremented before the check; over the limit replies `image_limit` |
| Blocklist | first word of the term, 49 entries, silent return **after** the quota is spent |
| Search term | `text.split("@")[0].replace("/", ' ')` — all slashes to spaces, truncated at the first `@`, leading space kept |
| Search | Custom Search, `num=10`, `filetype='jpg|gif|png'`, `safe='off'` (not SFW) / `'medium'` (SFW) |
| Success output | ten results shuffled; the first Telegram accepts is sent as a reply — `sendAnimation` when `'gif' in url`, else `sendPhoto` — captioned with that result's referrer URL |
| Failure output | react 🤷 + `anything_no_find`, for both "nothing found" and "everything failed to send" |
| Persistence | none |
| External calls | one search request; up to ten Telegram fetches of third-party URLs |

## Defects — verdict per item

| id | Defect | Verdict |
|---|---|---|
| D-IS-1 | The search and up to ten remote fetches run inline on the reply path, for every unrecognised command in every group. | **fixed** — both move to `cb-worker` (AGENTS.md §2.4); the gate, guards, quota and blocklist stay, as v1 checks them first too |
| D-IS-2 | Quotas are a per-process dict: "180 a day" meant 180 × five processes, reset by any restart, raced by a 50-thread pool. | **fixed** — `cache.incr_window`, so the cap means what it says across replicas. Stricter than v1 in practice, and deliberately so |
| D-IS-3 | `searchterm.split()[0]` raises `IndexError` on a wordless term. | **fixed** — treated as blocked, same silence, no traceback |
| D-IS-4 | The "addressed at another bot" check compares against five hardcoded usernames. | **fixed** — compares against the bot the update arrived on, so a sixth brand needs no code change |
| D-IS-5 | `'gif' in url` is a substring test on the whole URL. | **preserved** — a photo sent through `sendAnimation` arrives either way |

## The dispatch hazard, stated plainly

This is the only handler whose trigger is "nothing else matched", and getting
it wrong disables other features silently. Two rules keep it safe:

1. Its router is registered after every command router.
2. **Both non-matches raise `SkipHandler`.** Position alone is not enough:
   `welcome`'s `/newwelcome` reply prompt sits in the join chain, and
   `transcribe` and `fun_random` sit in the content-rules block, each because
   it also has a passive half. A handler that *returns* has handled the update
   and aiogram stops there — the first implementation of this feature did
   exactly that and silently disabled `/random`, `/transcribe` and
   `/newwelcome`. `qa/features/x_image_search.feature`'s "A real command is
   never turned into a search" scenario exists to catch a regression of it.

## Phase 6 — parity table

| Aspect | Verdict |
|---|---|
| `/anything` prompt, and its utility gate | **same** |
| Catch-all trigger, and the `//` and bot-address guards | **same** (the address check is now per-bot, D-IS-4) |
| Silence when utility is off | **same** |
| Quota arithmetic, including a refused call spending the global budget | **same** |
| Quota storage | **changed (fixed)** — shared Valkey counters, D-IS-2 |
| Blocklist contents and first-word matching, after the quota | **same** |
| Search term extraction, leading space and `@`-truncation included | **same** |
| `num`, `filetype`, `safe` values | **same** |
| Shuffle-and-send-the-first-that-works, referrer as caption | **same** |
| `'gif' in url` routing | **same, wart included** (D-IS-5) |
| 🤷 + `anything_no_find` on failure | **same** |
| Where it runs | **changed (fixed)** — search and sends in `cb-worker`, D-IS-1 |
| Wordless term | **changed (fixed)** — silence without an `IndexError`, D-IS-3 |

## Operational note

Two credentials, neither of which the chart or `.env.example` had before:
`CB_GOOGLE_SEARCH_API_KEY` and `CB_GOOGLE_SEARCH_CX`. With either missing the
job logs `image_search.no_credentials` and answers `anything_no_find` — the
same degradation an absent `CB_YOUTUBE_API_KEY` gives `/youtube`, so a
deployment without them loses the feature rather than erroring.

## Tests

| Layer | File |
|---|---|
| Unit — term extraction, the blocklist | `packages/cb-core/tests/test_image_search.py` |
| Unit — candidate rules, the quota | `packages/cb-gateway/tests/test_image_search.py` |
| Unit — the Google call and the send loop | `packages/cb-worker/tests/test_image_search_job.py` |
| Acceptance — ten scenarios | `qa/features/x_image_search.feature`, `qa/test_x_image_search.py` |

No integration-layer test: no persistence and no query of its own.
