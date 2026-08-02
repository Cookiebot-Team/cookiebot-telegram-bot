# fun_battle — Tasks

Grammar: `tlc-spec-driven` §5. Read `spec.md` and `design.md` first — both
decisions (redesign accepted, path A ships now) are settled there.

## Status

| Task | Status | Notes |
|------|--------|-------|
| T1 — Failing unit tests for the pure logic | ✅ done | |
| T2 — Handler and router registration | ✅ done | |
| T3 — Acceptance scenarios | ✅ done | |
| T-final — Close out | ✅ done | |

## Tasks

### T1 — Failing unit tests for the pure logic

- **Skills:** /migrate-feature (Phase 4)
- **What:** Write tests before the handler exists, per design R1/R4:
  `parse_tagged_targets` against v1's exact split-on-`@` behaviour (including
  the untrimmed/multi-word capture quirk and the case-sensitive `.endswith('bot')`
  filter — `"@spambot"` dropped, `"@AdminBot"` kept); `_leading_token` turning
  a raw capture into a lookup-ready token; the `BattleShape` selector over
  tag-count and the `"random"` substring, case-insensitively, against the
  *whole* message text; `_catalog_choice`'s cast-and-en-fallback behaviour
  for `battle_type`/`battle_rule`/`battle_equip` (top-level catalog lists);
  the caption/choices asymmetry (no `@` for explicit tags, `@` for `"random"`
  picks) as a pure assembly function taking already-resolved display strings.
- **Where:** `packages/cb-gateway/tests/test_battle.py` (new)
- **Depends on:** none
- **Reuses:** `packages/cb-gateway/tests/test_ship.py`'s pattern for testing
  a pure target-parsing function without Telegram or a database;
  `groupguardian.py:108-125`'s `_captcha_strings` is the template
  `_catalog_choice` copies, so its own tests are the reference for what a
  cast-and-fallback test looks like.
- **Done when:** the parsing/shape/catalog/caption tests pass standalone;
  anything that needs the handler (roster resolution, photo fetch, the B/C
  temporary route) fails on import because `cb_gateway.handlers.battle`
  does not exist yet.
- **Gate:** `uv run pytest packages/cb-gateway/tests/test_battle.py -q`
  (expected: pure-logic tests pass, the rest error on import)
- **Commit:** `test(fun_battle): target parsing, shape selection, catalog reads`
- **→ R1, R4.1, R4.2 (design.md)**

### T2 — Handler and router registration

- **Skills:** /migrate-feature (Phase 5)
- **What:** Implement `battle` per design R2 (roster resolution — case-
  insensitive username match, `rng.sample` for `"random"`, ordered
  photo-extraction failure per side, D-BT-3's crash naturally absent), R3
  (ONE_TAG/SELF's temporary `battle_no_picture` reply — no roster lookup, no
  Bot API call, with the comment pointing at `fun_death`'s shared bucket
  prerequisite and naming what changes once it lands), R5 (telemetry — only
  the `fun_off` gate calls `mark_outcome`), R6 (🔥 reaction before any
  branching, `upload_photo` chat action). `ctx.enabled("fun")` gate exactly
  like `ship.py`/`firecracker.py`.
- **Where:** `packages/cb-gateway/src/cb_gateway/handlers/battle.py` (new),
  `packages/cb-gateway/src/cb_gateway/handlers/__init__.py` (one import + one
  `include_router`, disjoint-commands block, next to `ship`/`firecracker`)
- **Depends on:** T1
- **Reuses:** `cb_core.members.roster`, `ship.py`'s `ctx.enabled("fun")` +
  `mark_outcome` + reaction-suppression idiom, `firecracker.py`'s
  `rng: random.Random | None = None` convention for the two random draws
  (flavour text, `"random"`-pick sampling)
- **Done when:** T1 is fully green and the router is registered exactly
  once, in the disjoint-triggers block (order-independent).
- **Gate:** `uv run pytest packages/cb-gateway/tests/test_battle.py -q && uv run python scripts/cb.py types`
- **Commit:** `feat(fun_battle): two-person battles via roster + Bot API, no scrape`
- **→ R2, R3, R5, R6 (design.md)**

### T3 — Acceptance scenarios

- **Skills:** /migrate-feature (Phase 3 + top of Phase 4)
- **What:** Copy `../Cookiebot-QA/features/fun_battle.feature` into
  `qa/features/` wording unchanged (one scenario), then append net-new
  scenarios per `spec.md`'s QA section and the accepted scope cut: `"random"`
  with two eligible members; fewer than two eligible members ⇒ `battle_no`;
  a tagged user not in the roster ⇒ `battle_extract` naming that tag (the
  accepted behavioural drift); a resolved user with no profile photos ⇒
  `battle_extract`; a bare `/battle` (no tag, no "random") ⇒ `battle_no_picture`;
  fun functions disabled ⇒ `fun_off` and nothing else. Assert the media
  group + poll shape (two `InputMediaPhoto`, `is_anonymous=False`,
  `allows_multiple_answers=False`, both replying to the trigger) via the
  mock Telegram's recorded calls.
- **Where:** `qa/features/fun_battle.feature` (new), `qa/test_fun_battle.py` (new)
- **Depends on:** T2
- **Reuses:** `qa/test_fun_ship.py` for seeding a roster via
  `cb_core.members.record` and driving the real dispatcher against mock
  Telegram; `qa/conftest.py`'s `next_update_id()`
- **Done when:** every scenario passes and no existing acceptance test
  regressed.
- **Gate:** `uv run pytest qa/test_fun_battle.py -q`
- **Commit:** `test(fun_battle): QA scenario plus the net-new v1 paths`
- **→ QA section (spec.md)**

### T-final — Close out

- **Skills:** none
- **What:** Write `docs/contracts/fun_battle.md` — Phase-2 table (v1
  `file:line` intact), Phase-6 parity table, the three D-BT verdicts, the
  accepted behavioural drift (unresolvable tag ⇒ existing `battle_extract`,
  not a new message) stated explicitly, and the B/C temporary-route note
  (which string, why, what replaces it once `Fight/` lands). Record the two
  QA/v1 conflicts from `spec.md`'s QA section in
  `docs/site/content/docs/feature-map.mdx`. Flip `fun_battle` to
  `Status.PARTIAL` in `scripts/spec.py` (not `done` — B/C are still a stub),
  then `cb.py docs-sync`. Write real prose into
  `docs/site/content/docs/features/fun_battle.mdx`. Update `HANDOFF.md`:
  fold B/C into the existing `fun_death`/`Fight`-bucket gap (§1 item 7) rather
  than giving them a gap of their own, and update §4's row.
- **Where:** `docs/contracts/fun_battle.md` (new),
  `docs/site/content/docs/feature-map.mdx`, `scripts/spec.py`,
  `docs/site/content/docs/features/fun_battle.mdx`, `HANDOFF.md`, this file's
  Status table.
- **Depends on:** T3
- **Done when:** `uv run python scripts/cb.py check` exits 0.
- **Gate:** `uv run python scripts/cb.py check`
- **Commit:** `docs(fun_battle): close out`
