# Contract: x_drawing_idea (v1 -> v2)

Phase 2/6 of `/migrate-feature` for `/ideiadesenho`, `/drawingidea`,
`/ideadibujo`. No upstream QA scenario exists. FEATURE-MAP row:
`x_drawing_idea`. Spec/design: `.specs/features/x_drawing_idea/`. Files owned
by this port: `packages/cb-gateway/src/cb_gateway/handlers/drawing_idea.py`
(new), `packages/cb-core/src/cb_core/textmatch.py` (three aliases),
`packages/cb-gateway/src/cb_gateway/handlers/__init__.py` (one router line),
`packages/cb-gateway/tests/test_drawing_idea.py` (new),
`qa/features/x_drawing_idea.feature` (new), `qa/test_x_drawing_idea.py` (new),
this file.

## Phase 1 — where v1 lives

- Handler: `drawing_idea`, `Miscellaneous.py:137-143`.
- Dispatch: `COOKIEBOT.py:248,253,256-257` — the `utilityfunctions`-gated
  stretch, alongside `/dado` and `/youtube`.
- Pool: `bloblist_ideiadesenho`, `Miscellaneous.py:16` —
  `list_blobs(prefix="IdeiaDesenho")`, 3,435 objects / 789 MB, exported and
  catalogued.
- Locale string: `drawing_idea`, already ported byte-identical in all three
  languages.

## Phase 2 — v1 behaviour contract

| Aspect | v1 behaviour (file:line) |
|---|---|
| Triggers | `/ideiadesenho`, `/drawingidea`, `/ideadibujo` (`COOKIEBOT.py:256`) |
| Preconditions | `utilityfunctions` — off replies `utility_off` (`:253`), **not** the fun gate |
| Cooldowns / quotas | None |
| Success output | `sendChatAction upload_photo`, then one photo drawn by `random.randint(0, len(pool)-1)`, captioned `drawing_idea` with `idea_id` = the index drawn, sent as a reply (`:138-143`) |
| Failure output | None — an empty pool raises inside `randint` |
| Persistence | None |
| External calls | GCS signed-URL read |

## The caption's id

`idea_id` is the **index** into the pool, not a stored identifier: nothing
records it and no command looks one up. v2 keeps it a position, into a catalog
the generator sorts by `source_path` — the same order `list_blobs` returns — so
numbers a group has quoted before still resolve to the same pictures. That is
exactly as strong as v1's own guarantee (any deleted blob shifted every id
after it) and no stronger. A unit test pins that the shipped catalog is sorted,
so a future generator change cannot silently renumber 3,435 references.

## Phase 6 — parity table

| Aspect | Verdict |
|---|---|
| Triggers | **same** |
| `utilityfunctions` gate and its `utility_off` reply | **same** |
| Chat action before the pool read | **same** |
| Uniform draw over the whole pool, inclusive of the last row | **same** |
| Caption text and the id it prints | **same** |
| Reply-to-trigger | **same** |
| Image transport | **changed (mechanism only)** — bytes from `cb_core.storage` instead of a 15-minute GCS signed URL |
| Empty pool (D-DI-1) | **changed (fixed)** — logs `drawing_idea.pool_empty` and sends nothing, where v1 raised `ValueError` |

## Tests

| Layer | File |
|---|---|
| Unit — the indexed draw, inclusive bounds, empty pool, catalog ordering | `packages/cb-gateway/tests/test_drawing_idea.py` |
| Acceptance — three triggers, the utility gate, the empty pool | `qa/features/x_drawing_idea.feature`, `qa/test_x_drawing_idea.py` |

No integration-layer test: no persistence, no query.
