"""x_admin_audit's fleet-wide audit read: two indexes on group_audit_events

The fleet viewer reads `group_audit_events` across every group, newest first
(`ORDER BY id DESC LIMIT n`, keyset on `id`). The primary key leads with
`group_id`, so without an index on `id` alone each shard would have to scan and
sort its rows; `id` is a UUIDv7, so `id DESC` is chronological and each shard
can stream its top-n straight off the index and let the coordinator merge.

The second index serves the `surface` filter. It leads with `group_id`, the
shard key (AGENTS.md §4), so the per-group variant stays a router query.

Citus propagates both indexes to every shard of the distributed table.

Revision ID: 0011
Revises: 0010
"""

from __future__ import annotations

from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE INDEX IF NOT EXISTS group_audit_events_id_desc ON group_audit_events (id DESC)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS group_audit_events_surface "
        "ON group_audit_events (group_id, surface, id DESC)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS group_audit_events_surface")
    op.execute("DROP INDEX IF EXISTS group_audit_events_id_desc")
