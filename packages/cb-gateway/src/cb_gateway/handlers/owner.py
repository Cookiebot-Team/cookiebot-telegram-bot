"""x_owner_commands — the operator's private-chat commands.

v1: the owner branch of the private-chat block,
`../COOKIEBOT-Telegram-Group-Bot/Bot/COOKIEBOT.py:83-105`. Contract:
`docs/contracts/x_owner_commands.md`. Spec:
`.specs/features/x_owner_commands/spec.md`. No QA scenario exists —
`qa/features/x_owner_commands.feature` is authored, not ported.

Every command here is gated on `settings.owner_id`, exactly as v1 gates on
its `ownerID` env var, and every one is private-chat only: v1's whole block
sits inside `if chat_type == 'private':` and returns unconditionally at the
end. They go through `cb_gateway.private_context`, the mechanism
`.specs/features/private_dispatch/` built for precisely this — a DM has no
`group_id`, and a type that cannot hold one cannot query `group_configs` with
a private chat's id.

## Two of v1's seven are deliberately not ported

`.specs/features/private_dispatch/spec.md` recommended porting none of them.
That recommendation is followed for exactly the two it actually argued
against, and the argument is about **process control**, not about owner
commands as a category:

* **`/stop`** — `kill_api_server(); os._exit(0)` (`COOKIEBOT.py:97-99`).
* **`/restart`** — `kill_api_server(); os.execl(...)` (`:100-102`).

v1 ran one process per persona on one host, so "the process" was
unambiguous. v2 runs N stateless gateway replicas behind a scheduler:
`os._exit` on whichever replica happened to receive the DM kills one of N and
the orchestrator restarts it immediately, so the command is at best a
confusing no-op and at worst a self-inflicted outage if an operator repeats
it. Restarting a deployment is the orchestrator's job and it already has a
verb for it. Both commands answer with that explanation rather than being
silently absent — an owner who types `/stop` and gets nothing would
reasonably assume it worked.

The other five are ordinary data operations that fit a multi-replica service
unchanged, and are ported.
"""

from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.types import Message

from cb_core import jobs, locales, ops
from cb_core.logging import get_logger
from cb_core.settings import get_settings
from cb_core.textmatch import ParsedCommand
from cb_gateway.filters import CommandName
from cb_gateway.private_context import private_context_for
from cb_gateway.queue import enqueue
from cb_gateway.telemetry import mark_outcome

log = get_logger("cb.owner")

router = Router(name="owner")

#: How many groups one `/grupos` prints. v1 sent **one Telegram message per
#: group** with a 0.4s `getChat` between each (`Miscellaneous.py:93-103`) —
#: FEATURE-MAP D11. One paged message instead.
GROUPS_PAGE_SIZE = 100

#: What `/stop` and `/restart` answer instead of doing what v1 did. See the
#: module docstring; this is a deliberate refusal, not a stub.
PROCESS_CONTROL_REFUSAL = (
    "Not available in v2. This runs as several replicas behind an orchestrator, "
    "so stopping or restarting the one process that received this message would "
    "take down a fraction of the deployment and be undone immediately. "
    "Use the orchestrator's own rollout/restart instead."
)


def is_owner(user_id: int) -> bool:
    """v1: `msg['from']['id'] == ownerID` (`COOKIEBOT.py:83`, and repeated on
    every branch). An unset `CB_OWNER_ID` means **nobody** is the owner —
    v1's `int(os.getenv('ownerID'))` would have crashed at import instead, so
    there is no "unconfigured means everyone" behaviour to preserve.
    """
    owner_id = get_settings().owner_id
    return bool(owner_id) and user_id == owner_id


def _private_owner_only(message: Message) -> bool:
    from_user = message.from_user
    return from_user is not None and is_owner(from_user.id)


def format_group_list(groups: tuple[ops.GroupSummary, ...], total: int) -> str:
    """v1's `f"{id} - {title}"` lines and its `groups.total` footer
    (`Miscellaneous.py:99,105`), in one message rather than one per group."""
    lines = [f"{group.group_id} - {group.title}" for group in groups]
    # `groups` is a nested catalog object (`total`/`new`/`remove`), so this
    # goes through `get_nested`, not `get` — the same distinction
    # `x_distortion` and `x_giveaways` document for their own sections.
    lines.append(locales.get_nested("groups", "total", "en", value=total))
    return "\n".join(lines)


def parse_subject(args: str) -> int | None:
    """v1: `msg['text'].split()[1]`, then `str(...).replace('@', '')`
    (`universal_funcs.py:307-308`) — so `@123` and `123` are the same id, and
    a username was never actually supported despite the `@` stripping. An
    unparseable argument returns `None`; v1 raised `IndexError`/`ValueError`
    into the global handler and answered nothing.
    """
    token = args.strip().split()[0] if args.strip() else ""
    token = token.lstrip("@")
    try:
        return int(token)
    except ValueError:
        return None


# -------------------------------------------------------------------- commands


@router.message(F.chat.type == ChatType.PRIVATE, CommandName("groups"), _private_owner_only)
async def list_groups(message: Message) -> None:
    """`/grupos`, `/groups`. v1: `list_groups` (`Miscellaneous.py:83-112`)."""
    private_context_for(message)  # asserts the shape; a DM has no group_id
    groups = await ops.list_groups(limit=GROUPS_PAGE_SIZE)
    total = await ops.count_groups()
    await message.answer(format_group_list(groups, total))


@router.message(F.chat.type == ChatType.PRIVATE, CommandName("leave"), _private_owner_only)
async def leave_group(message: Message, bot: Bot, parsed: ParsedCommand | None = None) -> None:
    """`/leave <chat_id>`. v1: `leave_and_blacklist` (`universal_funcs.py:320-329`)
    plus a confirmation DM (`COOKIEBOT.py:103`)."""
    subject = parse_subject(parsed.args if parsed is not None else "")
    if subject is None:
        await message.answer("Usage: /leave <chat_id>")
        return

    await ops.blacklist_add(subject, kind="chat", reason="owner /leave")
    await ops.forget_group(subject)
    try:
        await bot.leave_chat(subject)
    except Exception as exc:  # noqa: BLE001 - v1 prints and continues (`:328-329`)
        log.warning("owner.leave_failed", chat_id=subject, error=str(exc))
    # v1's own wording (`COOKIEBOT.py:103`).
    await message.answer(f"Auto-left\n{subject}")


@router.message(F.chat.type == ChatType.PRIVATE, CommandName("blacklist"), _private_owner_only)
async def blacklist(message: Message, parsed: ParsedCommand | None = None) -> None:
    """`/blacklist <user_id>`. v1: `blacklist_user` (`universal_funcs.py:307-309`)."""
    subject = parse_subject(parsed.args if parsed is not None else "")
    if subject is None:
        await message.answer("Usage: /blacklist <user_id>")
        return
    await ops.blacklist_add(subject, kind="user", reason="owner /blacklist")
    # v1's own wording (`COOKIEBOT.py:100`).
    await message.answer(f"Blacklisted user with ID {subject}")


@router.message(F.chat.type == ChatType.PRIVATE, CommandName("unblacklist"), _private_owner_only)
async def unblacklist(message: Message, parsed: ParsedCommand | None = None) -> None:
    """`/unblacklist <user_id>`. v1: `unblacklist_user` (`:311-313`)."""
    subject = parse_subject(parsed.args if parsed is not None else "")
    if subject is None:
        await message.answer("Usage: /unblacklist <user_id>")
        return
    removed = await ops.blacklist_remove(subject)
    if not removed:
        # v1 answers the same line either way, so an owner could not tell a
        # typo from a successful removal.
        await message.answer(f"User with ID {subject} was not blacklisted")
        return
    await message.answer(f"Unblacklisted user with ID {subject}")


@router.message(F.chat.type == ChatType.PRIVATE, CommandName("broadcast"), _private_owner_only)
async def broadcast(message: Message, parsed: ParsedCommand | None = None) -> None:
    """`/broadcast <text>`. v1: `broadcast_message` (`Miscellaneous.py:114-122`).

    v1 sent the message inline with `sleep(0.5)` between groups (D8); the
    fan-out is `cb_worker/jobs/broadcast.py`, which also reports the count
    back — v1 reported nothing.
    """
    text = parsed.args.strip() if parsed is not None else ""
    if not text:
        await message.answer("Usage: /broadcast <message>")
        return
    queued = await enqueue(jobs.BROADCAST_TO_GROUPS, text=text, owner_id=message.chat.id)
    if not queued:
        await message.answer("The broadcast could not be queued — the job broker is unreachable.")
        return
    await message.answer("Broadcast queued.")


@router.message(F.chat.type == ChatType.PRIVATE, CommandName("stop"), _private_owner_only)
@router.message(F.chat.type == ChatType.PRIVATE, CommandName("restart"), _private_owner_only)
async def process_control(message: Message) -> None:
    """`/stop` and `/restart` — deliberately not ported. See the module
    docstring for why, and why they answer rather than being absent."""
    mark_outcome("refused")
    await message.answer(PROCESS_CONTROL_REFUSAL)


__all__ = [
    "GROUPS_PAGE_SIZE",
    "PROCESS_CONTROL_REFUSAL",
    "blacklist",
    "broadcast",
    "format_group_list",
    "is_owner",
    "leave_group",
    "list_groups",
    "parse_subject",
    "process_control",
    "router",
    "unblacklist",
]
