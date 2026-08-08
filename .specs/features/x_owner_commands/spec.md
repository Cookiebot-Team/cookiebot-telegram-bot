# x_owner_commands — Specify

**Feature id:** `x_owner_commands` · **Area:** util · **Milestone:** M3 ·
**Kind:** v1 port with no QA scenario
(`docs/site/content/docs/feature-map.mdx` §4).

## Goal

The bot's private chat answers seven operator commands to whoever holds
`CB_OWNER_ID`, and nobody else: list the groups the deployment is in
(`/grupos`, `/groups`), broadcast a message to all of them (`/broadcast`),
leave and blacklist a chat (`/leave`), add and remove a user from the global
blacklist (`/blacklist`, `/unblacklist`), and stop or restart the process
(`/stop`, `/restart`).

## Source of truth

`../COOKIEBOT-Telegram-Group-Bot/Bot/COOKIEBOT.py:83-105` — the owner branches
of the private-chat `if/elif` chain — calling `list_groups`
(`Miscellaneous.py:83-112`), `broadcast_message` (`Miscellaneous.py:114-122`),
`leave_and_blacklist` (`universal_funcs.py:320-329`) and `blacklist_user` /
`unblacklist_user` (`universal_funcs.py:307-313`). The per-branch behaviour
table with file:line is `docs/contracts/x_owner_commands.md` §Phase 2.

## Three findings that shape the port

**1. Two of the seven are process control, and v2 has no process to control.**
`/stop` is `kill_api_server(); os._exit(0)` and `/restart` is
`kill_api_server(); os.execl(...)` (`COOKIEBOT.py:97-102`). v1 ran one process
per persona on one host, so "the process" was unambiguous. v2 runs N stateless
gateway replicas behind an orchestrator: `os._exit` on whichever replica
happened to receive the DM kills one of N and the orchestrator restarts it
immediately. `.specs/features/private_dispatch/spec.md` recommended not
porting the owner commands at all on the strength of this; that argument is
accepted for exactly these two and rejected for the other five, which are
ordinary data operations that fit a multi-replica service unchanged.

**2. `/grupos` was a thirty-minute command.** v1 fetched every group from the
backend, called `getChat` on each with a `time.sleep(0.4)` between them, and
sent **one Telegram message per group** with another `sleep(0.1)`
(`Miscellaneous.py:93-103`). A thousand groups is a thousand messages and
about eight minutes of a blocked thread. FEATURE-MAP **D11** ("no pagination
anywhere") names this. v2 stores the title as a column, so the whole answer is
one paged message and no Telegram round trip per row.

**3. The blacklist is already a v2 table with a `kind` column.** `blacklist`
(migration `0001`, a reference table) predates this port —
`util_doomlist` writes it on a CAS hit. v1 posted both banned users and
abandoned chats to the same `blacklist/{id}` endpoint with no way to tell them
apart, so `leave_and_blacklist` and `blacklist_user` were indistinguishable in
the store; `kind` finally separates them. Nothing about the doomlist's reads
changes: it already filters `kind = 'user'`.

## Decisions

| # | Decision | Why |
|---|---|---|
| R1 | Five commands ported, `/stop` and `/restart` **answer a refusal** rather than being absent | An owner who types `/stop` and gets silence assumes it worked. The refusal names the orchestrator as the thing that owns restarts. |
| R2 | Every handler is `F.chat.type == ChatType.PRIVATE` + an owner predicate | v1's whole block sits inside `if chat_type == 'private':` and re-tests `msg['from']['id'] == ownerID` on every branch. |
| R3 | The DM goes through `cb_gateway.private_context`, not `context_for` | `.specs/features/private_dispatch/` built `PrivateContext` for precisely this: a type that cannot hold a `group_id` cannot query `group_configs` — distributed on `group_id` — with a private chat's own id. |
| R4 | An unset `CB_OWNER_ID` means **nobody** is the owner | v1's `int(os.getenv('ownerID'))` would have crashed at import, so there is no "unconfigured means everyone" behaviour to preserve, and defaulting the other way would hand `/broadcast` to the internet. |
| R5 | `/broadcast` enqueues `jobs.BROADCAST_TO_GROUPS` and reports back | AGENTS.md §2.4: the fan-out is N Telegram calls. v1 looped inline with `sleep(0.5)` (FEATURE-MAP **D8**) and reported nothing at all. |
| R6 | `/grupos` is paged at `GROUPS_PAGE_SIZE = 100` | See finding 2. |
| R7 | `/leave` deletes the `groups` row and lets the FKs cascade | v1 deleted the three collections it remembered to name (`registers`, `configs`, `groups`) and left `randomdatabase`, scheduled posts and giveaways pointing at a group it had just left. |
| R8 | `/unblacklist` distinguishes "removed" from "was not listed" | v1 answered the same line either way, so an owner could not tell a typo from a successful removal. Rejected as a defect to preserve: it is an operator-facing message with no group-visible effect. |
| R9 | The five ported commands' statements deliberately span shards | AGENTS.md §4.4's sanctioned shape: single-table, index-backed, rare and human-triggered. "What groups am I in" is a question *about* the shard key, not one answerable inside it. |

## Success criteria

1. Every one of v1's seven branches has an equivalent that answers, in v1's
   own wording where v1 had wording.
2. A non-owner reaches none of them, in a DM or in a group.
3. `/broadcast` hands off to `cb-worker` and says so; the fan-out itself is
   the worker's test.
4. `qa/features/x_owner_commands.feature` passes against the real dispatcher.
5. `ruff`, `mypy` and `cb.py check` clean.
