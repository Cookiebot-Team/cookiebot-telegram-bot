# core_botskins — Specify

**Feature id:** `core_botskins` · **Milestone:** M1 · **Kind:** state report
**Status:** `partial` — the process-consolidation half of this feature is
done; the event-branding half QA actually describes is not started.

This is not a build spec. It records what exists, what doesn't, and why.

## What is actually implemented today

- `BotRegistry` (`packages/cb-gateway/src/cb_gateway/bots.py`) — one process
  serves every configured skin, replacing v1's one-OS-process-per-persona
  model (`get_bot_token`/`is_alternate_bot`,
  `../COOKIEBOT-Telegram-Group-Bot/Bot/universal_funcs.py:39-52`,
  `../COOKIEBOT-Telegram-Group-Bot/Bot/COOKIEBOT.py:24-32`). This is the part
  of the feature that's actually finished: no more divergent per-process
  caches, no more per-skin deploy.
- 2 of v1's 5 personas configured end to end: `cookiebot` and `bombot`
  (`.env.example:56`, tenant rows in
  `packages/cb-api/migrations/versions/0003_tenants.py:44-49`).
- `tenancy.registry.by_skin` resolves a `Tenant` per skin
  (`cb_core/tenancy.py:98-104`), consulted today only to hide a
  tenant-disabled command from `/commands`' own listing
  (`cb_gateway/handlers/listcommand.py:65-116`).

See `.specs/features/platform_tenancy/spec.md` for the full state of the
tenancy infrastructure this feature sits on top of — everything below that
isn't process consolidation is really that spec's gap wearing this feature's
name.

## What is missing

- **The behaviour QA actually specifies has no v2 implementation at all.**
  `../Cookiebot-QA/features/core_botskins.feature` describes three scenarios —
  Bombot skinning the bot for BrasilFurFest, "Pawsy" for Pawstral, Tarinbot
  for SCFurs — each asserting the bot "should display the … skin and provide
  event-specific interactions." None of the three has been ported to
  `qa/features/`: 0 of 3. (Note: the QA spec calls the Pawstral skin "Pawsy";
  v1's code calls it `pawstralbot` — a naming mismatch worth carrying forward
  the way `docs/site/content/docs/feature-map.mdx` tracks the others.)
- **No per-skin asset pack exists.** `packages/cb-core/src/cb_core/asset_data/`
  has one directory, `complaint/` (for `fun_complaint`) — nothing for any
  skin's branding or event content.
- **No handler-level differentiation exists between skins.** `build_router()`
  (`cb_gateway/handlers/__init__.py:39-76`) is a single fixed router built
  once for every tenant; `Tenant.handler_pack` is never read (see
  `platform_tenancy`). Two bots running today (`cookiebot`, `bombot`) present
  byte-identical command sets and behaviour — the "skin" is currently only a
  token and a display name, not an experience.
- **3 of v1's 5 personas — `pawstralbot`, `tarinbot`, `connectbot` — are not
  configured at all**, same gap as `platform_tenancy`.
- **v1's `funfunctions or is_alternate_bot` behaviour** (`COOKIEBOT.py:143` —
  any alternate-skin bot has fun features on unconditionally, regardless of
  the group's own config) has no v2 equivalent.
- v1's event-specific *commands* — `/bff`, `/patas`, `/trex`, `/furcamp`,
  `/pawstral`, driven by `event_countdown` and neighbours
  (`../COOKIEBOT-Telegram-Group-Bot/Bot/Miscellaneous.py:261-323`) — are
  **out of this feature's scope**, deliberately: they're tracked separately
  as `fun_partneredcons` (`Status.PLANNED` in `scripts/spec.py`), which is
  its own port with its own QA scenario. `core_botskins` is about which bot
  answers and how it's branded, not about a specific convention's command
  set.

## Why it stopped there

The mechanism this feature needed — one process serving multiple tokens — is
built and is genuinely the harder infrastructure problem v1's five-process
model created. What's left is the same unstarted layer `platform_tenancy`
documents: handler packs and asset packs are designed in
`docs/site/content/docs/multi-tenant.mdx` but not built, because nothing has
needed a *bespoke* skin yet — `cookiebot` and `bombot` today are
indistinguishable in behaviour, so there's been no forcing function. This
reads as "nobody has got to it" more than "blocked": the QA scenarios that
would define done exist and were simply never picked up, same bucket as
`fun_meme` or `util_youtube`.

## What it would take to finish, and what blocks it

Nothing here is externally blocked:

1. Port the 3 QA scenarios to `qa/features/core_botskins.feature` first —
   they're the actual acceptance bar and nobody has looked at them yet.
2. Build one real handler pack (`platform_tenancy`'s open item) using a real
   convention as the test case — Bombot/BrasilFurFest is the most concretely
   specified of the three.
3. Vendor a per-skin asset pack the way `fun_complaint` vendored
   `Bot/Static/reclamacao/`.
4. Configure the 3 missing personas if they're still wanted — a product
   question.

## v1 equivalent

`get_bot_token()` / `is_alternate_bot` dispatch,
`../COOKIEBOT-Telegram-Group-Bot/Bot/universal_funcs.py:39-52`; process
selection at `../COOKIEBOT-Telegram-Group-Bot/Bot/COOKIEBOT.py:24-32,143`;
event-specific commands (out of scope here) at
`../COOKIEBOT-Telegram-Group-Bot/Bot/Miscellaneous.py:261-323`.
