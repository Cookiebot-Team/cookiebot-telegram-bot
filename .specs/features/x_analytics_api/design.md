# x_analytics_api — Design

## Module placement

| Piece | Where | Why there |
|---|---|---|
| Queries + summary | `cb_core/analytics.py` (new) | a rollup read is not HTTP-shaped; `cb-worker` reports and any future console want the same rows |
| Auth + authorisation | `cb_api/security.py` (new) | first endpoint behind the token `/login` mints |
| `public_pem` | `cb_api/keys.py` | verifying needs the public half of the same stored key `public_jwk` publishes; deriving both from one place is what keeps them from disagreeing |
| Endpoints | `cb_api/routers/analytics.py` (new) | |

No migration: all three tables and both rollup functions already exist
(`0001`, `0002`).

## R1 — the queries

**R1.1** Three functions, each taking `(group_id, start, end)` and each with
`group_id` in the `WHERE`. No "all groups" variant exists, in this module or
above it (spec.md's Scope).

**R1.2** `commands` and `llm_costs` aggregate across the window rather than
returning a row per day: "which commands does this group use" is the question,
and 40 commands × 30 days is a table nobody reads. `daily` is the per-day one.

**R1.3** `max(p95_latency_ms)`, never `avg`. Averaging percentiles is
meaningless; the worst day is the answerable question.

**R1.4** `numeric` costs are converted to `float` in the struct. asyncpg hands
back `Decimal`, which JSON cannot encode, and converting once at the boundary
beats a custom encoder or a conversion at every call site.

**R1.5** `summarise` folds rows the caller already has — no second query, and
no `sum()` in SQL that would drift from the struct.

## R2 — auth

**R2.1** RS256 against **every** published key, not just the current signing
key: a rotation publishes both, and a token minted a minute before the swap
must keep working until it expires.

**R2.2** `iss` is not verified. It is derived per request from the URL the
client reached (`routers/login._issuer`), so a deployment reachable by two
names would reject its own tokens.

**R2.3** `sub` must parse as an integer — it is a Telegram user id. Anything
else is a token this deployment did not issue for a user.

## R3 — authorisation

**R3.1** `group_admins` membership for the requested group, or `Tenant.owns`.

**R3.2** **No Telegram refresh.** `cb_core.admins.refresh` needs a `Bot`;
cb-api has none and should not grow one. The table is written by the gateway on
every admin-gated command, so it is as fresh as the group's own activity — and
an HTTP read is not where "someone was promoted an hour ago" should be
discovered.

**R3.3** 404 for a group the caller does not administer, never 403.

## R4 — windows

**R4.1** Defaults and cap in `resolve_window`, a pure function with its own
tests: 30 days by default, 366 maximum, one end derived from the other when
only one is given.

**R4.2** Out-of-range windows are a 400, not a clamp. A caller that asked for
the wrong window should learn that rather than receive plausible numbers for a
window it did not request.

**R4.3** Dates, not timestamps: the rollups are daily and computed in UTC, and
accepting an instant would imply a resolution that does not exist.

## Open decisions — answered

1. **No fleet-wide endpoint.** Cross-tenant leak plus a Citus fan-out; the
   operator's own numbers are already in Grafana.
2. **404, not 403**, for an unauthorised group (R3.3).
3. **Absent days stay absent.** Zero-filling is indistinguishable from a real
   quiet day, and the caller drawing the chart knows what its x-axis is.
