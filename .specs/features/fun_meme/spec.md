# fun_meme — Specify

**Feature id:** `fun_meme` · **Area:** fun · **Milestone:** M2 · **Kind:** v1
port with no QA scenario.

## Goal

`/meme` picks a meme template with green placeholder rectangles and pastes
members' profile pictures into them — the tagged ones first, then whoever else
the group has — and posts it with a caption naming everyone used.

## Source of truth

`../COOKIEBOT-Telegram-Group-Bot/Bot/SocialContent.py:224-277`, its catalog
loader at `:35-47`, and `Bot/Static/Meme/metadata.py` (which generates the
catalog offline). Full behaviour table: `docs/contracts/fun_meme.md` §Phase 2.

## The two questions this port had to answer

**1. Where do 110 MB of templates live?** `fun_meme.mdx` already recorded that
`Bot/Static/Meme/` is 112 MB — too large for the package-data pattern
`fun_complaint` used for its 3.4 MB — and left the sizing as this feature's own
decision. Answer: the **metadata** (97 kB of CSV, and the thing every selection
rule reads) ships as package data; the **images** go into
`cb_core.storage.store()` under `meme/templates/<Language>/<filename>`, put
there by a new `cb.py meme-seed`. Bot-owned and global means `store()`, not
`media()` — the same reasoning `platform_bucket_export` gives for v1's GCS
assets.

**2. Does the roster fallback work in v1?** No. The second selection loop hands
`member['user']`, a dict, to a function that interpolates a *username* into a
URL (`:262-267`), so it never resolves — in practice v1 only ever fills
rectangles from explicit `@` tags, and a bare `/meme` can only produce
`meme_error`. Fixing it is accepted drift, recorded as D-ME-3: preserving it
would mean shipping a command with one working shape out of two.

## Decisions

| # | Decision | Why |
|---|---|---|
| R1 | Metadata as package data, images in object storage, `cb.py meme-seed` to move them | See above. Idempotent by key, because a template's identity is the filename the CSV refers to it by. |
| R2 | `cb_core.meme_templates`, not `cb_worker` | The seeder and the job must agree on the storage key; a key computed twice is a key that eventually differs. |
| R3 | Compositing is Pillow, not OpenCV | `util_birthday`'s collage already established Pillow here, and the contour finding OpenCV was for happens offline in the CSV. No new dependency. |
| R4 | Profile pictures from the Bot API | `fun_battle` (D-BT-2) and `x_giveaways` already replaced the same `telegram.me` scrape. |
| R5 | Tag parsing imported from `fun_battle` | Both call v1's single `get_members_tagged`; two transcriptions would drift. |
| R6 | An empty pool and an unseeded store both answer `meme_error` | v1's own string. It crashes on the first and cannot reach the second. |

## Success criteria

1. Selection reproduces v1's widening loop, its one-directional pt→en
   fallback, and its `es`→English mapping, asserted against v1's own CSV.
2. Faces land in the declared rectangles; the caption matches v1's, trailing
   space included.
3. Every dead end answers `meme_error` rather than going silent.
4. `meme-seed` is idempotent, has a dry run, and can verify the destination.
5. `ruff`, `mypy` and `cb.py check` clean.
