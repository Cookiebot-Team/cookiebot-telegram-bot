# util_deletereposts — Specify

**Feature id:** `util_deletereposts` · **Milestone:** M3 · **Kind:** v1 port
**v1 source:** `Bot/Publisher.py:316-327` (`cancel_posts`), dispatched at
`COOKIEBOT.py:209-211`.

## Goal

`/deleteposts` cancels every scheduled post this group asked for — both its own
`/repost` jobs and the fan-out campaigns it originated in *other* groups via
`/divulgar`.

## Scope

**In:** the command, its admin gate, the delete, the reaction and the reply.

**Out:** the `scheduled_posts` table and everything that writes it —
`util_postforwarder`.

## Phase 2 — v1 behaviour contract

| Aspect | v1 behaviour (file:line) |
|---|---|
| Triggers | `/deleteposts`, `/apagarposts` (`COOKIEBOT.py:209`). The chain lists `"/apagarposts"` **twice** and never ships `/deletereposts`, which is what QA specifies — see conflicts. All three already resolve in `cb_core.textmatch.COMMAND_ALIASES` (`textmatch.py:60-62`) |
| Preconditions | admin **or** `'sender_chat' in msg` (`:318`). Admins are fetched with `ignorecache=True` (`COOKIEBOT.py:210`). Note there is **no `ownerID` bypass** here, unlike `schedule_autopost` (`:290`) which has one — the owner cannot cancel a group's posts unless they are an admin of it |
| Feature flag | none — not gated on `functionsUtility` or `functionsFun` |
| Cooldowns | none |
| Not an admin | reply `not_group_admin` (`"You are not a group admin!"` / `"Você não é um administrador do grupo!"` / `"¡No eres un administrador del grupo!"`), return. **No video, no anonymous-mode wording** — see conflicts (`:319-321`) |
| Scope of the delete | every row whose `second_chatid` equals this chat — i.e. every job *this chat requested*, regardless of which group it targets (`:322-324`) |
| Success output | react `👍` to the command (`is_big=True`, the default), then reply `deletereposts_done` (`:325-327`) |
| Persistence | `DELETE FROM publisher WHERE name = ?`, once per matching row (`:120`) |
| Side effects | `send_chat_action(typing)` first (`:317`); posts already delivered are untouched — only future ones are cancelled |
| External calls | none |
| Known defects | D-DR-1 below |

### Strings — verbatim, all three languages

New keys in v2's `cb.json` overlay; v1 has them inline, not in
`Bot/Static/locales/`.

| key | en | pt | es |
|---|---|---|---|
| `not_group_admin` | `You are not a group admin!` | `Você não é um administrador do grupo!` | `¡No eres un administrador del grupo!` |
| `deletereposts_done` | `Posts and reposts canceled!` | `Posts e reposts do grupo cancelados!` | `¡Publicaciones y reenvíos del grupo cancelados!` |

`not_group_admin` is shared with `util_postforwarder`'s `/repost` gate — same
literal in v1 (`:291` and `:320`), one key.

### Known defects

| id | Defect | v1 | Verdict |
|---|---|---|---|
| D-DR-1 | The delete walks every row in Python and issues one `DELETE … WHERE name = ?` per match, against the unlocked shared SQLite connection of D-PF-1 | `:322-324` | **fix** — one predicate-driven `DELETE` |
| D-DR-2 | `'sender_chat' in msg` is treated as sufficient authorisation, so **any** anonymous sender qualifies, not only an anonymous *admin* | `:318` | **preserve as fixed** — v2's `cb_core.admins.resolve_actor` already resolves this correctly for every ported command (`docs/contracts/admins.md`: Telegram only permits `sender_chat` = the group itself for an admin), so using it here is the established v2 semantic, not a change made by this port |

## QA vs v1 conflicts

`Cookiebot-QA/features/util_deletereposts.feature`, two scenarios:

1. **Trigger name.** QA says `/deletereposts`; v1 ships `/deleteposts` and
   `/apagarposts`. Already logged in `feature-map.mdx:50` and already aliased
   both ways. **All three spellings must resolve.**
2. **The non-admin message.** QA asserts *"You don't have permission to use this
   command or are in anonymous mode"* plus *"a video displaying how to remove
   anonymous mode from the user settings"*. That is `/configurar`'s refusal
   (`Configurations.py:139-143`), not `cancel_posts`'s — v1 answers this command
   with the plain `"You are not a group admin!"` and sends no video. **v1 wins
   for observable behaviour** (AGENTS.md §1); the scenario is ported with its
   `Then` corrected to v1's actual string, and the divergence recorded in
   `feature-map.mdx`.
3. QA's first scenario asserts *"all posts sent by the post getter feature are
   deleted"*, which reads as deleting already-delivered messages. v1 deletes
   only **scheduled, not-yet-sent** rows and touches no existing message. v1
   wins; the scenario wording is tightened to "scheduled" and the conflict
   recorded.
