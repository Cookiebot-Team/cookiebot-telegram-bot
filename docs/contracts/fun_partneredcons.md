# Contract: fun_partneredcons (v1 -> v2)

Phase 2/6 of `/migrate-feature` for `/bff`, `/patas`, `/fursmeet`, `/furcamp`,
`/pawstral`, plus the net-new `/trex`. QA:
`../Cookiebot-QA/features/fun_partneredcons.feature`. FEATURE-MAP row:
`fun_partneredcons`. Spec: `.specs/features/fun_partneredcons/spec.md`. Files
owned by this port:
`packages/cb-gateway/src/cb_gateway/handlers/partneredcons.py` (new),
`packages/cb-gateway/src/cb_gateway/handlers/__init__.py` (one router line),
`packages/cb-core/src/cb_core/publisher.py` (`number_to_emojis`, the inverse of
the `emojis_to_numbers` already there),
`packages/cb-gateway/tests/test_partneredcons.py` (new),
`qa/features/fun_partneredcons.feature` (new), `qa/test_fun_partneredcons.py`
(new), this file.

## Phase 1 — where v1 lives

- Handler: `event_countdown`, `Miscellaneous.py:261-323`.
- Dispatch: `COOKIEBOT.py:248-251`.
- Image pools: `Miscellaneous.py:18-22` —
  `storage_bucket.list_blobs(prefix="Countdown/{Patas,BFF,FurSMeet,Furcamp,Pawstral}")`,
  exported by `cb_worker.bucket_export` and catalogued by `cb.py legacy-catalog`.
- Locale strings: `event.<name>.cta` (a list) and `event.error` — everything
  else in the `event` object (`name`, `caption`) is inert in v1 and stays inert
  here.
- Helper: `number_to_emojis`, `universal_funcs.py:346-351`.

## Phase 2 — v1 behaviour contract

| Aspect | v1 behaviour (file:line) |
|---|---|
| Triggers | `/patas`, `/bff`, `/fursmeet`, `/furcamp`, `/pawstral` — `msg['text'].lower().startswith(...)` inside the handler (`:264` etc.), matched case-sensitively by the dispatcher (`COOKIEBOT.py:248,250`) |
| Preconditions | **None.** This `elif` sits above `elif not utilityfunctions: notify_utility_off(...)` (`COOKIEBOT.py:253`) and outside the `functionsFun` block entirely, so neither switch reaches it |
| Cooldowns / quotas | None — no entry for any of the five names in `Cooldowns.py` |
| Success output | ① react `🔥` (`:262`) ② `sendChatAction upload_photo` (`:263`) ③ one random photo from the event's own `Countdown/*` prefix ④ a caption hardcoded per event in Python ⑤ sent as a reply to the trigger (`:323`) |
| Countdown | `(hardcoded_date - now).days + 1`, then `while daysremaining < -5: daysremaining += 365` (`:268-273`). The caption always prints the **hardcoded** day/month, never a wrapped-forward one |
| "Happening now" | `-5 <= daysremaining <= 0` ⇒ the caption becomes one bare YouTube link, the same for every event (`:270`) |
| Caption language | Portuguese for `patas`/`bff`/`fursmeet`/`furcamp`, English for `pawstral`, **regardless of the group's language**. Only the `cta` line is looked up per language, and only the `en` catalog carries the `event` object at all |
| Failure output | `event.error` when no prefix matches (`:319-321`) — dead code; the only call site has already matched one of the five |
| Persistence | None |
| External calls | GCS signed-URL read per invocation |

## `/trex` — net-new

`/trex` is in QA and in no v1 code path, not even as dead code. It does have 67
images under `Countdown/Trex` that no v1 code ever listed — found by diffing a
full bucket listing against `bucket_export.PREFIXES`, and exported since. The
three questions `spec.md` left open are answered here:

| Question | Answer |
|---|---|
| What picture? | One of the 67 `Countdown/Trex` objects, drawn at random like every other event's pool |
| Countdown or plain poster? | **Plain poster, no caption.** No date for the event exists in any of the three reference repos, and every caption string this feature has is an f-string about a specific date. Inventing one would be fabricating content about a real-world event |
| Gated or ungated? | **Ungated**, like its five siblings — it is the same handler and the same kind of post |

## Phase 6 — parity table

| Aspect | Verdict |
|---|---|
| Triggers | **same** (plus `/trex`, net-new) |
| Ungated dispatch — neither `functionsFun` nor `functionsUtility` | **same** |
| Reaction `🔥` then `upload_photo` chat action, before any pool read | **same** |
| Hardcoded dates, venue lines, ticket links, group handles | **same, verbatim** — including the three dates already in the past |
| `+ 1` day count and the `+365` wraparound (365 always, never 366) | **same, preserved** |
| Caption printing the hardcoded day/month while the count wraps | **same, preserved** |
| "Happening now" YouTube-link caption | **same** |
| Caption language mismatch (Portuguese to a Spanish group) | **same, preserved** — v1's executing code beats its own inert `caption` templates (AGENTS.md §1) |
| `cta` drawn at random per invocation | **same** |
| Image transport | **changed (mechanism only)** — bytes from `cb_core.storage` instead of a 15-minute GCS signed URL |
| Empty pool | **changed (fixed)** — logs `partneredcons.pool_empty` and sends nothing, where v1 raised `ValueError` inside `random.randint(0, -1)` |
| `event.error` dead branch | **not ported** — unreachable in v1 and unreachable here; the string stays in the catalog |

## QA

QA's six scenarios ask only that each command sends a picture; they say nothing
about the countdown, which is where all of v1's behaviour is. They are kept
verbatim as a `Scenario Outline`, and four net-new scenarios cover the caption,
`/trex`'s caption-less send, the ungated dispatch and the empty pool. QA's
duplicate `/fursmeet` scenario (byte-identical, listed twice) is an authoring
slip and is not reproduced — recorded in
`docs/site/content/docs/feature-map.mdx`.

## Tests

| Layer | File |
|---|---|
| Unit — countdown maths, wraparound, happening-now window, caption templates, the event table itself | `packages/cb-gateway/tests/test_partneredcons.py` |
| Acceptance — QA's six + four net-new | `qa/features/fun_partneredcons.feature`, `qa/test_fun_partneredcons.py` |

No integration-layer test: no persistence, no query.
