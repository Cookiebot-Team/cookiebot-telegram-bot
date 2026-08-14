# x_custom_commands — Design

`spec.md` stopped at "BLOCKED — there is not even a trigger list". The export
produced 53 `Custom/<name>/` folders, so there is one now, and it is *data*.
Everything below follows from that.

## Module placement

| Piece | Where | Reuses |
|---|---|---|
| Filter | `cb_gateway/filters.py` — `CustomCommandName` | `parse_command`'s head-extraction rules, `legacy_assets.entries_for_custom` |
| Pack registry | `cb_gateway/packs.py` (new) | `cb_core.tenancy.registry` |
| Handler | `cb_gateway/handlers/custom_command.py` (new) | `cb_core.storage`, `deny_if_disabled("fun")` |
| Router registration | `handlers/__init__.py` | **last** among command routers |

## R1 — the trigger list is data

**R1.1** The names are `legacy_assets.custom_command_names()`, not
`COMMAND_ALIASES` entries. An alias table is a static map from spelling to
canonical name; these are folder names that arrive with the assets. Putting
them in the table would also mean regenerating a Cython-compiled module's
constant from package data at import.

**R1.2** `CustomCommandName` extracts the head with `parse_command`'s rules
(`textmatch.py:161-181`), **not** v1's
`text.replace('/', '').replace('@CookieMWbot', '').split()[0]`. v1's chain
strips every slash anywhere in the word and knows two of its own five bot
usernames, so `/louie@SCTarinBot` never resolved to a folder; nothing a group
could depend on changes, and `@`-addressed commands start working for every
skin.

**R1.3** Registered last among the command routers, so a folder named after a
real command can never shadow it. A unit test asserts the shipped catalog has
no such collision.

**R1.4** Consequence, recorded rather than worked around: these do not produce
a `ParsedCommand`, so `TenantCommandGateMiddleware` never sees them and
`disabled_commands` does not apply. R3 is the per-tenant control instead.

## R2 — selection

**R2.1** `parse_index(args)` — v1's `.isdigit()` on the second whitespace
token only (`:148-149`), so `-1` and `1.5` fall through to the random draw and
non-ASCII decimal digits do not.

**R2.2** Out of range: send nothing, log `custom_command.index_out_of_range`.
v1 raised `IndexError` and the group saw nothing (D-CC-1); this keeps the
outcome and drops the traceback. Clamping was rejected — it would send one
picture while the caption named another id.

**R2.3** `display_name` is `.capitalize()`, v1's own (`:153`), which
lower-cases the rest of the name.

## R3 — handler packs (`platform_tenancy`'s last gap)

**R3.1** `cb_gateway/packs.py` maps a pack name to the set of *families* it
provides. `legacy_custom` is the first family; `"core"` provides it (v1 parity),
`"minimal"` provides nothing, an unknown name falls back to core with a log.

**R3.2** `PackProvides(family)` is a router filter reading `skin` out of the
update context and resolving the tenant through the already-cached registry.
Fails open: `registry.by_skin` never raises, and `FALLBACK.handler_pack` is
`"core"`.

**R3.3** Deviation from `multi-tenant.mdx`'s original "one dispatcher per pack"
sketch, and the reason, are in `packs.py`'s module docstring; the page is
updated to describe what shipped.

## Open decisions — answered

1. **Out-of-range index sends nothing** rather than clamping or inventing a
   locale string this feature never had.
2. **The fun gate replies `fun_off`** rather than reproducing v1's fall-through
   into the image-search catch-all. That fall-through is `x_image_search`'s to
   own, and both contracts record it.
3. **`"core"` provides the family**, so the Cookiebot brand keeps working with
   no tenant-row change; opting out is one field.
