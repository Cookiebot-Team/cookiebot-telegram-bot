# Contract: admin resolution (v1 -> v2)

Phase 2 of `/migrate-feature` for `group_admins`. The table has existed since
migration 0001 and nothing populates it — every admin-gated command in M1
(`/config`, `/newrules`, `/newwelcome`, `/repost`, `/deleteposts`, giveaways, the
`/adm` call-admins flow, `/everyone`) depends on this module existing first.

## Phase 2 table

| Aspect | v1 behaviour (with file:line) |
|---|---|
| Admin fetch | `get_admins(cookiebot, chat_id, ignorecache=False)` (`Configurations.py:56-77`) calls `cookiebot.getChatAdministrators(chat_id)` (`:64`) once per chat, unwraps each entry's `admin['user']['id']` and `admin['status']` into three parallel lists (`listaadmins` = usernames, `listaadmins_id` = user ids as `str`, `listaadmins_status` = Telegram's `status` string), and calls `get_group_info` to push the id list to the Java backend (`groups/{chat_id}` `adminUsers`, `Configurations.py:39-54`, `GroupService.java` `adminUsers` field — a flat `Set<String>`, no role, no privilege flags). |
| Cache | A single unbounded, unlocked, never-expiring module dict `cache_admins = {}` (`Configurations.py:10`), one copy **per process** — v1 runs 5 bot processes, so 5 independent, permanently-stale-until-manually-reloaded views (FEATURE-MAP D6). |
| Refresh / `/reload` | `/reload` and `/recarregar` (`COOKIEBOT.py:197-201`) call `get_admins(..., ignorecache=True)` **and** `get_config(..., ignorecache=True)` for the *calling process only*, then reply with the localised string `lib.json:"reload"` — `"Memory reloaded successfully!"` / pt `"Memória recarregada com sucesso!"` / es `"¡Memoria recargada con éxito!"`. The other 4 processes keep serving stale admin lists until each is separately reloaded or restarted. Several other commands also force a refresh as a side effect: `/repost`, `/deleteposts` (`COOKIEBOT.py:207,210`), and the "Pub"/"GIVEAWAY" callback path (`:279`) all pass `ignorecache=True`. |
| No-admin-fetch failure path | `get_admins` has **no** `try/except` around `getChatAdministrators` (`Configurations.py:64`). A Telegram failure there (network error, bot removed from group, rate limit) propagates up through `thread_function`'s single broad `except Exception: send_error_traceback(...)` (`COOKIEBOT.py:329-330`), which mails the bot owner a traceback and drops the update — **the group gets total silence**, whatever the user asked for, admin-gated or not. There is no "serve stale / treat nobody as admin" fallback anywhere in v1. |
| Ordinary permission check | The repeated pattern is `str(msg['from']['id']) not in listaadmins_id and int(msg['from']['id']) != ownerID` (e.g. `update_welcome_message`/`update_rules_message`: `Configurations.py:254,270`; `schedule_autopost`: `Publisher.py:290`; `cancel_posts`: `Publisher.py:318`) — a plain membership test against the id list, plus a hardcoded bot-owner bypass (`ownerID`, out of scope for this module — see "Not built here" below). Failure text is one of three hardcoded per-language strings, e.g. `"You are not a group admin!"` / `"Você não é um administrador do grupo!"` / `"¡No eres un administrador del grupo!"`. |
| Creator vs. administrator | `listaadmins_status` carries Telegram's raw `status` (`"creator"` or `"administrator"`), but it is used **only** in one bizarre recurring guard, never to grant extra privilege: `'creator' in listaadmins_status and str(from_id) not in listaadmins_id and str(from_id) != str(ownerID)` (`Configurations.py:141` `configurar`; `Giveaways.py:27` `giveaways_ask`; `COOKIEBOT.py:363,416` the `Pub`/`GIVEAWAY` callback branches). Because every real Telegram group always has exactly one member with `status == "creator"`, `'creator' in listaadmins_status` is true for every group with any admin data at all — the clause is dead weight and the check reduces to "not in the id list and not the bot owner". No caller ever branches on creator vs. administrator to grant a *different* set of privileges. |
| Specific privileges | `can_restrict_members`, `can_delete_messages`, `can_promote_members` etc. are **never read** anywhere in `Bot/*.py` (confirmed by grep across the whole bot source) or in the Java backend (`Group.java` stores only the id set, `AuthorizationService.isAdminOfGroup` / `GroupService.isAdmin` is membership-only). v1's admin model is binary: in the id list, or not. |
| **The anonymous-admin defect** | Because the check above is a membership test on `msg['from']['id']`, and an anonymous admin's `from.id` is Telegram's synthetic `GroupAnonymousBot` (**1087968824**) rather than the admin's own id, `configurar`, `giveaways_ask`, and the `Pub`/`GIVEAWAY` callback handlers **always reject a genuine admin who is posting anonymously** — the `'creator' in listaadmins_status` clause doesn't save them, since it was already established to be a no-op. The user is shown a permission-denied message plus, only in `configurar` (`Configurations.py:141-144`), the bot uploads and sends `Static/remove_anonymous_tutorial.mp4` telling them to turn anonymous mode off in Telegram's group settings — treating Telegram's own admin feature as user error. Six other call sites (`update_welcome_message`, `update_rules_message`: `Configurations.py:254,270`; `schedule_autopost`, `cancel_posts`: `Publisher.py:290,318`; `everyone`: `UserRegisters.py:99`) get this right *by accident*: their guard is `str(from_id) not in listaadmins_id and 'sender_chat' not in msg and ...`, so the mere **presence** of `sender_chat` in the update short-circuits the admin check entirely (Telegram only lets a user attach `sender_chat` = the group to a message if Telegram itself has already verified they are an admin with the anonymity permission on — the bot never needs to re-derive their user id from the id list). So v1 has two contradictory anonymous-admin behaviours living side by side, and the more visible, more heavily used one (`/configurar`, the config entry point) is the wrong one, complete with a tutorial video whose actual fix ("stop being anonymous") is unnecessary — the bot could simply have trusted `sender_chat` like the other six call sites already do. |
| Anonymous vs. linked-channel post | v1's guard is `'sender_chat' not in msg`, i.e. presence of the key alone, not "does `sender_chat` equal this group". A message auto-forwarded from a *linked discussion channel* also carries `sender_chat` (set to the **channel**, not the group), so those call sites would treat a channel's automated repost as if it were an anonymous group admin. Rare in practice for these commands (nobody replies `/newrules` from a linked channel), but it is real drift between "sender is hidden because they're an anonymous group admin" and "sender is hidden because Telegram is showing the linked channel as the author". |

## Policy decided for v2

1. **Fetch, cache, persist**: `getChatAdministrators` is the source of truth,
   fetched through the aiogram `bot` passed in. The result is cached (L1
   in-process + L2 Valkey, see below) *and* written to `group_admins` so the row
   set survives a cache flush, a process restart, and a Telegram outage. This
   directly replaces the v1 D6 per-process dict (`Configurations.py:9-12`).
2. **`/reload` becomes `refresh()`**: one call, forces a real Telegram fetch,
   rewrites the cache and the table in a single transaction. Because the cache is
   shared (Valkey), a refresh from any replica fixes every replica — v1's "reload
   each of 5 processes separately" problem does not exist here.
3. **Telegram-failure fallback (new — v1 had none)**: `refresh()` catches the
   Telegram call failing and falls back to what is already durably persisted in
   `group_admins` for that `group_id`; if the table has no rows yet either
   (never synced), nobody is treated as an admin, and the failure is logged
   (`admins.telegram_failed`). This is an explicit fix for the "total silence"
   defect above (`COOKIEBOT.py:329-330`): a Telegram outage now degrades
   gracefully to "last known admins" or "no admins", never to "everyone is an
   admin", and never to silently dropping the update.
4. **Anonymous senders are trusted, not interrogated** (fixes the `/configurar`
   half of the defect, generalises the accidental-good half): `is_anonymous_sender`
   is true when `message.sender_chat.id == message.chat.id` (an anonymous group
   admin — tightened from v1's "`sender_chat` present" to specifically "the
   *group itself*, not a linked channel", closing the linked-channel-post gap
   above) **or** `message.from_user.id == ANONYMOUS_BOT_ID` (Telegram's
   `GroupAnonymousBot`, belt-and-suspenders for any transport that surfaces one
   signal but not the other). `resolve_actor` treats this case as
   `ActorCheck(user_id=None, is_admin=True, anonymous=True)` unconditionally —
   Telegram itself only allows a message to carry `sender_chat` = the group when
   the sender already holds admin rights with anonymity turned on, so there is
   nothing left to verify against our own cached id list (which cannot contain a
   real user id for this message anyway). This means an anonymous admin now
   **succeeds** at `/config`, giveaways, etc. instead of being shown a
   permission-denied message and a tutorial video about a Telegram setting that
   was never the actual problem. No handler built on `resolve_actor` needs its
   own `'sender_chat' not in msg`-shaped special case ever again.
5. **No creator/administrator privilege split, because v1 never had one**: the
   `role` field (`'creator' | 'administrator'`) is captured and persisted since
   the column already exists and callers may want it for display or analytics,
   but `is_admin`/`admin_ids`/`resolve_actor` do not distinguish — matching v1,
   where the creator-vs-administrator check was dead code. `can_restrict_members`
   and `can_delete_messages` are parsed from the Telegram payload (v1 never read
   them, but the dataclass is useful groundwork for M2 moderation commands that
   will actually gate on them); a `creator` is always reported with both `True`
   (the role has every privilege implicitly, and Telegram's `ChatMemberOwner`
   payload doesn't even carry these fields to ask). **These two flags are not
   persisted as separate columns** — migration 0001's `group_admins` only has
   `role`/`anonymous`/`synced_at` — so when `refresh()` falls back to the
   persisted table after a Telegram failure, the flags are reconstructed as
   `True` for `creator` and conservatively `False` for `administrator` (their
   real values are only known fresh from Telegram); this is called out in the
   docstring and only affects code that reads privilege flags during an active
   Telegram outage, which nothing in M1 does yet.
6. **`group_admins.anonymous`**: this column records each admin's Telegram
   `is_anonymous` flag (the "remain anonymous" toggle on *that admin's role*,
   independent of whether any given message happened to be sent anonymously) —
   useful for the admin panel and analytics later. It is not part of the public
   `Admin` dataclass (which mirrors the fields M1 handlers actually need) but is
   written on every `refresh()`.

## Not built here (explicitly out of scope)

- The bot-owner bypass (`ownerID`/`Settings.owner_id`) is a superuser concept
  layered on top of group-admin resolution, not part of it — v1 mixes the two
  in the same `if` everywhere, but a handler that wants "admin OR bot owner"
  composes `resolve_actor(...).is_admin or user_id == settings.owner_id` itself.
- Rendering the permission-denied message, and whether to still send a
  reduced/no-op tutorial hint, is a handler concern. `ActorCheck` gives the
  handler `is_admin` and `anonymous`; nothing here calls `send_message`.
- `get_group_info`'s side effect of pushing `adminUsers` to the Java backend
  (`Configurations.py:39-54`) is v1's own backend-sync path; v2's backend reads
  `group_admins` directly, no separate push required.

## Public API (`packages/cb-core/src/cb_core/admins.py`)

```python
ANONYMOUS_BOT_ID: int = 1087968824

@dataclass(frozen=True, slots=True)
class Admin:
    user_id: int
    role: str            # 'creator' | 'administrator'
    can_restrict_members: bool
    can_delete_messages: bool

async def admins(bot, group_id: int) -> tuple[Admin, ...]        # cached
async def admin_ids(bot, group_id: int) -> frozenset[int]
async def is_admin(bot, group_id: int, user_id: int) -> bool
async def refresh(bot, group_id: int) -> tuple[Admin, ...]       # forces a Telegram fetch

def is_anonymous_sender(message) -> bool
async def resolve_actor(bot, message) -> ActorCheck

@dataclass(frozen=True, slots=True)
class ActorCheck:
    user_id: int | None
    is_admin: bool
    anonymous: bool
```

## Caching design

- **L1**: a per-process dict, TTL = `Settings.config_cache_l1_seconds` (30s
  default) — mirrors the same L1/L2 split the config cache uses, so a message
  storm on one group doesn't hit Valkey per message.
- **L2**: Valkey, key `cb:admins:{group_id}`, TTL = `Settings.admin_cache_seconds`
  (600s default, already existed in `Settings` before this change).
- Every lookup at every layer is counted through the existing
  `cb_core.metrics.cache_lookups_total(cache="admins", layer="l1"|"l2"|"telegram"|"fallback", outcome="hit"|"miss"|"error")`
  metric (no new metric added, per the task constraint).
- `refresh()` always bypasses L1/L2 reads (it exists specifically to force a
  real fetch), then repopulates both layers plus `group_admins` on success.
