# Contract: fun_meme (v1 -> v2)

Phase 2/6 of `/migrate-feature` for `/meme`. **No QA scenario exists** —
`qa/features/fun_meme.feature` is authored as part of this port (AGENTS.md §5).
FEATURE-MAP row: `fun_meme`. Spec: `.specs/features/fun_meme/spec.md`.

Files owned by this port:
`packages/cb-core/src/cb_core/meme_templates.py` (new),
`packages/cb-core/src/cb_core/asset_data/meme/` (v1's CSV, verbatim),
`packages/cb-core/pyproject.toml` (package data),
`packages/cb-core/src/cb_core/jobs.py` (`COMPOSE_MEME`),
`packages/cb-gateway/src/cb_gateway/handlers/meme.py` (new),
`packages/cb-gateway/src/cb_gateway/handlers/__init__.py` (registration),
`packages/cb-worker/src/cb_worker/jobs/meme.py` (new),
`packages/cb-worker/src/cb_worker/meme_seed.py` (new),
`packages/cb-worker/src/cb_worker/main.py` (registration),
`scripts/cb.py` (`meme-seed`), and the tests below.

## Phase 1 — where v1 lives

- Handler: `meme`, `../COOKIEBOT-Telegram-Group-Bot/Bot/SocialContent.py:224-277`.
- Template catalog: `Bot/Static/Meme/meme_metadata.csv`, loaded at import
  (`SocialContent.py:35-47`), generated offline by `Bot/Static/Meme/metadata.py`
  (OpenCV `inRange((0,210,0),(40,255,40))` + `findContours` → bounding rects).
- Templates: `Bot/Static/Meme/{English,Portuguese}/` — **803 rows, 801 files,
  110 MB**.
- Profile pictures: `get_profile_image`, `SocialContent.py:279-292` — a
  BeautifulSoup scrape of `https://telegram.me/{username}`.
- Dispatch: `COOKIEBOT.py:214,222-223` — the `funfunctions` chain.
- Locale strings: `meme_no`, `meme_error` — already ported byte-identical, all
  three languages.

## Phase 2 — v1 behaviour contract

| Aspect | v1 behaviour (file:line) |
|---|---|
| Trigger | `/meme` — one spelling, no alias |
| Preconditions | `functionsFun` only |
| Too many tags | `len(members_tagged) > 5` ⇒ `meme_no`, before anything else (`:230-233`) |
| Tag parsing | `get_members_tagged` — split on `"@"`, drop the head, drop anything ending in `"bot"` (`:104-111`) |
| Language | `'Portuguese' if 'pt' in language.lower() else 'English'` — **`es` gets English** (`:234`) |
| Template choice | every template whose `blob_count` is in `range(len(tagged), 6)`; if that is empty *and* the language is Portuguese, retry against English; then `random.choice` (`:236-245`) |
| Empty pool | **`NameError`** — `contours_green` is assigned only inside `if suitable_templates:` and read straight after it (`:244-248`) |
| Filling a rectangle | drain the tagged list first (removing each from the roster as it goes), then draw from the roster; skip anyone whose picture cannot be fetched (`:257-268`) |
| Nobody usable | `meme_error` (`:269-272`) |
| Compositing | `cv2.resize(..., INTER_NEAREST)` into `template[y:y+h, x:x+w]` (`:273`) |
| Caption | `caption += f"@{chosen_member} "` per filled rectangle — trailing space included (`:274`) |
| Output | `cv2.imwrite("meme.png", ...)` in the working directory, then `send_photo` as a reply (`:275-277`) |
| Persistence | None |
| External calls | one `telegram.me` HTML fetch per candidate |
| Known defects | D-ME-1 … D-ME-4 below |

## Defects — verdict per item

| id | Defect | Verdict |
|---|---|---|
| D-ME-1 | **An empty template pool is a `NameError`**, not a message: `contours_green` is read outside the `if` that assigns it (`:244-248`). The update dies in v1's global handler and the group hears nothing. | **fix** — `meme_templates.choose` returns `None` and the job answers `meme_error`, v1's own "I couldn't build this" string. No new string invented. |
| D-ME-2 | **`meme.png` is a fixed filename in the process working directory** (`:275`), shared by every concurrent request across a 50-thread pool — FEATURE-MAP D4. | **fix** — the composite never touches disk; the PNG goes straight to `BufferedInputFile`. |
| D-ME-3 | **The roster fallback is dead code.** The second loop picks `member['user']` — a *dict* — and hands it to `get_profile_image(username)`, which interpolates it into a URL (`:262-267`). That never resolves, so in practice v1 only ever fills rectangles from explicit `@` tags, and a bare `/meme` in a group can only produce `meme_error`. | **fix** — a roster entry here carries a real `user_id`, so the fallback works. **Accepted drift**: a bare `/meme` now produces a meme where v1 produced an error. That is what the code was written to do; preserving the defect would mean shipping a command with one working shape out of two. |
| D-ME-4 | **The `telegram.me` scrape** — an undocumented HTML dependency, one request per candidate, no timeout. Same mechanism `fun_battle` (D-BT-2) and `x_giveaways` already replaced. | **fix** — `get_user_profile_photos` + `bot.download`, through the authenticated session. |
| — | The whole thing ran on the reply path: a 110 MB template pool read, N HTTP fetches and an OpenCV composite. | **fix** — AGENTS.md §2.4; `scripts/spec.py`'s own row already said "image compositing is a worker job, not a reply-path call". The gate and the >5 refusal stay on the reply path, where v1 also makes them first. |

## Where the templates live, and why

110 MB across 801 files does not go in a wheel — `fun_meme.mdx` already
recorded that sizing as this feature's own decision, contrasting it with
`fun_complaint`'s 3.4 MB of package data. The split this port makes:

| Artefact | Size | Where | Why |
|---|---|---|---|
| `meme_metadata.csv` | 97 kB | package data, `cb_core/asset_data/meme/` | it *is* the catalog; every selection rule reads it, and it must be present for the code to mean anything |
| the 801 templates | 110 MB | `cb_core.storage.store()`, key `meme/templates/<Language>/<filename>` | bot-owned and global — no `group_id`, so `store()` rather than `media()`, exactly the reasoning `platform_bucket_export` gives |

Seeding is `python scripts/cb.py meme-seed` (`cb_worker/meme_seed.py`):
idempotent by key, `--dry-run`, `--force`, `--verify`. Separate from
`bucket-export` because the source is different in kind — these files are
checked into the v1 repo, so this is a directory copy with no credential
involved, while that tool reads a private GCS bucket through a read-only-scoped
credential. A deployment that has not seeded gets `meme_error` rather than
silence, and the log line names the missing key.

## Preserved deliberately

- **`>5` refused before anything else**, with `meme_no`.
- **The tag parser**, imported from `fun_battle` rather than re-transcribed —
  including v1's trailing-text and `endswith('bot')` quirks, which that
  module's docstring documents in full.
- **Spanish groups get English templates** (`'pt' in lang.lower()`).
- **One-directional language fallback**: Portuguese may fall back to English,
  never the reverse — the caption text is part of the image.
- **`NEAREST` resampling**, v1's `INTER_NEAREST`. The crunchiness is the look.
- **The caption's trailing space.**
- **`upload_photo` chat action before the work starts.**

## Phase 6 — parity

| Behaviour | v1 | v2 | Same? |
|---|---|---|---|
| Trigger and gate | `/meme`, `functionsFun` | same | ✅ |
| More than five tags | `meme_no` | same | ✅ |
| Template selection rules | widen upward, pt→en fallback | same | ✅ |
| Empty pool | `NameError`, silence | `meme_error` | ⚠️ D-ME-1 |
| Tagged members fill first | yes | yes | ✅ |
| Roster fills the rest | intended, dead in practice | works | ⚠️ D-ME-3 |
| No usable picture | `meme_error` | same | ✅ |
| Caption | `@name ` per face | same | ✅ |
| Reply | photo, replying to the command | same, from the worker | ⚠️ timing |
| Picture source | `telegram.me` scrape | Bot API | ⚠️ D-ME-4, same output |
| Template source | 110 MB next to the code | object storage | ⚠️ by design |

## Tests

| Layer | File |
|---|---|
| Unit (catalog) | `packages/cb-core/tests/test_meme_templates.py` — selection rules against v1's own CSV, both language behaviours, and the empty-pool case v1 crashes on |
| Unit (job + seeder) | `packages/cb-worker/tests/test_meme_job.py` — faces land in their rectangles, the caption, the three dead ends, and seed/re-seed/dry-run/verify |
| Unit (trigger) | `packages/cb-gateway/tests/test_meme.py` |
| Acceptance | `qa/features/fun_meme.feature` + `qa/test_fun_meme.py` — four scenarios, authored |
