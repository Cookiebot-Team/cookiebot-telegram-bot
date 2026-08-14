# x_drawing_idea — Specify

**Feature id:** `x_drawing_idea` · **Milestone:** M3 · **Kind:** v1 port
**v1 source:** `Bot/Miscellaneous.py:137-143` (`drawing_idea`), dispatched
`Bot/COOKIEBOT.py:248,253,256-257`.

## Goal

`/ideiadesenho`, `/drawingidea`, `/ideadibujo` post a random reference photo
for someone to draw from, captioned with the reference's id and a "don't trace
without credits" note.

## Phase 2 — v1 behaviour contract

| Aspect | v1 behaviour (file:line) |
|---|---|
| Triggers | `/ideiadesenho`, `/drawingidea`, `/ideadibujo` (`COOKIEBOT.py:256`) |
| Preconditions | `utilityfunctions` gate — off replies `utility_off` (`COOKIEBOT.py:253`), the same block `/dado` and `/youtube` sit in. **Not** the fun gate |
| Cooldowns / quotas | None — no entry in `Cooldowns.py` |
| Success output | ① `sendChatAction upload_photo` (`:138`) ② one photo drawn from `bloblist_ideiadesenho` by `random.randint(0, len-1)` (`:139-140`) ③ captioned `drawing_idea` with `idea_id` = **the index drawn** (`:142`) ④ sent as a reply to the trigger (`:143`) |
| Failure output | None. An empty pool raises `ValueError` inside `randint(0, -1)` and the dispatcher's bare `except` drops the update |
| Persistence | None |
| External calls | GCS signed-URL read (`IdeiaDesenho` prefix, 3,435 objects / 789 MB) |

## The id is a position, not an identity

`idea_id` is the index `random.randint` drew, printed into the caption
("Reference ID 2814"). Nothing stores it, nothing looks one up — there is no
`/ideiadesenho 2814` in v1. Its only use is a person quoting the number, which
works exactly as long as the pool's order is stable; v1's was only as stable as
a GCS listing, and any deleted blob shifted every id after it.

**Preserved as a position.** The v2 pool is
`legacy_assets.entries_for("IdeiaDesenho")`, sorted by `source_path` — the same
lexicographic order `list_blobs` returns — so previously quoted numbers land on
the same images. That is as strong a guarantee as v1 ever offered, and the
contract says so rather than implying more.

## Defects — verdict per item

| id | Defect | Verdict |
|---|---|---|
| D-DI-1 | Empty pool crashes: `random.randint(0, len(pool)-1)` on an empty listing raises `ValueError`, propagating to the dispatcher's bare `except` and silently dropping the update. | **fix** — reachable in v2 in a way it was not in v1 (a deployment where `legacy-catalog` has not run), and the same shape `fun_death`'s D-DE-3 already fixed: log and send nothing |

## QA

No upstream `Cookiebot-QA` scenario exists — confirmed against the full listing
of `../Cookiebot-QA/features/`. `qa/features/x_drawing_idea.feature` is
authored locally against v1's behaviour: the three triggers, the utility gate
and the empty pool.
