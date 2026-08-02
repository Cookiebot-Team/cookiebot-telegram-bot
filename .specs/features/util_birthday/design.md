# util_birthday / util_nextbirthday — Design

Scope, data source and library choices are all approved (`spec.md`'s
Status section). This document is the how.

## R1 — module placement

| Piece | Where | Reuses |
|---|---|---|
| Shared birthday query | `packages/cb-core/src/cb_core/birthdays.py` (new) | `cb_core.db`, same single-shard join shape `cb_core.members.roster` already established |
| Static assets (`Confetti.png`, `No_Image_Available.jpg`) | `packages/cb-core/src/cb_core/asset_data/birthday/` (new) | `cb_core.assets.path`/`pool` — `fun_complaint`'s precedent, no changes to `assets.py` itself |
| Collage compositing | `packages/cb-worker/src/cb_worker/collage.py` (new, pure — no I/O) | Pillow (new dependency, `cb-worker`'s `pyproject.toml`) |
| `/birthday` collage job | `packages/cb-worker/src/cb_worker/jobs/birthday.py` (new) | `cb_core.members.roster`, `cb_core.birthdays`, `collage.py`, `cb_core.assets` |
| Deferred next-birthdays job | same file, `next_birthdays_followup` | `cb_core.birthdays` |
| `/birthday` gateway handler | `packages/cb-gateway/src/cb_gateway/handlers/birthday.py` (new) | `ctx.enabled("fun")`, `cb_gateway.queue.enqueue` |
| `/nextbirthday` gateway handler | `packages/cb-gateway/src/cb_gateway/handlers/nextbirthday.py` (new) | `cb_core.birthdays`, no worker needed — see R4 |

No migration: `users.birthdate`/`birth_month`/`birth_day` and their index
already exist (`0001_initial_schema.py:79-95`). This port is read-only
against that data.

## R2 — the shared query (`cb_core/birthdays.py`)

**R2.1** `users` is a **reference table** (replicated to every node, not
distributed on `group_id`); `group_members` **is** distributed on `group_id`,
colocated with `groups`. Every read here puts `group_id` first in the
`WHERE` and joins to `users` only by its primary key — the same
single-shard, node-local shape `cb_core.members.roster` already established
and `qa/integration/test_everyone.py` already asserts `Task Count: 1` for.
No new Citus concern; this port's queries are structurally identical to
that one, just filtered by `birth_month`/`birth_day` instead of "everyone."

```python
@dataclass(frozen=True, slots=True)
class BirthdayPerson:
    user_id: int
    username: str | None
    first_name: str | None
    last_name: str | None


async def members_with_birthday(group_id: int, month: int, day: int) -> tuple[BirthdayPerson, ...]:
    """SELECT u.user_id, u.username, u.first_name, u.last_name
    FROM group_members gm JOIN users u ON u.user_id = gm.user_id
    WHERE gm.group_id = $1 AND gm.left_at IS NULL
      AND u.birth_month = $2 AND u.birth_day = $3
    ORDER BY u.user_id"""


def display_name(person: BirthdayPerson) -> str:
    """v1: f"@{username}" if present, else f"{firstName} {lastName}"
    (Birthdays.py:93,117 — both `make_birthday_caption` and `next_birthdays`
    use the identical fallback, ported once here instead of twice)."""
```

**R2.2** Both the collage job and `/nextbirthday` (handler and its own
deferred-job twin) call `members_with_birthday` — one function, three call
sites, matching v1's own reuse of the same backend query shape across
`birthday()` and `next_birthdays()`.

## R3 — the collage (`cb_worker/collage.py` + `jobs/birthday.py`)

**R3.1 — photo sourcing, `fun_battle`'s precedent, generalised to N people.**
For each `BirthdayPerson` (real birthday hits, already carrying `user_id`
from R2's join) and each manually `@`-tagged extra (resolved to a `user_id`
by a case-insensitive roster username match — `fun_battle`'s
`_find_in_roster`, copied not imported, same reasoning every other job file
in this codebase gives for not creating cross-feature coupling):
`bot.get_user_profile_photos(user_id, limit=1)` → `file_id` →
`bot.download(file_id)` → bytes. No `user_id` resolvable, no photos, or a
download failure ⇒ the vendored `No_Image_Available.jpg` bytes — v1's own
fallback (`Birthdays.py:64-69`), now a local asset read instead of a second
disk read of a relative path that assumes the process's cwd is `Bot/`.

**R3.2 — D-BD-3's fix.** Every photo (including the placeholder) is resized
to one fixed square cell (`256×256`, an arbitrary but generous size — large
enough that Telegram's own photo compression is the limiting factor, not
this resize) via `Image.resize` **before** compositing. This is what makes
the grid math safe: v1's crash-prone "size the canvas from image 0, place
each image at its own size" is replaced with "every cell is the same size,
size the canvas from the grid dimensions and the fixed cell size."

**R3.3 — the grid, v1's own math, made safe by R3.2.**
`width = ceil(sqrt(n))`, `height = ceil(n / width)` (`Birthdays.py:70-71`,
unchanged) — with every image now a uniform `256×256`, placement is
`(col * 256, row * 256)`, no per-image shape lookup needed at all.

**R3.4 — confetti overlay.** v1's OpenCV alpha-index-and-replace
(`confetti[:, :, -1] == 0` picks transparent pixels, backfills them with the
collage) is exactly what `PIL.Image.alpha_composite(canvas, confetti)` does
natively once both are `RGBA` and the same size (`confetti.resize(canvas.size)`)
— no manual pixel indexing needed; Pillow's own alpha compositing is the
direct equivalent, not an approximation.

**R3.5 — no local file, unlike v1.** v1 writes `birthday.png` to a
hardcoded, non-namespaced path (`Birthdays.py:79`, `cv2.imwrite("birthday.png", ...)`)
— the same shape of race `fun_battle`'s D-BT-1 was (two concurrent
`/birthday` calls in different groups clobbering the same file). Composited
bytes stay in memory (`io.BytesIO`) and go straight into a
`BufferedInputFile` for `sendPhoto` — there is no file to race over.

**R3.6 — caption, verbatim.** `t(ctx-equivalent, "bday.cta", names=...)`
(random line, `%(names)s` = display names joined `" e "`, v1's literal
separator regardless of language — preserved, a cosmetic quirk, not a bug)
`+ t(..., "bday.closing", date=...)`.

## R4 — `/nextbirthday` stays on the reply path

**R4.1** No image, no external API — four single-shard DB reads
(`members_with_birthday` for `today + 1..4`) and one text reply. Fast enough
for the gateway; no worker hop needed, matching v1's own synchronous nature
(`next_birthdays` in v1 never touches anything slow either).

**R4.2** The deferred follow-up (R5) calls the **same** text-building logic
from `cb-worker`, because it fires from a job, not a live update — the text
construction itself is pulled into `cb_core.birthdays` precisely so both
call sites share it rather than drifting.

## R5 — the deferred follow-up, replacing `threading.Timer`

**R5.1** After a successful collage post, `cb_worker/jobs/birthday.py`'s
collage job enqueues its own module's `next_birthdays_followup` job:
`enqueue(jobs.NEXT_BIRTHDAYS_FOLLOWUP, group_id=..., lang=..., _defer_by=900)`
— arq's native deferred execution (the `enqueue` wrapper already documents
passing arq's reserved kwargs through), durable across a restart because
the job sits in Redis, not process memory, unlike v1's `threading.Timer`
(D-BD-2). Same 900-second interval, same target behaviour (`next_birthdays`'
own text), different (fixed) mechanism.

## R6 — telemetry

**R6.1** `cb_worker_birthday_collage_total{outcome}` (`sent|no_photos_available`
— never "error" as a distinct label; a partial photo-fetch failure degrades
per-person to the placeholder, per R3.1, not to a whole-job failure).
`cb_worker_birthday_photo_total{outcome}` (`fetched|placeholder`) — one
counter per photo resolution, mirroring `everyone_dm_total`'s per-item
shape. No group/user id label (AGENTS.md §7).

## Open decisions — answered

1. **Cell size for the collage.** `256×256` — R3.2, a judgement call with no
   v1 precedent to match (v1 never resized at all), chosen for headroom
   against Telegram's own photo compression, not a value copied from
   anywhere.
2. **Where the shared query lives.** `cb_core/birthdays.py` — R2, needed by
   both a gateway handler and two worker jobs, same tier `cb_core.members`
   already occupies for an identical three-consumer shape.
3. **The daily broadcast is not built and is not resolved** — `spec.md`'s
   "unverified daily broadcast" section, carried into `docs/contracts/util_birthday.md`
   and `HANDOFF.md` verbatim at close-out, not summarised away.
