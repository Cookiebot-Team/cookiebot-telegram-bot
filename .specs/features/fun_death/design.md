# fun_death — Design

**Not executable yet.** See `spec.md`'s "The blocker" section — the image
pool does not exist anywhere this session (or this repo) can read. This
document exists so that once the prerequisite (someone exports v1's `Death/`
GCS prefix) lands, `tasks.md` can be executed mechanically without a second
research pass. `tasks.md` exists (grammar compliance) but every row is
`🚫 blocked` on the same prerequisite — do not start `T-final`.

## Module placement (once unblocked)

| Piece | Where | Reuses |
|---|---|---|
| Vendored images/gifs | `packages/cb-core/src/cb_core/asset_data/death/` | same package-data pattern `fun_complaint` already uses (`packages/cb-core/src/cb_core/asset_data/complaint/`) |
| Handler | `packages/cb-gateway/src/cb_gateway/handlers/death.py` (new) | `cb_core.assets.pool`/`path`, `cb_gateway.context.context_for`/`t`, `ctx.enabled("fun")` (same gate `ship.py`/`firecracker.py` already use) |
| Router registration | `packages/cb-gateway/src/cb_gateway/handlers/__init__.py` | one line, disjoint trigger — order-independent, same as `ship`/`firecracker`/`everyone` |
| Nested-catalog reader | local `_death_strings(lang)` in `death.py` | copies `groupguardian.py`'s `_captcha_strings` pattern (below) — no change to `cb_core/locales.py` |

No database table, no migration, no worker job — this is a single `sendPhoto`/
`sendAnimation` reply, same shape as `fun_complaint`'s entry 1. Everything
stays on the reply path.

## R1 — the nested-catalog gap

**R1.1** `cb_core.locales.get(key, lang)` only resolves *flat* keys —
confirmed by reading `_load_catalog` (`cb_core/locales.py:84-98`): it is
`dict(json.loads(...))` with no flattening step. `lib.json`'s `"death"` key
is a nested object (`template`/`variants`/`Reason`), the same shape
`"captcha"` already is for `core_groupguardian`. `groupguardian.py:108-125`
already solved this exact problem for its own nested key — a local
`_captcha_strings(lang) -> Mapping[str, str]` that reads `locales.catalog(lang)`
(cast to `dict[str, object]`, since the module's own declared
`Mapping[str, str]` type is a known simplification, not a hard type
contract other features are able to fix from outside `cb_core`), reaches into
`raw.get("captcha")`, and replicates `locales.get`'s en-fallback by hand.

**R1.2** `death.py` copies that pattern verbatim for its own `"death"` key:
a local `_death_strings(lang) -> Mapping[str, object]` (the value type isn't
uniformly `str` — `variants` is a `list[str]`), same cast-and-fallback shape.
This is not a `cb_core/locales.py` change — that file is out of this port's
file ownership, same reasoning `groupguardian.py`'s docstring already gives
("a real mismatch between the declared and actual shape, not something this
module can fix").

**R1.3** The `%`-substitution `locales.get` normally does
(`death.template` with `variant=...`, `death.Reason` with `line=...`) is done
by hand in `death.py` too: `strings["template"] % {"variant": ...}`,
`strings["Reason"] % {"line": ...}` — `locales.get`'s own try/except around a
malformed placeholder (content bug, not a crash) is worth copying alongside
the substitution itself, not just the lookup.

## R2 — asset pool, mixed extensions

**R2.1** v1's bucket mixes still images and `.gif`s under one prefix and
branches on `filename.endswith('.gif')` at send time
(`Miscellaneous.py:354-357`). `cb_core.assets.pool(*parts, suffix=...)` takes
one `suffix` per call (`assets.py:31-38`); the smallest change that needs no
edit to `assets.py` is calling it once per extension actually present in the
vendored directory and concatenating: `pool("death", suffix=".jpg") +
pool("death", suffix=".png") + pool("death", suffix=".gif")` (extend the
tuple with whatever extensions the real export turns out to contain — this
cannot be pinned down further until the files exist to inspect).

**R2.2** `random.choice` over the concatenated pool, then branch on
`.suffix == ".gif"` for `reply_animation` vs. `reply_photo` — same
conditional v1 has, reproduced exactly (D-DE-2, chat-action mismatch,
preserved).

**R2.3** If the vendored directory is ever empty (D-DE-3, spec.md), degrade
to a no-op with a `log.warning` rather than v1's uncaught `ValueError` — this
can only happen from a packaging mistake, not from anything a real bucket
export would produce, so it is a safety net, not a designed user-facing path.

## R3 — target resolution and caption

**R3.1** `ParsedCommand.args` (same field `fun_ship`'s `explicit_targets`
already reads, `ship.py:104-116`) gives the raw tail of the message; the
first whitespace token, if any, is the tagged target — no membership lookup,
matching v1's `msg['text'].split()[1]` exactly (spec.md's target-resolution
row, branch ①).

**R3.2** Branch ② (reply, first_name) and ③ (caller, username-or-first_name,
with D-DE-1's dropped-emoji quirk preserved exactly) read
`message.reply_to_message`/`message.from_user`, same fields every other
ported handler in this codebase already reads off an aiogram `Message`.

**R3.3** Caption assembly is a small pure function
(`build_caption(target: str, has_username: bool, template: str, reason: str) -> str`
or similar), unit-testable without a database or Telegram — same shape
`ping_chunks` (`everyone.py`) and `explicit_targets` (`ship.py`) already
established for this codebase's "extract the pure part, test it directly"
house style.

## R4 — telemetry

**R4.1** `mark_outcome("refused")` on the `fun_off` gate path, same as every
other `ctx.enabled("fun")`-gated handler. No other outcome label needed —
this feature has no failure branch a user can trigger (D-DE-3's empty-pool
guard is a packaging-bug safety net, not a reachable user path).

## Open decisions — deferred, not answered

These cannot be settled until the real export exists to look at:

1. **Exact file extensions in the pool.** R2.1 assumes `.jpg`/`.png`/`.gif`
   because that is what `fun_meme`'s and `fun_complaint`'s vendored trees
   already contain in this codebase; v1's `Death` prefix might turn out to
   be gif-only, or include `.jpeg`/`.webp`. Confirm against the real file
   listing when it arrives, adjust R2.1's tuple accordingly.
2. **Pool size / package weight.** `fun_complaint`'s vendored audio is
   ~3.2 MB total; there is no way to estimate `Death`'s footprint without
   listing the bucket. If it turns out to be large (v1's meme folder alone
   is hundreds of files), reconsider whether `cb_core.assets`'s "ship as
   package data" pattern still holds or whether this pool specifically
   should go through `cb_core.storage` instead, despite not being
   user-supplied — worth a second look once the size is known, not a
   default assumption either way.
