# Contract: core_mediarestrict (v1 -> v2)

Phase 2/6 of `/migrate-feature` for the new-member media restriction. QA:
`../Cookiebot-QA/features/core_mediarestrict.feature`. FEATURE-MAP row:
`core_mediarestrict`, status `✅`.

v1: `welcome_message` (`../COOKIEBOT-Telegram-Group-Bot/Bot/GroupShield.py:140-152`),
called from the join event (`COOKIEBOT.py:141,150`). FEATURE-MAP additionally
cites `COOKIEBOT.py:167-172` as "the trigger" — see the "What COOKIEBOT.py:167-172
actually is" section below; it is not a restriction check.

## Boundary with `core_welcome` (already drawn, respected here)

`docs/contracts/core_welcome.md` explicitly assigns the `limbotimespan` half of
`welcome_message` to this feature and states: "This belongs to
`core_mediarestrict`'s responsibility... This port's join handler: does **not**
call `restrictChatMember`, does **not** send `restrict_message`, does **not**
write to `group_members`." This port is the other half: `welcome.py` is not
touched, and this feature owns `restrictChatMember`'s replacement mechanism,
the `restrict_message` text, and all reads/writes of `group_members.joined_at`.

## Phase 2 — v1 behaviour contract

| Aspect | v1 behaviour (file:line) |
|---|---|
| Trigger | The join event, `GroupShield.py:140-152`, inside `welcome_message` — the same function `core_welcome` ports the messaging half of. No separate command or user action triggers this; it is purely a side effect of someone joining. |
| Precondition | `if limbotimespan > 0:` (`GroupShield.py:145`). `limbotimespan == 0` is v1's off switch: no `restrictChatMember` call, no message, ever — verified by reading the guard, not inferred. |
| Who gets restricted | Only `user = msg['new_chat_member'] if 'new_chat_member' in msg else msg['from']` (`GroupShield.py:144`) — the same deprecated singular field `core_welcome`'s join handler reads for messaging (`new_chat_members[0]`). In a batch join (several people added in one service message), only the first joiner is ever passed to `restrictChatMember`; the 2nd+ joiners are never natively restricted in v1, full stop — not a v2 regression, a pre-existing v1 defect this port preserves (consistent with `core_welcome`'s identical, explicitly-preserved quirk for the messaging half of the same function). |
| Admin exemption | None explicit in `welcome_message` itself — but a group admin does not go through the "new member joins" flow at all in the ordinary product sense (an admin is usually the group's own creator/staff, added once, long before this feature matters) and the task brief for this port requires an explicit admin bypass (`ctx.is_admin`) regardless, which this port adds as a deliberate v2 hardening: an admin who is *also* newly added to a group must never be muted. |
| Mechanism | `cookiebot.restrictChatMember(chat_id, user['id'], permissions={can_send_messages: True, can_send_media_messages: True, can_send_other_messages: True, can_add_web_page_previews: True})` immediately followed by a second call with the same three permissions set to `False` and `until_date=int(time.time() + limbotimespan)` (`GroupShield.py:147-148`) — i.e. a native Telegram mute, timed, applied once, at join. |
| Success output | `i18n.get("restrict_message", lang=language, time=round(limbotimespan/60))`, sent once via `send_message` (plain, non-reply) immediately after the two `restrictChatMember` calls (`GroupShield.py:149-150`), **before** the welcome text (`core_welcome`'s half runs next in the same function). Exact strings (ported verbatim, `packages/cb-core/src/cb_core/locale_data/{en,pt,es}/lib.json`): <br>• en: `"ATTENTION! Your media is restricted for <b> %(time)s minutes </b>. Please introduce yourself and get to know the members in the conversation.\n<blockquote> Press the button below or use /rules to see the group rules </blockquote>"` <br>• pt: `"ATENÇÃO! Suas mídias estão restritas por <b> %(time)s minutos </b>. Por favor se apresente e se enturme na conversa com os membros.\n<blockquote> Aperte o botão abaixo ou use o /regras para ver as regras do grupo </blockquote>"` <br>• es: `"¡ATENCIÓN! Sus medios están restringidos por <b> %(time)s minutos </b>. Por favor, preséntese y entérmese en la conversación con los miembros.\n<blockquote> Presione el botón de abajo o use el /regras para ver las reglas del grupo </blockquote>"` |
| Failure output | `GroupShield.py:151-152`: `except Exception as e: print(e)` — any `restrictChatMember` failure (bot lacks `can_restrict_members`, user already left, rate limit, ...) is swallowed with a bare `print()` to the process's stdout. No message to the group, no retry, no record anywhere that the member should have been restricted. This is a real, silent, permanent failure mode in v1: a single missed call and that member is never restricted, ever, for their entire membership. |
| Persistence | None. v1 has no table at all for "who is currently muted" — the only record of the restriction is the `until_date` Telegram itself tracks for that one `restrictChatMember` call. If the process restarts, the bot loses `can_restrict_members`, or the call simply fails, there is nothing to reconstruct from. |
| Side effects | None beyond the two API calls and the one message. |
| External calls | `restrictChatMember` (twice), `send_message` — no third-party APIs. |
| Known defects | (1) 2nd+ joiner in a batch never restricted (see "who gets restricted" above) — preserved. (2) A failed `restrictChatMember` call silently and permanently un-restricts the member, with zero record kept — this is the exact defect v2's `joined_at`-based re-architecture (see below) structurally cannot reproduce, since restriction becomes a property re-derived from persisted state on every relevant message rather than a one-shot native call. |

### What `COOKIEBOT.py:167-172` actually is

FEATURE-MAP's `core_mediarestrict` row cites this line range as the code
trigger. Read literally:

```python
elif content_type == "photo":
    if sfw and funfunctions and not publisherpost:
        add_to_random_database(msg, chat_id, msg['photo'][-1]['file_id'])
elif content_type == "video":
    if sfw and funfunctions and not publisherpost:
        add_to_random_database(msg, chat_id)
```

This is `fun_random`'s code (saving photos/videos for `/random`), not a
restriction check. There is no restriction check anywhere in v1's dispatcher.
Its real relevance to `core_mediarestrict` is structural: **a restricted
member's photo/video message never reaches this branch, or any other line of
Python, at all.** Telegram's client-side and server-side enforcement of a
`can_send_media_messages: False` mute means the message is rejected before it
is ever delivered to the bot. `COOKIEBOT.py:167-172` is, in effect, "the code
path a *not-currently-restricted* user's media takes" — its citation in
FEATURE-MAP documents the boundary, not a check to port.

## Phase 2 — the v1 vs v2 mechanism (the re-architecture)

v1 restricts **preventively and natively**: one `restrictChatMember` call at
join time, with a Telegram-managed `until_date`. Enforcement is entirely
Telegram's — the bot does nothing at message time, because Telegram never
delivers the message. v1 has no persisted notion of "when did this member
join" at all; the only state is the mute Telegram itself is holding.

v2 has `group_members.joined_at` (`packages/cb-api/migrations/versions/
0001_initial_schema.py:232-248`), whose own migration comment already commits
to a different design: *"has this member been here longer than the limit?"*.
This port implements exactly that: restriction becomes **reactive**, evaluated
at message time by comparing `now() - joined_at` against
`ctx.config.media_restrict_seconds`, rather than a native mute applied once at
join.

Concretely:

1. **Join**: this feature's own `record_join` handler
   (`@router.message(F.new_chat_members)`) inserts `(group_id, user_id)` into
   `group_members` with `joined_at = now()` (the column default). No native
   `restrictChatMember` call is made — v2 never mutes anyone for this feature.
2. **Media message**: `enforce_media_restriction`
   (`@router.message(F.photo | F.video | F.animation | F.sticker | F.voice |
   F.video_note | F.document | F.audio)`) looks up `joined_at`. If the
   configured window (`media_restrict_seconds`) has not yet elapsed and the
   sender is not an admin, the bot **deletes the message after the fact** and
   sends the same `restrict_message` text.

### The observable difference (read before assuming parity)

This is a real, user-visible mechanism change, not merely an implementation
detail, and it is intentional:

| | v1 (native, preventive) | v2 (reactive, post-hoc) |
|---|---|---|
| Who blocks the send | Telegram itself, client-side | Nobody — Telegram delivers it |
| What the sender sees | An in-client error; the message never leaves their device | Their message briefly appears in the group, then is deleted |
| What the group sees | Nothing — the attempt is invisible | A flash of the media, then its removal |
| When the warning is shown | Once, at join, before any attempt | On every blocked attempt (there is no "at join" moment to hook in v2, since no native mute exists to accompany) |
| What permission the bot needs | `can_restrict_members` (for the mute) | `can_delete_messages` (for the delete) — a **different** Telegram admin permission; v1 never needed this one for this feature at all |
| Failure mode | A missed `restrictChatMember` call permanently and silently un-restricts the member (v1 defect, see above) | A missed/failed `delete_message` call (bot lacks `can_delete_messages`) leaves the media visible but the warning is still sent (this port's `contextlib.suppress` around the delete only, not the send) |

This trade-off is exactly what the migration's own "has this member been here
longer than the limit?" comment commits v2 to: a mechanism that survives a bot
restart, a missed webhook, or the bot lacking `can_restrict_members` at join
time (all of which permanently defeat v1's one-shot mute with **zero**
record), at the cost of the block being reactive instead of preventive. Adding
a native `restrictChatMember` call on top would re-introduce v1's silent-failure
defect *and* collide with `core_welcome`'s already-drawn boundary (which
explicitly does not call `restrictChatMember` either) — this port does not do
that.

**Not ported, deliberately, as a consequence of the mechanism change:**
`can_add_web_page_previews: False` (link previews riding on an ordinary text
message). Deleting an entire text message because it happens to carry a
preview is a materially larger, more disruptive behaviour change than deleting
a photo — there is no clean reactive equivalent, and no QA scenario (v1's or
added here) exercises it. Flagged, not silently dropped.

## Phase 3 — QA scenario

Copied `../Cookiebot-QA/features/core_mediarestrict.feature` verbatim (both of
its scenarios, unchanged) into `qa/features/core_mediarestrict.feature`, then
added, to cover v1 behaviour and this port's own re-architecture decisions the
original spec did not exercise:

- A scenario for `media_restrict_seconds == 0` (v1's off switch,
  `GroupShield.py:145`).
- A scenario proving an admin is exempt even immediately after joining (the
  task's own explicit requirement, not separately spec'd in v1's code but
  required by this port's brief).
- A scenario asserting the exact `restrict_message` text and minute count
  (`round(600/60) == 10`), since the original two scenarios only assert
  "a warning message" in the abstract.
- A Scenario Outline covering every content type this port treats as
  "restricted media" that the QA harness's `make_message_update` can express
  (`photo`, `video`, `animation`, `sticker`) — the original spec only ever
  says "media content" generically.

## Phase 5 — Implementation (v2)

`packages/cb-gateway/src/cb_gateway/handlers/mediarestrict.py`,
`router = Router(name="mediarestrict")`:

- `record_join` — `@router.message(F.new_chat_members)`. First-joiner-only
  (v1 parity, see above). Skips the bot itself and any other bot joining.
  Inserts `group_members(group_id, user_id)` with `ON CONFLICT DO NOTHING`
  (a rejoin does not reset the clock — no handler in this codebase manages
  `left_at`/rejoin lifecycle yet, so this port does not invent one).
- `enforce_media_restriction` — `@router.message(F.photo | F.video |
  F.animation | F.sticker | F.voice | F.video_note | F.document | F.audio)`.
  Skips when `media_restrict_seconds <= 0` (v1's off switch) or `ctx.is_admin`.
  Reads `joined_at`; if the row is missing entirely (never recorded — see the
  router-ordering caveat below), **fails open** (does not restrict) rather
  than risk permanently restricting an existing member whose join was never
  observed. Otherwise compares elapsed time against the configured window;
  if still inside it, best-effort deletes the message and sends
  `restrict_message` with `time=round(media_restrict_seconds/60)` (the
  *configured* window, matching v1's arithmetic exactly — not remaining time).

### Router-ordering caveat (a real gap, flagged for the wiring owner)

`welcome.router`'s `on_join` (`handlers/welcome.py`) has an unconditional
`@router.message(F.new_chat_members)` handler with no `SkipHandler`. Verified
directly against the installed `aiogram` source in this repo's `.venv`
(`TelegramEventObserver.trigger` / `Router._propagate_event`): a router's
`trigger()` stops at the first handler that completes without raising
`SkipHandler`, and `Router._propagate_event` stops walking sibling routers the
moment any one of them returns a non-`UNHANDLED` response. Concretely: **once
`handlers/__init__.py` (not owned by this task) registers both
`welcome.router` and `mediarestrict.router`, whichever is registered first
"wins" the join event and the other's join handler never runs**, for every
single join, regardless of order in the update. `docs/contracts/core_welcome.md`
already flagged this exact class of problem for captcha/doomlist and left it
to the wiring owner; this port does the same. Mitigation already built into
this port so a lost race degrades gracefully rather than misbehaving: a
missing `group_members` row is treated as "not yet restricted" (fail open),
not as an error and not as "definitely new, restrict them" — see the
`enforce_media_restriction` note above.

**Needed in a file this task does not own**: `handlers/__init__.py` must add
`root.include_router(mediarestrict.router)` for either handler in this file to
run at all, and whoever does that wiring should also resolve (or explicitly
accept) the ordering race above — e.g. by having `welcome.py`'s `on_join`
`raise SkipHandler` after it finishes, or by registering `mediarestrict.router`
before `welcome.router`. Neither change is made here, per this task's file
ownership boundary.

## Phase 6 — Parity table

| Row | Verdict | Note |
|---|---|---|
| Restriction is gated on `media_restrict_seconds > 0` | same | `GroupShield.py:145`'s `if limbotimespan > 0`, ported as `<= 0` early-return. |
| Only the first joiner in a batch is ever restricted | same (preserved quirk) | Mirrors v1's `new_chat_member`-singular-field defect and `core_welcome`'s identical, explicitly-preserved quirk for the same function's messaging half. |
| `restrict_message` text, all three languages | same | Byte-identical, ported verbatim into `cb_core/locale_data/{en,pt,es}/lib.json` (pre-existing at the time of this port — not written by this task). |
| Minutes shown | same | `round(media_restrict_seconds/60)`, the *configured* window — identical arithmetic to `GroupShield.py:149`, including v1's round-half-to-even behaviour at exact `.5` boundaries. |
| Admin exemption | changed (intentional, hardening) | v1 has no explicit admin bypass in `welcome_message` (a joining admin is not a real-world case v1's code considers); this port adds `ctx.is_admin` explicitly, per this task's brief. Strictly safer, never more restrictive than v1. |
| Enforcement mechanism | **changed (intentional, re-architecture)** | Native preventive mute (v1) -> reactive post-hoc delete based on `group_members.joined_at` (v2), per migration 0001's own design comment. Full comparison and the exact observable differences (who blocks the send, what the sender/group see, when the warning appears, which bot permission is needed, the differing failure mode) are in the "observable difference" table above. This is the single largest intentional divergence in this port. |
| A failed `restrictChatMember`/`delete_message` silently defeats restriction forever | changed (intentional, fix) | v1: yes, permanently, with no record (`GroupShield.py:151-152`, bare `print()`). v2: restriction is re-derived from `joined_at` on every message, so a single failed delete only lets one message through — the *next* qualifying message is still correctly evaluated and blocked. |
| `can_add_web_page_previews` (link previews) | **not built here** | No clean reactive equivalent (deleting a whole text message is a much bigger change than deleting a photo); no v1 or added QA scenario exercises it. Flagged, not silently dropped — see "not ported, deliberately" above. |
| Persistence | same intent, new shape | v1: none (Telegram-native `until_date` only). v2: `group_members(group_id, user_id, joined_at, left_at)`, `PRIMARY KEY(group_id, user_id)`, every statement filters on `group_id` first, distributed on `group_id`, colocated with `groups` (pre-existing table, migration 0001 — not created by this task). |
| Router wiring | **not built here** | `handlers/__init__.py:build_router()` does not yet register `mediarestrict.router`, and the join-event ordering race against `welcome.router` is unresolved — both flagged above for the wiring owner, out of this task's file ownership. |

## Test results (`uv run` from repo root)

- `ruff check` / `ruff format` — clean on all five owned files.
- `pytest -q -m "not integration" qa/test_core_mediarestrict.py packages/cb-gateway/tests/test_mediarestrict.py` — unit suite (`test_mediarestrict.py`) is green and infra-free. The acceptance suite (`test_core_mediarestrict.py`) requires the real database (`clean_members`) to seed `joined_at` and, independently of the database, is red end-to-end until `mediarestrict.router` is registered in `handlers/__init__.py` — see the router-wiring note above. Both conditions are outside this task's file ownership; the same is true today of the sibling `core_welcome`/`core_rules` ports (see their own test files' NOTE sections).
- `pytest -q -m integration qa/integration/test_media_restrict.py` — requires a reachable Postgres/Citus; skips cleanly otherwise, same as every other file in `qa/integration/`.
