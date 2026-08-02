# private_dispatch — Design

## R1 — the mechanism

**R1.1** `packages/cb-gateway/src/cb_gateway/private_context.py` (new),
mirroring `cb_gateway/context.py`'s role but for a chat that is not a group:

```python
@dataclass(frozen=True, slots=True)
class PrivateContext:
    user_id: int


def private_context_for(message: Message) -> PrivateContext: ...
```

**R1.2 — deliberately synchronous, not async.** `context_for` is `async`
because it awaits `group_config.get_config()` and `admins.resolve_actor()` —
both real I/O against distributed tables. `private_context_for` reads only
fields already on the `Message` object aiogram handed the handler; there is
nothing to await because there is nothing to query. This is not a
simplification for its own sake — it is the concrete, type-level proof of
AGENTS.md's non-negotiable #2 for this module: a function with no `await`
cannot accidentally issue a distributed-table read.

**R1.3 — no `group_id` field, anywhere.** `PrivateContext` does not carry a
`group_id`, a `config: GroupConfig`, or an `actor: ActorCheck` — the three
things `ChatContext` exists to hold, all of them either keyed by or derived
from a real group's row. This is the actual design decision worth stating
explicitly, per the brief: it is not merely that today's two consumers don't
need those fields, it is that giving `PrivateContext` a `group_id` at all
would let a future handler write `group_config.get_config(ctx.group_id)`
against a private chat's id and get a plausible-looking wrong answer, exactly
the live bug `/privacy` has today (spec.md). The type itself is the guard
rail.

**R1.4 — `user_id` only, `lang` deliberately not included.** v1 uses **two**
different DM language conventions, not one: `pv_default_message` derives a
per-sender language from `normalize_lang(from.language_code)`, while
`/commands` and `/privacy` both hardcode English regardless of the sender.
A single `PrivateContext.lang` field would have to guess which convention a
future handler wants, or silently pick one — neither is honest. Both of
this slice's consumers hardcode `"en"` directly at the call site (matching
v1 exactly, see R2 below); `lang` is added to `PrivateContext` only when a
real second convention needs representing — `/start`'s per-sender derivation
is the obvious future candidate, not built here (spec.md's named follow-up).

**R1.5 — routing pattern, not a new router file.** No new entry in
`handlers/__init__.py`'s `build_router()`. Both of this slice's retrofits
(R2) add a **second, chat-type-filtered handler function to an
already-registered router** (`privacy.router`, `listcommand.router`) rather
than introducing a new router — the same `F.chat.type == ChatType.PRIVATE` /
`F.chat.type != ChatType.PRIVATE` pair every group-only handler in this
codebase already spells out for the opposite case (`ship.py`, `battle.py`,
`fun_random.py`, `members.py`). No new registration means no ordering
question either: a private-chat update simply never matches any of the
group-scoped filters, so which router runs first is irrelevant between the
two chat-type worlds.

**R1.6 — the bookkeeping-hook shape is *not* built here.** `util_birthday`'s
DM collection (next slice) needs something that runs for *every* private
message, unconditionally, the private-chat equivalent of
`cb_gateway/handlers/members.py` (registered first, always `SkipHandler`, for
groups). That is a new router file and *does* need an `include_router` line
— deliberately deferred to the birthday slice, where there is a real first
consumer to build it against, rather than shipping an empty hook here on
spec alone.

## R2 — the two retrofits

**R2.1 `privacy.py`** — split the single ungated handler into two:

```python
@router.message(F.chat.type == ChatType.PRIVATE, CommandName("privacy"))
async def privacy_private(message: Message) -> None:
    await message.reply(locales.get("privacy", "en"))


@router.message(F.chat.type != ChatType.PRIVATE, CommandName("privacy"))
async def privacy(message: Message) -> None:
    ctx = await context_for(cast(Bot, message.bot), message)
    await message.reply(t(ctx, "privacy"))
```

Neither branch calls `private_context_for` — there is nothing on it either
handler needs (spec.md's contract: no owner check, no per-sender text). The
mechanism's value here is the **filter pattern and the fixed bug**, not the
context object; forcing a call to `private_context_for()` just to extract an
unused `user_id` would be dead code for its own sake.

**R2.2 `listcommand.py`** — same split, relocating the existing inline
`if message.chat.type == ChatType.PRIVATE` branch out of the single handler
into its own filtered function; `_commands_available(skin)`'s catalog gate
applies to both, matching v1's own choice to dispatch DM and group `/commands`
through the identical function (`list_commands`, only the `language` argument
differs):

```python
@router.message(F.chat.type == ChatType.PRIVATE, CommandName("commands"))
async def list_commands_private(message: Message, skin: str = tenancy.DEFAULT_TENANT) -> None:
    if not await _commands_available(skin):
        mark_outcome("silent")
        return
    await message.reply(locales.text("Cookiebot_functions", "en"))


@router.message(F.chat.type != ChatType.PRIVATE, CommandName("commands"))
async def list_commands(
    message: Message, bot: Bot | None = None, skin: str = tenancy.DEFAULT_TENANT
) -> None:
    if not await _commands_available(skin):
        mark_outcome("silent")
        return
    ctx = await context_for(cast(Bot, bot or message.bot), message)
    await message.reply(locales.text("Cookiebot_functions", ctx.lang))
```

Purely a relocation — `docs/contracts/core_listcommand.md`'s Phase 6 table
does not change a single verdict, only "where the code that does this lives."
Updated at close-out to say so explicitly rather than silently.

## R3 — AGENTS.md §2's non-negotiable #2, worked out explicitly

Every module a private-chat code path in this slice touches, and what each
does with the chat's id:

| Module | Called with a private chat's id? | Safe? |
|---|---|---|
| `cb_core.group_config` (`group_configs`, distributed on `group_id`) | **No** — neither retrofit calls `context_for`/`group_config.get_config` on the private branch | n/a, never reached |
| `cb_core.admins` (`group_admins`, distributed on `group_id`; also calls `bot.get_chat_administrators`, a group-only Bot API method) | **No** | n/a, never reached |
| `cb_core.members` (`group_members`, distributed on `group_id`) | **No** — not touched by either retrofit; the birthday slice will need to reason about this separately, since it writes to `users` (a **reference** table, not distributed on `group_id` at all) | n/a here |
| `cb_core.locales` | Yes — `locales.get("privacy", "en")` / `locales.text("Cookiebot_functions", "en")` | Safe — a pure in-memory lookup keyed by a language string, never a chat id |
| `private_context.private_context_for` | Yes — reads `message.from_user.id` | Safe by construction (R1.2/R1.3) — no I/O, no distributed table, no `group_id`-shaped field to misuse |

Nothing in this slice issues a Postgres query keyed on a private chat's id,
in either the code that ships or the code deliberately not written.

## Open decisions — answered

1. **Wiring `/privacy` and `/commands` DM behaviour is in this slice**, not a
   follow-up — `/commands` already existed (relocated only) and `/privacy`'s
   fix is small and directly demonstrates the mechanism generalises beyond a
   single feature, per the brief's own framing (`util_everyone`'s enqueue
   precedent).
2. **Owner-only ops commands are recommended dropped**, not ported — see
   spec.md. Flagged as a policy call, not decided unilaterally; nothing here
   depends on the answer.
3. **`/start` and the DM welcome screen are a named follow-up**, not this
   slice — real, larger, separate design questions (multi-tenant branding,
   the per-sender-language convention `PrivateContext.lang` would need to
   grow to support it).
4. **`PrivateContext` has no `lang` field yet** — R1.4. Grows when `/start`
   (the first consumer that needs per-sender language) is actually built,
   not speculatively now.
5. **No new router file / `handlers/__init__.py` change** — R1.5/R1.6. The
   birthday slice adds the private equivalent of `members.py` when it has a
   real hook to register.
