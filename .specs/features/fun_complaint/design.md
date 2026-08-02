# fun_complaint — Design

Reads with `spec.md`. Requirement ids are back-referenced from `tasks.md`.

## R1 — Static assets

v1 opens files from a relative `Static/reclamacao/` path at send time. v2 needs
the same bytes reachable from an installed wheel.

- **R1.1** Copy `../COOKIEBOT-Telegram-Group-Bot/Bot/Static/reclamacao/*` into
  `packages/cb-core/src/cb_core/asset_data/complaint/` **byte-identical**
  (`diff -r` must be clean, exactly as `locale_data` was ported). Ten files:
  `milton_pt.jpg`, `milton_eng.jpg`, `hold{1,2,3,4,5,6,7,9}.wav`.
- **R1.2** New `packages/cb-core/src/cb_core/assets.py`:
  `def path(*parts: str) -> Path` resolving through `importlib.resources.files`,
  and `def pool(*parts: str, suffix: str) -> tuple[Path, ...]` returning a
  **sorted** tuple so the random choice is reproducible under a seeded rng. This
  is the only asset accessor; do not add a second one when `fun_death` and
  `fun_meme` land — they reuse it.
- **R1.3** These are bot-owned static assets, not user-supplied media, so they
  do **not** go through `cb_core.storage` (AGENTS.md §5 governs user content).
  Say so in the contract, because the next porter will ask.
- **R1.4** Whatever packaging config `cb_core/locale_data` uses to ship non-`.py`
  files in the wheel must cover `asset_data` too — extend the existing
  declaration in `pyproject.toml`, do not invent a second mechanism.

## R2 — Handler placement

- **R2.1** New `packages/cb-gateway/src/cb_gateway/handlers/complaint.py`
  exporting one `router` carrying **both** entry points.
- **R2.2** Registered in `handlers/__init__.py` in the disjoint-commands block.
  The reply-capture handler filters narrowly enough (photo caption containing a
  signature) that ordering against the other command routers is irrelevant; it
  must still raise `SkipHandler` — never `return` — on any "not mine" path if it
  is registered anywhere that another handler could need the update.
- **R2.3** Entry 1 filter: `CommandName("complaint")`. Check
  `cb_core/textmatch.py:COMMAND_ALIASES` first — if `milton`, `reclamacao`,
  `reclamação`, `queja` are not all mapped to `complaint`, add the missing ones
  **next to** the existing entries, never instead of them (AGENTS.md §2.1).
- **R2.4** Entry 2 filter: a stateless predicate `_is_milton_reply(message)`,
  modelled on `rules.py:_is_new_rules_reply` / `welcome.py:_is_welcome_reply`,
  with the one structural difference that it reads
  `message.reply_to_message.caption` (a photo caption, not `.text`) and uses
  substring containment over both signatures, not equality:
  `any(sig in caption for sig in MILTON_SIGNATURES)`. Also require that the
  incoming message is not itself a command, mirroring v1's dispatcher order
  (`COOKIEBOT.py:186`).
- **R2.5** `MILTON_SIGNATURES = ("Milton do RH.", "Milton from HR.")` is a module
  constant with a comment saying it is the tail of the `complaint` locale string
  and that editing either locale value breaks entry 2.

## R3 — The hold, without blocking the reply path

D-CP-4. Precedent: `groupguardian.py:504-507` runs the captcha's 30 s unban as an
in-process `asyncio.create_task` and documents that a gateway restart loses it,
because gateway→worker enqueue wiring does not exist.

- **R3.1** Entry 2 does, synchronously: delete the replied-to photo, send the
  chat action, send the voice note with the protocol caption. Then it schedules
  the tail — `sleep(delay)` → delete the voice note → send the answer line — with
  `asyncio.create_task`, and returns.
- **R3.2** Keep a module-level set of the scheduled tasks so they are not garbage
  collected mid-flight, exactly as `groupguardian` does. Copy that idiom; do not
  write a new one.
- **R3.3** Comment the restart caveat and point at the same gateway→worker gap
  (HANDOFF §1 gap 5). When that wiring lands — `util_everyone` is expected to
  build it — this tail is a candidate to move, but **not in this port**.
- **R3.4** The delay is `rng.randint(10, 20)` seconds and must be injectable so
  the acceptance test does not sleep for real: the tail coroutine takes the delay
  as an argument, and the sleep function is a module attribute the test can
  monkeypatch. No `time.sleep` anywhere.

## R4 — Output fidelity

- **R4.1** Photo choice is `"pt" if ctx.lang == "pt" else "eng"` — an equality
  check on the resolved language, not a locale lookup (D-CP-2).
- **R4.2** Caption is `t(ctx, "complaint", user=<sender first_name>)`. Use the
  sender's `first_name` exactly as Telegram gives it, unescaped, as v1 does.
- **R4.3** Protocol: `f"{rng.randint(10, 99)}-{rng.randint(100000, 999999)}/{year}"`
  where `year` comes from the current UTC year. Caption is `f"Protocol: {protocol}"`
  — never localised.
- **R4.4** The voice note is sent with `send_voice` (a voice note, not an audio
  file), replying to the user's message. The hold file is
  `rng.choice(assets.pool("complaint", suffix=".wav"))`.
- **R4.5** The answer is `rng.choice(locales.lines("answers", ctx.lang))`, sent as
  a reply to the user's message — the same idiom as `ship.py:130`.
- **R4.6** Both deletions are wrapped in `contextlib.suppress(Exception)`,
  matching v1's swallowing `delete_message` (`universal_funcs.py:340-344`) and
  the existing ports in `rules.py`/`welcome.py`.
- **R4.7** Fun gate on both entry points, replying with `t(ctx, "fun_off")`, plus
  `mark_outcome` on the refused path — same idiom as `ship.py`.

## R5 — Tests

- **R5.1** Unit — `packages/cb-gateway/tests/test_complaint.py`: every command
  alias resolves; `_is_milton_reply` accepts a caption containing either
  signature (including embedded mid-caption and with surrounding text), rejects a
  caption with neither, rejects a reply whose `reply_to_message` has `text` but
  no `caption`, and rejects a message that is itself a command; the protocol
  string matches `^\d{2}-\d{6}/\d{4}$` over a seeded rng; `assets.pool` returns
  exactly the 8 `.wav` files and is sorted.
- **R5.2** Asset parity — a unit test asserting the copied directory is
  byte-identical to the v1 one **when the v1 checkout is present**, skipping
  cleanly otherwise (the reference repos are not available in CI). Same shape as
  whatever guards the `locale_data` diff today; reuse it rather than adding a
  second skip idiom.
- **R5.3** Integration — none. The feature writes no row (spec: Persistence
  none). State that in the contract rather than adding an empty test.
- **R5.4** Acceptance — `qa/features/fun_complaint.feature` copied wording-intact
  plus scenarios for the fun-off gate and for a reply to a non-Milton caption
  doing nothing. Monkeypatch the delay to 0 per R3.4 and await the scheduled
  task before asserting the tail.

## R6 — Docs

- **R6.1** `docs/contracts/fun_complaint.md` — Phase-2 table, Phase-6 parity
  table, the five D-CP verdicts, R1.3 (why not `cb_core.storage`) and R3.3 (the
  restart caveat).
- **R6.2** The three QA/v1 conflicts from `spec.md` go into
  `docs/site/content/docs/feature-map.mdx`.
- **R6.3** `scripts/spec.py` status → `done`, then `cb.py docs-sync`. The
  generated frontmatter lists `triggers: ["/complaint", "/milton", "/queja"]` —
  add the two missing spellings there via `scripts/spec.py`, not by hand.

## Open decisions — answered

1. **Where do the assets live?** In the `cb-core` wheel as package data (R1.1),
   not object storage. They are ~3.4 MB, versioned with the code, and needed on
   the reply path with no network round-trip.
2. **Fix the `es` gaps?** No (D-CP-1, D-CP-2). Preserve, record.
3. **Worker job for the hold?** No — in-process task with the `groupguardian`
   caveat (R3.3). Revisit when enqueue wiring exists.
4. **New table for the pending complaint?** No. v1 is stateless and the reply
   chain re-derives everything from the payload (R2.4).
