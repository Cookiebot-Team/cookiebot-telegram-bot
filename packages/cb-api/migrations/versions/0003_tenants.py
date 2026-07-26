"""tenants: many bots on one core

v1 shipped multi-tenancy without naming it — five personas chosen by a CLI
argument, per-group feature flags, per-group custom commands pulled from a GCS
prefix, locale packs, and one global owner id. This gives that a home.

`tenants` is a **reference table**: a handful of rows, joined from bot lookup and
from the per-update path, so replication makes every read node-local. The shard
key stays `group_id` — a tenant is a logical boundary, not a second distribution
column, so no query plan changes and no data moves.

Revision ID: 0003
Revises: 0002
"""

from __future__ import annotations

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE tenants (
            tenant_id         text PRIMARY KEY,
            display_name      text NOT NULL,
            -- which handler pack builds this tenant's router; "core" is shared
            handler_pack      text NOT NULL DEFAULT 'core',
            owner_ids         bigint[] NOT NULL DEFAULT '{}',
            default_locale    text NOT NULL DEFAULT 'en',
            disabled_commands text[] NOT NULL DEFAULT '{}',
            feature_defaults  jsonb NOT NULL DEFAULT '{}'::jsonb,
            llm_overrides     jsonb NOT NULL DEFAULT '{}'::jsonb,
            storage_prefix    text NOT NULL DEFAULT '',
            monthly_llm_budget_usd numeric(10, 2),
            active            boolean NOT NULL DEFAULT true,
            created_at        timestamptz NOT NULL DEFAULT now(),
            updated_at        timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("SELECT create_reference_table('tenants')")

    # The five v1 personas become the first tenants, all on the shared pack.
    op.execute(
        """
        INSERT INTO tenants (tenant_id, display_name) VALUES
            ('cookiebot', 'Cookiebot'),
            ('bombot',    'Bombot')
        ON CONFLICT (tenant_id) DO NOTHING
        """
    )

    # bots belong to a tenant. Reference-to-reference FK: no distributed impact.
    op.execute("ALTER TABLE bots ADD COLUMN tenant_id text NOT NULL DEFAULT 'cookiebot'")
    op.execute("UPDATE bots SET tenant_id = skin WHERE skin IN (SELECT tenant_id FROM tenants)")
    op.execute(
        "ALTER TABLE bots ADD CONSTRAINT bots_tenant_fk "
        "FOREIGN KEY (tenant_id) REFERENCES tenants (tenant_id)"
    )
    op.execute("CREATE INDEX bots_tenant_idx ON bots (tenant_id)")

    # Groups carry their tenant so routing never needs a second lookup. Kept as a
    # plain column rather than part of the distribution key — repartitioning a
    # live cluster to add it would be a data move for no query-plan gain.
    op.execute("ALTER TABLE groups ADD COLUMN tenant_id text NOT NULL DEFAULT 'cookiebot'")
    op.execute("CREATE INDEX groups_tenant_idx ON groups (tenant_id, group_id)")

    # Per-tenant spend, rolled up from llm_usage. Distributed and colocated so the
    # aggregation stays per-shard.
    op.execute(
        """
        CREATE TABLE tenant_monthly_cost (
            group_id   bigint NOT NULL,
            tenant_id  text   NOT NULL,
            month      date   NOT NULL,
            cost_usd   numeric(12, 4) NOT NULL DEFAULT 0,
            calls      bigint NOT NULL DEFAULT 0,
            computed_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (group_id, tenant_id, month)
        )
        """
    )
    op.execute(
        "SELECT create_distributed_table('tenant_monthly_cost', 'group_id', "
        "colocate_with => 'groups')"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS tenant_monthly_cost CASCADE")
    op.execute("DROP INDEX IF EXISTS groups_tenant_idx")
    op.execute("ALTER TABLE groups DROP COLUMN IF EXISTS tenant_id")
    op.execute("ALTER TABLE bots DROP CONSTRAINT IF EXISTS bots_tenant_fk")
    op.execute("DROP INDEX IF EXISTS bots_tenant_idx")
    op.execute("ALTER TABLE bots DROP COLUMN IF EXISTS tenant_id")
    op.execute("DROP TABLE IF EXISTS tenants CASCADE")
