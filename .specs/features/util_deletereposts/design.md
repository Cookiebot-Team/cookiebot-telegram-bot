# util_deletereposts — Design

Consumes `util_postforwarder`'s `scheduled_posts` table; see
`.specs/features/util_postforwarder/design.md` R1.

## R1 — the handler

**R1.1** `packages/cb-gateway/src/cb_gateway/handlers/deletereposts.py`, one
handler on `CommandName("deletereposts")` (the canonical name all three
spellings already map to, `textmatch.py:60-62`) and
`F.chat.type != ChatType.PRIVATE` — the established group-only pattern.

**R1.2 Admin gate.** `ctx.is_admin`, i.e. `cb_core.admins.resolve_actor`, which
already treats an anonymous sender as an admin because Telegram only permits
`sender_chat` = the group for one (`docs/contracts/admins.md`). This is
strictly narrower than v1's `'sender_chat' in msg` (D-DR-2) and is the semantic
every other ported admin command in v2 already uses. Refusal replies
`not_group_admin` and returns — no video, no anonymous-mode wording (the QA
scenario is wrong about that; see `spec.md`).

**R1.3 No `owner_id` bypass.** `schedule_autopost` has one (`:290`) and
`cancel_posts` does not (`:318`). Reproduced as-is, asymmetry included.

## R2 — the delete

**R2.1** One statement, replacing v1's read-all-then-delete-per-row loop
(D-DR-1):

```sql
DELETE FROM scheduled_posts WHERE requester_chat_id = $1
```

**R2.2** This filters on a **non-distribution column**, so Citus fans it out to
every shard. That is deliberate and is the design's R1.4: the rows this command
cancels are, by definition, spread across every group the campaign targeted, so
no `group_id` predicate exists that would be correct. It is index-backed
(`scheduled_posts_requester_idx`), single-table DML — not a repartition join —
and it runs at most once per admin invocation. The call site carries that
comment; AGENTS.md §4's rule is "say so", not "never".

**R2.3** The statement returns its row count and the handler logs it
(`publisher.reposts_cancelled`, `count=…`). v1 reported nothing; the reply text
is unchanged.

## R3 — output

**R3.1** React `👍` to the command (`message.react`, `is_big` left at Telegram's
default — v1 calls `react_to_message` without the argument, which defaults to
`True`, `universal_funcs.py:300`), then reply `deletereposts_done`. Reaction
first, then the reply, in v1's order (`:325-327`); the reaction is best-effort
suppressed like every other in this codebase.

**R3.2** No `send_chat_action`. v1 opens with `typing` (`:317`); v2 has never
ported the chat action for any command, on the grounds that it is a no-op for a
reply issued in the same round trip. Consistent with the eleven commands
already shipped, recorded in the contract rather than silently dropped.

## R4 — telemetry

**R4.1** `cb_gateway_deletereposts_total{outcome}`, outcome in
`cancelled|denied`. Row counts go to the log, not to a label.

## Open decisions — answered

1. **One fan-out `DELETE`, not a per-row loop.** R2.1/R2.2.
2. **`ctx.is_admin`, so an anonymous *non*-admin is refused.** R1.2 — v2's
   established semantic, and v1's check is a hole rather than a behaviour.
3. **QA's "no permission / anonymous mode" message and its video are not
   ported.** They belong to `/configurar`; v1 answers this command with
   `not_group_admin`. Recorded in `feature-map.mdx`.
