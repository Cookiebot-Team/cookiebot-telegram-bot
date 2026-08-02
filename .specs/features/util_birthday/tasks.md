# util_birthday / util_nextbirthday — Tasks

## Status

| Task | Status | Notes |
|------|--------|-------|
| T1 — Shared query (`cb_core/birthdays.py`) | ✅ done | |
| T2 — Vendor `Confetti.png`/`No_Image_Available.jpg` | ✅ done | |
| T3 — Pillow + pure compositing | ✅ done | |
| T4 — Worker jobs (collage + deferred follow-up) | ✅ done | |
| T5 — Gateway handlers + registration | ✅ done | |
| T6 — Acceptance (QA + net-new) | ✅ done | |
| T-final — Close out | ✅ done | |

## T1 — Shared query

- **What:** `cb_core/birthdays.py` per design R2: `BirthdayPerson`,
  `members_with_birthday(group_id, month, day)`, `display_name(person)`.
- **Where:** `packages/cb-core/src/cb_core/birthdays.py` (new),
  `packages/cb-core/tests/test_birthdays.py` (new)
- **Gate:** `uv run pytest packages/cb-core/tests/test_birthdays.py -q`
- **Commit:** folded into T4

## T2 — Vendor assets

- **What:** Copy `Confetti.png` (155KB) and `No_Image_Available.jpg` (13KB)
  from `../COOKIEBOT-Telegram-Group-Bot/Bot/Static/` byte-identical, same
  discipline `fun_complaint`'s `T1` used.
- **Where:** `packages/cb-core/src/cb_core/asset_data/birthday/` (new)
- **Gate:** `diff` against the v1 source, byte-identical
- **Commit:** folded into T4

## T3 — Pillow + pure compositing

- **What:** Add `Pillow` to `packages/cb-worker/pyproject.toml`.
  `cb_worker/collage.py`: pure functions, no I/O — `resize_to_cell(image,
  size)`, `build_grid(images, cell_size)` (design R3.2/R3.3), `overlay_confetti(grid,
  confetti)` (R3.4). Takes/returns `PIL.Image.Image`, never touches disk or
  network — testable with in-memory generated images, no real photos needed.
- **Where:** `packages/cb-worker/pyproject.toml`,
  `packages/cb-worker/src/cb_worker/collage.py` (new),
  `packages/cb-worker/tests/test_collage.py` (new)
- **Gate:** `uv run pytest packages/cb-worker/tests/test_collage.py -q`
- **Commit:** folded into T4

## T4 — Worker jobs

- **What:** `cb_core/jobs.py`: `BIRTHDAY_COLLAGE`, `NEXT_BIRTHDAYS_FOLLOWUP`.
  `cb_worker/jobs/birthday.py`: `post_birthday_collage` (design R3: resolve
  birthday people + tagged extras, fetch/placeholder photos, composite via
  `collage.py`, send, pin best-effort, 🎂, enqueue the deferred follow-up)
  and `next_birthdays_followup` (design R5, calls the same text builder
  `nextbirthday.py`'s handler will use). Register both in
  `cb_worker/main.py`.
- **Where:** `packages/cb-core/src/cb_core/jobs.py`,
  `packages/cb-worker/src/cb_worker/jobs/birthday.py` (new),
  `packages/cb-worker/src/cb_worker/main.py`,
  `packages/cb-worker/tests/test_birthday_job.py` (new)
- **Depends on:** T1, T2, T3
- **Gate:** `uv run pytest packages/cb-worker/tests/test_birthday_job.py -q && uv run python scripts/cb.py types`
- **Commit:** `feat(util_birthday): the collage job, roster-sourced, no scrape`

## T5 — Gateway handlers

- **What:** `cb_gateway/handlers/birthday.py` — `fun` gate, bare-argument
  check (`bday.title`, no lookup, the QA/v1 conflict `spec.md` records),
  otherwise enqueue `BIRTHDAY_COLLAGE` with `group_id`, `message_id`,
  `extra_names` (parsed `@`-tokens), `lang`. `cb_gateway/handlers/nextbirthday.py`
  — `fun` gate, builds and replies the text directly (design R4, no worker).
  Register both in `handlers/__init__.py`.
- **Where:** `packages/cb-gateway/src/cb_gateway/handlers/{birthday,nextbirthday}.py` (new),
  `packages/cb-gateway/src/cb_gateway/handlers/__init__.py`,
  `packages/cb-gateway/tests/test_birthday.py`,
  `packages/cb-gateway/tests/test_nextbirthday.py` (new)
- **Depends on:** T1, T4
- **Gate:** `uv run pytest packages/cb-gateway/tests/test_birthday.py packages/cb-gateway/tests/test_nextbirthday.py -q && uv run python scripts/cb.py types`
- **Commit:** folded into T4's commit

## T6 — Acceptance

- **What:** Copy both QA feature files wording-unchanged. `util_birthday.feature`'s
  step for "the bot should reply with a montage..." asserts v1's real bare-`/birthday`
  behaviour (`bday.title`, no enqueue) per the recorded conflict; a net-new
  scenario (`/birthday @someone`) exercises the real collage path via a fake
  queue (`util_everyone`'s pattern). `util_nextbirthday.feature` exercises
  the real (non-worker) text reply directly.
- **Where:** `qa/features/util_birthday.feature`, `qa/test_util_birthday.py`,
  `qa/features/util_nextbirthday.feature`, `qa/test_util_nextbirthday.py` (all new)
- **Depends on:** T5
- **Gate:** `uv run pytest qa/test_util_birthday.py qa/test_util_nextbirthday.py -q`
- **Commit:** `test(util_birthday): QA scenarios, the recorded conflict, and nextbirthday`

## T-final — Close out

- **What:** `docs/contracts/util_birthday.md` (Phase 2/6, D-BD-1/2/3, the
  bare-argument QA conflict, **the unverified daily broadcast recorded as an
  open parity gap, not resolved** — `spec.md`'s exact language, not
  softened). `docs/contracts/util_nextbirthday.md`. `scripts/spec.py` →
  `partial` for both (the daily broadcast is a known, unverified gap — not
  `done`, matching `status.py`'s new bidirectional check: a `partial` needs
  this written reason, which the contract now provides). `cb.py docs-sync`.
  `.mdx` prose for both, prose stating the parity gap plainly. `HANDOFF.md`:
  the daily-broadcast gap recorded explicitly, not folded into a generic
  "done" line. `feature-map.mdx`: the bare-`/birthday` conflict.
- **Depends on:** T1-T6
- **Gate:** `uv run python scripts/cb.py check`
- **Commit:** `docs(util_birthday): close out, and the unverified daily broadcast`
