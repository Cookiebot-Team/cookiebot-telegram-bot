# Contract: util_deletereposts (v1 -> v2)

Phase 2/6 of `/migrate-feature` for `/deleteposts`. QA:
`../Cookiebot-QA/features/util_deletereposts.feature`. FEATURE-MAP row:
`util_deletereposts`. Spec/design:
`.specs/features/util_deletereposts/{spec,design,tasks}.md`.

Shipped alongside `util_postforwarder`, which owns the `scheduled_posts` table
this command deletes from. Files owned here:
`packages/cb-gateway/src/cb_gateway/handlers/deletereposts.py`, its one
registration line, `scheduled_posts.delete_by_requester`, and the tests below.

## Phase 1 — where v1 lives

- `cancel_posts`, `../COOKIEBOT-Telegram-Group-Bot/Bot/Publisher.py:316-327`.
- Dispatch: `COOKIEBOT.py:209-211`. The tuple lists `"/apagarposts"` **twice**
  and never ships `/deletereposts`, which is what QA specifies.
- Strings: inline ternaries, not `Bot/Static/locales/` — new `cb.json` keys.

## Phase 2 — v1 behaviour contract

| Aspect | v1 behaviour (file:line) |
|---|---|
| Triggers | `/deleteposts`, `/apagarposts` (`COOKIEBOT.py:209`). QA's `/deletereposts` does not exist in v1; all three resolve in v2 (`textmatch.py:60-62`). |
| Preconditions | admin **or** `'sender_chat' in msg` (`:318`); admins fetched with `ignorecache=True` (`COOKIEBOT.py:210`). **No `ownerID` bypass**, unlike `/repost` (`:290`) — the owner cannot cancel a group's posts unless they are an admin of it. |
| Feature flag | none — not gated on `functionsFun`/`functionsUtility` |
| Cooldowns | none |
| Not an admin | reply `not_group_admin` (`"You are not a group admin!"` / `"Você não é um administrador do grupo!"` / `"¡No eres un administrador del grupo!"`), return (`:319-321`) |
| Scope of the delete | every row whose `second_chatid` equals this chat — everything **this chat requested**, regardless of which group it targets (`:322-324`) |
| Success output | react `👍` (`is_big=True`, `react_to_message`'s default), then reply `deletereposts_done` (`:325-327`) |
| Persistence | `DELETE FROM publisher WHERE name = ?`, once per matching row (`:120`) |
| Side effects | `send_chat_action(typing)` (`:317`). Already-delivered posts are untouched — only future ones are cancelled. |
| External calls | none |

## Defects — verdict per item

| id | Defect | Verdict |
|---|---|---|
| D-DR-1 | Reads every row into Python and issues one `DELETE ... WHERE name = ?` per match, against the unlocked shared SQLite connection of D-PF-1 | **fix** — one predicate-driven statement |
| D-DR-2 | `'sender_chat' in msg` is treated as sufficient authorisation, so **any** anonymous sender qualifies, not only an anonymous *admin* | **fix** — `ctx.is_admin`, i.e. `cb_core.admins.resolve_actor`, which resolves this correctly (`docs/contracts/admins.md`) and is what every other ported admin command already uses. Strictly narrower than v1. |

## The delete fans out across shards, on purpose

`DELETE FROM scheduled_posts WHERE requester_chat_id = $1` filters on a column
that is **not** the distribution key, so Citus sends it to every shard. That is
deliberate and there is no alternative: the rows this command cancels are, by
definition, spread across every group the campaign targeted, so no `group_id`
predicate would be correct. It is index-backed
(`scheduled_posts_requester_idx`), single-table DML rather than a repartition
join, and it runs at most once per admin invocation. AGENTS.md §4's rule for
this case is "say so" — the comment is at the call site and in
`cb_core/scheduled_posts.py`'s module docstring.

The row count is logged (`publisher.reposts_cancelled`); v1 reported nothing and
the reply text is unchanged.

`send_chat_action(typing)` is not ported — no ported command sends one, on the
grounds that it is a no-op for a reply issued in the same round trip.

## Phase 6 — parity table

| Aspect | Verdict |
|---|---|
| All three trigger spellings resolve | **same** (and QA's, which v1 never had) |
| Refusal string, all three languages | **same, verbatim** |
| Success string, all three languages | **same, verbatim** |
| 👍 before the reply, `is_big` default | **same** |
| Scope: everything this chat requested, across every target group | **same** |
| No `ownerID` bypass | **same** — v1's asymmetry with `/repost`, reproduced |
| Only scheduled rows are removed; delivered messages are untouched | **same** |
| Storage and the number of statements | **changed (intentional, fix)** — D-DR-1 |
| Who counts as authorised when anonymous | **changed (intentional, fix)** — D-DR-2; an anonymous non-admin is now refused |
| `send_chat_action(typing)` | **changed (intentional)** — consistent with every ported command |

## Tests

| Layer | File |
|---|---|
| Unit — all three spellings, the shared canonical name | `packages/cb-gateway/tests/test_deletereposts.py` |
| Integration — rows across two target groups cancelled together, another requester's untouched | `qa/integration/test_scheduled_posts.py::TestCancel` |
| Acceptance — both QA scenarios, corrected | `qa/features/util_deletereposts.feature`, `qa/test_util_deletereposts.py` |

## QA vs v1 conflicts recorded

1. **Trigger name.** QA says `/deletereposts`; v1 ships `/deleteposts` and
   `/apagarposts`. Already in `feature-map.mdx:50`; all three resolve.
2. **The refusal.** QA asserts *"You don't have permission to use this command
   or are in anonymous mode"* plus a video showing how to leave anonymous mode.
   That is `/configurar`'s refusal (`Configurations.py:139-143`), not this
   command's. v1 wins; the scenario's `Then` is corrected and the acceptance
   test additionally asserts **no** `sendVideo`.
3. **Scope.** QA scenario 1 says "all posts sent by the post getter feature are
   deleted", which reads as deleting delivered messages. v1 deletes scheduled,
   not-yet-sent rows only. Wording tightened to "scheduled".
