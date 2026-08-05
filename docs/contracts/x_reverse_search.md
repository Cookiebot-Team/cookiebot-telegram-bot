# Contract: x_reverse_search (v1 -> v2)

Phase 2/6 of `/migrate-feature` for `/buscarfonte`. QA: **none upstream** —
`qa/features/x_reverse_search.feature` is authored. FEATURE-MAP row:
`x_reverse_search` (added by this port; the map had none). Spec/design:
`.specs/features/x_reverse_search/{spec,design,tasks}.md`.

Files owned by this port: `cb_core/textmatch.py` (three aliases),
`cb_core/settings.py` (`saucenao_api_key`, `saucenao_timeout_seconds`),
`cb_core/jobs.py` (`REVERSE_SEARCH`), `.env.example`,
`packages/cb-gateway/src/cb_gateway/handlers/reverse_search.py`,
`packages/cb-worker/src/cb_worker/jobs/reverse_search.py`, the two
registrations, and the tests below.

## Phase 1 — where v1 lives

- Handler: `reverse_search`, `../COOKIEBOT-Telegram-Group-Bot/Bot/SocialContent.py:113-142`.
- Image resolution: `fetch_temp_jpg(..., only_return_url=True)`, `:86-98`.
- Client: `SauceNao(saucenao_key)`, `:15,21`.
- Dispatch: `COOKIEBOT.py:212-213`, under `utilityfunctions`.
- Strings: `reverse_image`, `reverse_other`, `reverse_limit`, `reverse_best`,
  `reverse_no_found` — already ported byte-identical in `lib.json`.

## Phase 2 — v1 behaviour contract

| Aspect | v1 behaviour (file:line) |
|---|---|
| Triggers | `/buscarfonte`, `/searchsource`, `/buscarfuente` (`COOKIEBOT.py:212`). None were in `COMMAND_ALIASES` before this port. |
| Preconditions | `utilityfunctions` only. No admin check, no cooldown (`Cooldowns.py` has no entry). |
| No reply | reply `reverse_image`, return (`:115-118`) |
| Image source | largest `photo` size, falling back to `document` on `KeyError` (`:86-98`) |
| Search | `SauceNao.from_url(telegram_file_url)` (`:119-120`) |
| Short limit | `ShortLimitReachedError` ⇒ `reverse_other`, return (`:121-124`) |
| Long limit | `LongLimitReachedError` ⇒ `reverse_limit`, return (`:125-128`) |
| Hit | `results and results[0].urls and results[0].similarity > 80` ⇒ react `🫡` (`is_big=False`), reply `reverse_best` + `f'"{title}"'` + `f" - {author}"` when present + `f"\n{urls[0]}\n\n"` (`:129-138`) |
| Miss | react `🤷` (`is_big=False`), reply `reverse_no_found` (`:139-142`) |
| Persistence | none |
| Side effects | `send_chat_action(typing)`; one reaction per outcome |
| External calls | SauceNAO, **with no timeout at either the call site or inside `saucenao_api`** |
| Known defects | D-RS-1 … D-RS-5 |

The threshold is **strictly** `> 80`, and only `results[0]` is ever consulted —
v1 never looks at a later result even when it would clear the bar. Both ported
as-is and asserted.

## Defects — verdict per item

| id | Defect | Verdict |
|---|---|---|
| **D-RS-1** | **The bot token is handed to a third party.** `fetch_temp_jpg(only_return_url=True)` builds `https://api.telegram.org/file/bot{cookiebotTOKEN}/{path}` (`:89,95`) and `reverse_search` passes it to SauceNAO (`:119-120`), which fetches it. The token is then in that service's access logs and any referer it forwards. Anyone holding it controls the bot. | **fixed.** v2 downloads the bytes with `bot.download()` and uploads them as a multipart `file` part. The URL is never constructed. Regression-tested by inspecting the outgoing request body — see below. |
| D-RS-2 | No timeout, on the reply path | **fixed** — moved to `cb-worker` (AGENTS.md §2.4) with `saucenao_timeout_seconds`, default 15.0 |
| D-RS-3 | Anything but the two rate-limit exceptions propagates uncaught, so an outage is silence in the group | **fixed** — degrades to `reverse_no_found`, the "nearest existing honest string" policy `util_youtube`'s D-YT-1 set |
| D-RS-4 | The answer is **machine-translated a second time**: it is already localised by `i18n.get("reverse_best", lang)`, and `send_message(..., language)` re-runs it through Google Translate for `eng`/`es` groups (`universal_funcs.py:195-198`) — which also translates the artwork's title and the artist's name | **not ported.** No v2 feature machine-translates an outgoing message; every ported handler resolves a string with `t(ctx, key)` and stops. Reproducing it means a live translation call on a path that already has the right string, and garbled proper nouns. |
| D-RS-5 | `fetch_temp_jpg` distinguishes photo from document by catching `KeyError`, so a reply carrying neither raises a second `KeyError` and the update dies with no reply | **fixed** — an explicit check; answers `reverse_image`, the same string the no-reply branch uses (v1 has no separate one and this port does not invent one) |

## D-RS-1 in detail, because it is the reason for the shape

The gateway resolves only a `file_id` and puts that on the queue. The worker
downloads the bytes where they are used and posts them:

```python
files = {"file": ("image.jpg", image, "image/jpeg")}
```

SauceNAO's REST API accepts either `url=` or a file upload; only one of them
leaks a credential. `test_the_bot_token_is_never_sent_to_saucenao` asserts the
request body contains a `file` part, contains no `url` part, and mentions
neither `api.telegram.org` nor `bot` in the URL — so reintroducing `url=`
fails the build rather than quietly re-leaking.

Downloading in the worker rather than the gateway also keeps the arq payload
scalar: the file id crosses the queue, the image does not.

## Other deliberate choices

- **REST over `httpx`, not `saucenao_api`.** One POST with four form fields;
  v2 already has `httpx` for every outbound call (AGENTS.md §5). Same call
  `util_youtube` made against `google-api-python-client`.
- **The two rate limits are read off `header.short_remaining` /
  `header.long_remaining`**, which is exactly what `saucenao_api` raises its
  two exceptions from — checked in v1's `except` order, short before long.
- **The author is looked up under `author_name`/`member_name`/`creator`/
  `author`/`artist`.** SauceNAO names it differently per index and
  `saucenao_api` normalises; without the same normalisation an author v1
  displayed would silently vanish.
- **The reply is sent with no `parse_mode`.** The answer interpolates a
  SauceNAO title and author verbatim, and one containing `<` or `&` would be
  rejected as bad HTML — v1's `send_message` defaults to `parse_mode='HTML'`
  and loses exactly those replies.

## Phase 6 — parity table

| Aspect | Verdict |
|---|---|
| All three trigger spellings | **same** (newly aliased — none resolved before) |
| `functionsUtility` gate, and that a gated-off command *answers* | **same** |
| No-reply refusal string | **same** |
| Image chosen: largest photo, else document | **same** |
| Similarity threshold `> 80`, and only `results[0]` | **same** |
| Short-limit and long-limit strings, and their precedence | **same** |
| Answer assembly, trailing newlines included | **same, byte-identical** |
| `🫡` / `🤷`, both `is_big=False`, and no reaction on a rate limit | **same** |
| Where the image bytes go | **changed (intentional, security fix)** — D-RS-1 |
| Request timeout | **changed (intentional, fix)** — v1 had none |
| A broken search vs. a genuine no-match | **same observable message**, distinguished only in telemetry |
| A reply carrying no image | **changed (intentional, fix)** — D-RS-5; v1 answered nothing at all |
| Double machine-translation of the answer | **changed (intentional, not ported)** — D-RS-4 |
| `parse_mode` on the reply | **changed (intentional)** — v1's HTML default drops any answer whose title contains `<` or `&` |
| Where the search runs, and therefore when the reply arrives | **changed (unavoidable consequence)** — a queue hop, the precedent `util_youtube` set |
| `send_chat_action(typing)` | **changed (intentional)** — no ported command sends one |

## Tests

| Layer | File |
|---|---|
| Unit — the three aliases, file-id resolution including D-RS-5 | `packages/cb-gateway/tests/test_reverse_search.py` |
| Unit — **the token-leak regression test**, the strict threshold at 79.9/80.0/80.1, author normalisation, both rate limits and their order, five degradation paths, the answer string, the three run outcomes, no `parse_mode` | `packages/cb-worker/tests/test_reverse_search_job.py` |
| Acceptance — six scenarios, authored | `qa/features/x_reverse_search.feature`, `qa/test_x_reverse_search.py` |

## QA vs v1

There is no upstream scenario. `feature-map.mdx` §4 already lists reverse
search among the 20+ v1 features the QA spec never covered; the six scenarios
here are transcribed from v1's behaviour, not from an intent document, and the
feature-map row is added by this port.
