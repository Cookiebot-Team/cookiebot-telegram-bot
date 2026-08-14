# x_drawing_idea — Design

One handler, one pure function, no database. The only decision worth writing
down is why the draw is not `legacy_assets.choose`.

## Module placement

| Piece | Where | Reuses |
|---|---|---|
| Handler | `packages/cb-gateway/src/cb_gateway/handlers/drawing_idea.py` (new) | `cb_core.legacy_assets`, `cb_core.storage`, `deny_if_disabled("utility")` |
| Aliases | `cb_core/textmatch.py:COMMAND_ALIASES` | three spellings → `drawingidea` |
| Router registration | `handlers/__init__.py` | one line, next to `/youtube` in the same gated stretch |

## R1 — the indexed draw

**R1.1** `pick_reference(entries, rng) -> tuple[int, LegacyAsset] | None`
returns the index *and* the row. `legacy_assets.choose` cannot be used: it
returns a row and forgets its position, and the position is what the caption
prints (`spec.md`, "The id is a position").

**R1.2** `random.randint(0, len - 1)`, inclusive at both ends, so the last
reference is drawable — a slip to `randrange` semantics would silently make one
image unreachable forever.

**R1.3** `None` on an empty pool (D-DI-1); the handler logs
`drawing_idea.pool_empty` and sends nothing.

## R2 — gate and ordering

**R2.1** `deny_if_disabled(message, ctx, "utility")` first — v1 answers
`utility_off` rather than staying silent (`COOKIEBOT.py:253`).

**R2.2** `send_chat_action("upload_photo")` before the pool read, matching
v1's order (`:138`), so an empty pool looks like the moment before v1 crashed.

## Open decisions — answered

1. **The pool order is the contract.** Ids stay meaningful only because the
   catalog is sorted by `source_path`; a unit test pins that the shipped
   catalog is sorted, so a future generator change cannot silently renumber
   3,435 references.
