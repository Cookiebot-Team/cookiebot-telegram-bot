# Contract: core_botskins (v1 -> v2)

Phase 2/6 of `/migrate-feature` for v1's bot personas. QA:
`../Cookiebot-QA/features/core_botskins.feature` — three scenarios, **none of
which had been ported** before this slice (`.specs/features/core_botskins/spec.md`
recorded "0 of 3"). FEATURE-MAP row: `core_botskins`.

Files owned by this slice:
`packages/cb-core/src/cb_core/skins.py` (new),
`packages/cb-core/src/cb_core/tenancy.py` (`TenantRegistry.cached`),
`packages/cb-core/src/cb_core/asset_data/doomlist/` (v1's asset, verbatim),
`packages/cb-core/src/cb_core/asset_data/skins/` (the override tree),
`packages/cb-core/pyproject.toml` (package data),
`packages/cb-api/migrations/versions/0007_remaining_skins.py` (new),
`packages/cb-gateway/src/cb_gateway/handlers/setlang.py` (the intro animation),
`packages/cb-gateway/src/cb_gateway/handlers/doomlist.py` (the join flair),
`qa/conftest.py` (`feed(..., skin=)`), and the tests below.

## Phase 1 — where v1 lives

- Persona selection: `get_bot_token`, `universal_funcs.py:39-52` — a five-way
  `match` on an `is_alternate_bot` int taken from `sys.argv`
  (`COOKIEBOT.py:24-32`). One OS process per persona.
- Behavioural forks: `COOKIEBOT.py:130`, `:143`, `:333`, `:459`.
- Asset: `Bot/Static/silence_scammer.jpg`.
- Locale string: `caption` (the intro animation's caption) — already ported.

## Phase 2 — v1 behaviour contract

| Aspect | v1 behaviour (file:line) |
|---|---|
| Personas | 5: `cookiebot`, `bombot`, `pawstralbot`, `tarinbot`, `connectbot` (`universal_funcs.py:22,39-52`) |
| Selection | a CLI integer, one process each (`COOKIEBOT.py:24-32`, `LAUNCHER.py`) |
| Intro animation | posted when the bot is added to a group, **only if `not is_alternate_bot`** (`:130-132`) — a Dribbble CDN GIF, captioned with `caption` |
| Join-time flair | after any ban check fires, `if (funfunctions or is_alternate_bot) and random.randint(1,10) == 1` ⇒ `Static/silence_scammer.jpg` (`:143-145`) |
| Daily birthday sweep | only the primary process (`:333`) |
| Scheduler + API server | only the primary process (`:459-464`) |
| Anything else | identical across personas — the skin is a token, a process and those four branches |

## What was already done, and what this slice adds

`cb_gateway.bots.BotRegistry` had already replaced the process half: one
process serves every skin, so there are no divergent per-process caches and no
per-skin deploy. What was missing — and what
`.specs/features/core_botskins/spec.md` named exactly — is that "the 'skin' is
currently only a token and a display name, not an experience".

| Gap (from that spec) | Closed by |
|---|---|
| 0 of 3 QA scenarios ported | `qa/features/core_botskins.feature` + `qa/test_core_botskins.py`, plus two scenarios for the flagship-only behaviour an event-skin scenario cannot show |
| No per-skin asset pack exists | `cb_core/skins.py:asset` — `asset_data/skins/<skin>/<parts>` overrides `asset_data/<parts>`, falling back per file. A brand ships only what it rebrands. |
| No handler-level differentiation between skins | `posts_intro_animation` and `scammer_photo_allowed`, wired into `setlang.on_bot_added_to_group` and `doomlist.on_join` |
| `funfunctions or is_alternate_bot` has no v2 equivalent | `skins.scammer_photo_allowed` |
| 3 of 5 personas not configured | migration `0007` |

## The two forks that survive into v2, and the two that do not

`:333` (only the primary process ran the daily birthday sweep) and `:459`
(only the primary ran the scheduler and the API server) are artefacts of v1's
process model. In v2 scheduling is `cb-worker`'s and the HTTP surface is
`cb-api`'s, neither of which is per-skin — so neither is ported as a skin
behaviour. (`:333` is separately load-bearing for `util_birthday`: it is the
call site of the daily broadcast, and it is documented there.)

`:130` and `:143` are real, user-visible and per-skin, and are what
`cb_core/skins.py` exists for.

## Preserved deliberately

- **The intro animation is a URL**, v1's own Dribbble CDN link, verbatim — no
  asset ships with it and nothing re-uploads it.
- **The flair's odds** (`randint(1, 10) == 1`) and its position: after the ban
  and after the notice, best-effort, never able to fail a block.
- **v1's persona ids**, from its env-var names.

## QA vs v1: "Pawsy"

QA calls the Pawstral skin **Pawsy**; v1's code calls the token
`pawstralbot`. Both are kept — migration `0007` makes the id v1's and the
display name QA's — rather than choosing one, the same treatment
`docs/site/content/docs/feature-map.mdx` gives the other trigger mismatches.

## Phase 6 — parity

| Behaviour | v1 | v2 | Same? |
|---|---|---|---|
| Five personas | five processes | five tenant rows, one process | ⚠️ by design |
| Flagship posts the intro animation | yes | yes | ✅ |
| Event skin joins quietly | yes | yes | ✅ |
| Flair gated on `fun or alternate` | yes | yes | ✅ |
| Flair odds | 1 in 10 | 1 in 10 | ✅ |
| Per-skin assets | none (one `Static/`) | override tree with fallback | ➕ new mechanism |
| Daily sweep / scheduler forks | primary process only | not a skin concern | ⚠️ by design |

## Still open

- **No skin has supplied artwork.** `asset_data/skins/` is empty on purpose:
  this slice owed the mechanism, not the content. Adding a brand is a
  directory, not a code change.
- **`Tenant.handler_pack` is read now** — `cb_gateway/packs.py`, shipped with
  `x_custom_commands`. A pack names the command *families* a tenant receives,
  and `legacy_custom` (v1's `Custom/` picture pools) is the first; a brand that
  wants none of them sets `handler_pack = 'minimal'`. This entry used to read
  "still never read", which was `platform_tenancy`'s last open item. What is
  still missing is a family whose handlers are genuinely bespoke to one event
  skin — the mechanism exists, the content does not, same as the artwork above.

## Tests

| Layer | File |
|---|---|
| Unit | `packages/cb-core/tests/test_skins.py` — both forks, the asset fallback and override, and the byte-identity of v1's flair image |
| Unit | `packages/cb-gateway/tests/test_doomlist.py` — the flair fires only when allowed |
| Acceptance | `qa/features/core_botskins.feature` + `qa/test_core_botskins.py` — the three QA scenarios, plus the two intro-animation ones |
